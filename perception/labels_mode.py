"""Perception, labels mode.

The reference deployment does not run a vision model on GPU. It takes action
labels as the perception output: one row per action with who acted (subject),
what they did (action verb), on which part (object), onto what (target), and
with which tool. That is the five-element description HA-ViD uses, and it is
what a vision and action model would produce on a plant's own cameras.

The manifest CSV (see ACTION_FIELDS in agent.store) is the contract between
perception and reasoning, whatever produced it. `object` and `target` are
optional: without them the rules match on verb and tool alone.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

REQUIRED = ("unit_id", "station", "worker", "date", "shift", "t_start", "t_end")
_TOKEN = re.compile(r"[^a-z0-9]+")


def norm(value: str | None) -> str:
    """Lowercase snake_case token: 'Hex Key' -> 'hex_key'. Empty for none/null."""
    v = _TOKEN.sub("_", (value or "").strip().lower()).strip("_")
    return "" if v in ("none", "null", "na", "n_a") else v


def manifest_rows_to_actions(rows: list[dict], procedure: dict | None = None) -> tuple[list[dict], list[str]]:
    """Validate and normalize manifest rows. Returns (actions, problems).

    With a procedure, verbs outside its `actions` list are dropped and tools
    outside its `tools` list are reported (the row is kept, so the rules can
    still flag a wrong tool).
    """
    verbs = set(procedure.get("actions") or []) if procedure else set()
    tools = set(procedure.get("tools") or []) if procedure else set()
    out, problems = [], []
    for i, r in enumerate(rows, start=2):  # header is line 1
        action = norm(r.get("action"))
        if not action or (verbs and action not in verbs):
            problems.append(f"line {i}: action '{r.get('action')}' is not in the procedure vocabulary (row skipped)")
            continue
        miss = next((k for k in REQUIRED if r.get(k) in (None, "")), None)
        if miss:
            problems.append(f"line {i}: missing {miss}")
            continue
        try:
            float(r["t_start"]); float(r["t_end"])
        except ValueError:
            problems.append(f"line {i}: t_start/t_end not numeric")
            continue
        tool = norm(r.get("tool"))
        if tool and tools and tool not in tools:
            problems.append(f"line {i}: tool '{r.get('tool')}' is not in the procedure tool list")
        out.append({**r, "action": action, "tool": tool,
                    "subject": norm(r.get("subject")), "object": norm(r.get("object")),
                    "target": norm(r.get("target")), "seq": r.get("seq") or str(len(out) + 1)})
    return out, problems


def read_manifest(path: str | Path, procedure: dict | None = None) -> tuple[list[dict], list[str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return manifest_rows_to_actions(rows, procedure)


def units_from_actions(actions: list[dict]) -> list[dict]:
    """Collapse action rows to one unit dict each (first row wins for unit fields)."""
    units: dict[str, dict] = {}
    for a in actions:
        u = units.setdefault(a["unit_id"], {
            "unit_id": a["unit_id"], "station": a["station"], "worker": a["worker"],
            "date": a["date"], "shift": a["shift"], "start_ts": a.get("start_ts", ""),
            "end_ts": a.get("end_ts", ""),
        })
        if a.get("end_ts") and a["end_ts"] > u["end_ts"]:
            u["end_ts"] = a["end_ts"]
    return list(units.values())
