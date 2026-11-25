"""Deterministic deviation rules.

The model never sets severity. This module compares one unit's action sequence
(produced by the perception layer) with the standard procedure file and emits
deviations whose type, severity and thresholds come from that file.

Input action rows (dicts) carry at least:
    seq, action, tool, t_start, t_end, posture_score, clip_ref
Times are seconds from the start of the unit, or epoch seconds; only differences
are used.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable

DEVIATION_TYPES = (
    "missed_step",
    "out_of_order",
    "wrong_tool",
    "over_time",
    "posture_strain",
    "fatigue_indicator",
)

SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}


def load_procedure(path: str | Path | None = None) -> dict:
    """Load the procedure JSON. Defaults to the bundled assembly-box file."""
    p = Path(path) if path else Path(__file__).parent / "procedure" / "assembly-box.json"
    with open(p, "r", encoding="utf-8") as fh:
        proc = json.load(fh)
    validate_procedure(proc)
    return proc


def validate_procedure(proc: dict) -> None:
    required_top = ("procedure_id", "steps", "thresholds", "severity", "compliance")
    missing = [k for k in required_top if k not in proc]
    if missing:
        raise ValueError(f"procedure missing keys: {missing}")
    ids = [s["id"] for s in proc["steps"]]
    if ids != sorted(ids) or len(set(ids)) != len(ids):
        raise ValueError("procedure step ids must be unique and ascending")
    for t in DEVIATION_TYPES:
        if t not in proc["severity"]:
            raise ValueError(f"procedure severity missing type: {t}")
        if proc["severity"][t] not in SEVERITY_RANK:
            raise ValueError(f"unknown severity for {t}: {proc['severity'][t]}")


@dataclass
class Deviation:
    deviation_id: str
    unit_id: str
    station: str
    worker: str
    date: str
    shift: str
    step_id: int | None
    step_name: str
    type: str
    severity: str
    detail: str
    observed_seconds: float | None
    target_seconds: float | None
    clip_ref: str
    ts: str

    def to_row(self) -> dict:
        return asdict(self)


@dataclass
class UnitFinding:
    unit_id: str
    station: str
    worker: str
    date: str
    shift: str
    start_ts: str
    end_ts: str
    cycle_seconds: float
    steps_matched: int
    steps_total: int
    deviation_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    compliant: bool = True
    procedure_id: str = ""
    deviations: list[Deviation] = field(default_factory=list)

    def to_row(self) -> dict:
        d = asdict(self)
        d.pop("deviations", None)
        d["compliant"] = "true" if self.compliant else "false"  # Athena-friendly boolean text
        return d


def _dev_id(unit_id: str, dtype: str, step_id, extra: str = "") -> str:
    raw = f"{unit_id}|{dtype}|{step_id}|{extra}"
    return "dev-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _tool_ok(step_tool: str | None, action_tool: str | None) -> bool:
    return step_tool is None or (action_tool or "") == step_tool


def _same(step: dict, action: str, obj: str | None) -> bool:
    """Same verb, and same manipulated object when both sides name one."""
    if step["action"] != action:
        return False
    return not step.get("object") or not obj or step["object"] == obj


def _match_step(action: str, tool: str | None, obj: str | None, steps: list[dict],
                used: set[int], max_seen: int) -> tuple[dict | None, bool]:
    """Pick the procedure step an observed action belongs to.

    A step matches on its verb, and on the manipulated object when the action
    row carries one (HR-SAT style labels do; verb-only labels fall back to the
    verb). Order is stronger evidence than the tool. The next unmatched step
    wins when it matches and either the tool is right, or no tool-compatible
    step lies ahead with only matched steps in between. Otherwise the earliest
    tool-compatible step ahead of the last matched one is taken (a skip or an
    early step), then any compatible earlier step (out of order), then the
    earliest matching step with the wrong tool. Returns (step, wrong_tool).
    """
    unused = [s for s in steps if s["id"] not in used]
    cand = [s for s in unused if _same(s, action, obj)]
    if not cand:
        return None, False
    compat = [s for s in cand if _tool_ok(s.get("tool"), tool)]
    expected = unused[0]
    if _same(expected, action, obj):
        if _tool_ok(expected.get("tool"), tool):
            return expected, False
        for c in compat:
            if c["id"] > expected["id"] and not any(expected["id"] < s["id"] < c["id"] for s in unused):
                return c, False
        return expected, True
    fwd = [s for s in compat if s["id"] > max_seen]
    if fwd:
        return fwd[0], False
    if compat:
        return compat[0], False
    return cand[0], True


def evaluate_unit(unit: dict, actions: list[dict], proc: dict) -> UnitFinding:
    """Compare one unit's actions with the procedure and return its finding.

    unit: dict with unit_id, station, worker, date, shift, start_ts, end_ts
    actions: rows for that unit, any order; sorted here by t_start then seq
    """
    steps = proc["steps"]
    thr = proc["thresholds"]
    sev = proc["severity"]
    acts = sorted(actions, key=lambda a: (float(a.get("t_start", 0)), int(a.get("seq", 0))))

    assigned: dict[int, dict] = {}        # step_id -> action
    used_steps: set[int] = set()
    devs: list[Deviation] = []
    max_step_seen = 0
    # Evidence for a step that never happened is the unit's whole clip set.
    first_clip = next((a.get("clip_ref", "") for a in acts if a.get("clip_ref")), "")
    unit_clips = first_clip.rsplit("/", 1)[0] + "/" if "/" in first_clip else ""

    def mk(dtype, step, detail, observed=None, target=None, clip="", extra=""):
        sid = step["id"] if step else None
        return Deviation(
            deviation_id=_dev_id(unit["unit_id"], dtype, sid, extra),
            unit_id=unit["unit_id"],
            station=unit["station"],
            worker=unit["worker"],
            date=unit["date"],
            shift=unit["shift"],
            step_id=sid,
            step_name=step["name"] if step else "",
            type=dtype,
            severity=sev[dtype],
            detail=detail,
            observed_seconds=observed,
            target_seconds=target,
            clip_ref=clip,
            ts=unit.get("end_ts", ""),
        )

    for a in acts:
        action = (a.get("action") or "").lower()
        tool = (a.get("tool") or None) or None
        tool = tool.lower() if tool else None
        obj = (a.get("object") or "").strip().lower() or None
        step, wrong_tool = _match_step(action, tool, obj, steps, used_steps, max_step_seen)
        if step is None:
            continue  # extra action with no step; not a deviation by itself
        used_steps.add(step["id"])
        assigned[step["id"]] = a
        if step["id"] < max_step_seen:
            devs.append(mk("out_of_order", step,
                           f"Step {step['id']} completed after step {max_step_seen}",
                           clip=a.get("clip_ref", "")))
        max_step_seen = max(max_step_seen, step["id"])
        if wrong_tool:
            devs.append(mk("wrong_tool", step,
                           f"Used {tool or 'no tool'} where {step['tool']} is specified",
                           clip=a.get("clip_ref", "")))
        dur = float(a.get("t_end", 0)) - float(a.get("t_start", 0))
        target = float(step.get("target_seconds") or 0)
        if target > 0 and dur > target * float(thr["over_time_ratio"]):
            devs.append(mk("over_time", step,
                           f"{dur:.0f}s against a {target:.0f}s target",
                           observed=round(dur, 1), target=target, clip=a.get("clip_ref", "")))
        ps = a.get("posture_score")
        if ps not in (None, "") and float(ps) >= float(thr["posture_strain_score"]) \
                and dur >= float(thr["posture_strain_min_seconds"]):
            devs.append(mk("posture_strain", step,
                           f"Posture score {float(ps):.2f} sustained for {dur:.0f}s",
                           observed=round(dur, 1),
                           target=float(thr["posture_strain_min_seconds"]),
                           clip=a.get("clip_ref", ""), extra=str(a.get("seq", ""))))

    for s in steps:
        if s.get("required", True) and s["id"] not in used_steps:
            devs.append(mk("missed_step", s, f"No matching action for step {s['id']}", clip=unit_clips))

    t0 = min((float(a.get("t_start", 0)) for a in acts), default=0.0)
    t1 = max((float(a.get("t_end", 0)) for a in acts), default=0.0)
    uf = UnitFinding(
        unit_id=unit["unit_id"], station=unit["station"], worker=unit["worker"],
        date=unit["date"], shift=unit["shift"],
        start_ts=unit.get("start_ts", ""), end_ts=unit.get("end_ts", ""),
        cycle_seconds=round(t1 - t0, 1), steps_matched=len(used_steps),
        steps_total=len(steps), procedure_id=proc["procedure_id"], deviations=devs,
    )
    _tally(uf, proc)
    return uf


def _tally(uf: UnitFinding, proc: dict) -> None:
    uf.deviation_count = len(uf.deviations)
    uf.high_count = sum(1 for d in uf.deviations if d.severity == "high")
    uf.medium_count = sum(1 for d in uf.deviations if d.severity == "medium")
    uf.low_count = sum(1 for d in uf.deviations if d.severity == "low")
    bad = set(proc["compliance"]["counts_against_score"])
    uf.compliant = not any(d.severity in bad for d in uf.deviations)


def fatigue_indicators(findings: list[UnitFinding], proc: dict) -> list[Deviation]:
    """Per worker and shift: flag when over_time events cluster late in the shift.

    Uses each unit's end_ts (ISO 8601) to find the last `fatigue_window_minutes`
    of the worker's shift. Attaches one fatigue_indicator to the worker's last unit.
    """
    from datetime import datetime, timedelta

    thr = proc["thresholds"]
    window = timedelta(minutes=float(thr["fatigue_window_minutes"]))
    min_events = int(thr["fatigue_min_over_time_events"])
    out: list[Deviation] = []
    groups: dict[tuple, list[UnitFinding]] = {}
    for f in findings:
        groups.setdefault((f.worker, f.date, f.shift), []).append(f)
    for (worker, date, shift), units in groups.items():
        units = [u for u in units if u.end_ts]
        if not units:
            continue
        units.sort(key=lambda u: u.end_ts)
        last_end = datetime.fromisoformat(units[-1].end_ts)
        late = [u for u in units if datetime.fromisoformat(u.end_ts) >= last_end - window]
        n = sum(1 for u in late for d in u.deviations if d.type == "over_time")
        if n >= min_events:
            last = units[-1]
            dev = Deviation(
                deviation_id=_dev_id(last.unit_id, "fatigue_indicator", None, f"{worker}|{date}|{shift}"),
                unit_id=last.unit_id, station=last.station, worker=worker, date=date, shift=shift,
                step_id=None, step_name="", type="fatigue_indicator",
                severity=proc["severity"]["fatigue_indicator"],
                detail=f"{n} over-time events in the last {int(window.total_seconds() // 60)} minutes of the shift",
                observed_seconds=None, target_seconds=None,
                clip_ref=next((d.clip_ref.rsplit("/", 1)[0] + "/" for d in last.deviations if d.clip_ref), ""),
                ts=last.end_ts,
            )
            last.deviations.append(dev)
            _tally(last, proc)
            out.append(dev)
    return out


def evaluate_units(units: Iterable[dict], actions_by_unit: dict[str, list[dict]], proc: dict) -> list[UnitFinding]:
    findings = [evaluate_unit(u, actions_by_unit.get(u["unit_id"], []), proc) for u in units]
    fatigue_indicators(findings, proc)
    return findings


def compliance_score(findings: Iterable[UnitFinding]) -> float | None:
    fs = list(findings)
    if not fs:
        return None
    return round(100.0 * sum(1 for f in fs if f.compliant) / len(fs), 1)
