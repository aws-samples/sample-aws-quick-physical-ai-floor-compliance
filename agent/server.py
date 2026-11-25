"""Floor compliance agent: an MCP server for AWS Quick.

Runs on Amazon Bedrock AgentCore Runtime (streamable HTTP on port 8000 at /mcp, bound to the address in MCP_HOST,
stateless) and is registered in AWS Agent Registry, which is how AWS Quick
discovers it. Every tool reads the findings tables the rules engine wrote; the
NVIDIA Nemotron narrative is optional and never changes a number.

Environment:
    FINDINGS_BUCKET   S3 bucket with actions/, findings/, deviations/ (omit for local DATA_DIR)
    DATA_DIR          local folder used when FINDINGS_BUCKET is not set
    MODEL_ID          Bedrock model id for narratives (default nvidia.nemotron-nano-3-30b)
    LLM_ENABLED       "false" to skip Bedrock entirely
    PORT              default 8000
"""
from __future__ import annotations

import os
from collections import Counter, defaultdict

from mcp.server.fastmcp import FastMCP

from agent import llm, pipeline
from agent.planning import allocation
from agent.store import ACTIONS_KEY, DEVIATIONS_KEY, FINDINGS_KEY, Store

mcp = FastMCP(
    "floor-compliance-agent",
    instructions=(
        "Tools over a manufacturing floor compliance findings table. Dates are YYYY-MM-DD; "
        "shifts are A, B or C; stations look like ST-01; workers look like W-101. "
        "Call list_filters first if you are unsure which values exist."
    ),
    # Loopback by default. AgentCore Runtime needs the server on all interfaces inside its
    # isolated microVM, so the CloudFormation template sets MCP_HOST for the runtime only.
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("PORT", "8000")),
    stateless_http=True,
)

_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def _findings() -> list[dict]:
    return store().read_csv(FINDINGS_KEY)


def _deviations() -> list[dict]:
    return store().read_csv(DEVIATIONS_KEY)


def _latest_date(rows: list[dict]) -> str:
    return max((r["date"] for r in rows), default="")


def _pct(rows: list[dict]) -> float | None:
    if not rows:
        return None
    ok = sum(1 for r in rows if str(r.get("compliant")).lower() == "true")
    return round(100.0 * ok / len(rows), 1)


def _match(r: dict, **filters) -> bool:
    for k, v in filters.items():
        if v and str(r.get(k, "")).lower() != str(v).lower():
            return False
    return True


# ---------------------------------------------------------------- tools

@mcp.tool()
def list_filters() -> dict:
    """List the dates, shifts, stations and workers present in the findings, so questions can use real values."""
    f = _findings()
    return {
        "dates": sorted({r["date"] for r in f}),
        "shifts": sorted({r["shift"] for r in f}),
        "stations": sorted({r["station"] for r in f}),
        "workers": sorted({r["worker"] for r in f}),
        "latest_date": _latest_date(f),
        "units_total": len(f),
    }


@mcp.tool()
def get_shift_summary(date: str = "", shift: str = "") -> dict:
    """Compliance summary for one date and shift: compliance percent by station, deviation counts by type and severity, and the top deviations. Leave date empty for the latest date; leave shift empty for all shifts that day."""
    f = _findings()
    d = _deviations()
    date = date or _latest_date(f)
    fr = [r for r in f if _match(r, date=date, shift=shift)]
    dr = [r for r in d if _match(r, date=date, shift=shift)]
    by_station = []
    for st in sorted({r["station"] for r in fr}):
        srows = [r for r in fr if r["station"] == st]
        sdev = [r for r in dr if r["station"] == st]
        by_station.append({
            "station": st, "units": len(srows), "compliance_pct": _pct(srows),
            "deviations": len(sdev),
            "high": sum(1 for x in sdev if x["severity"] == "high"),
            "medium": sum(1 for x in sdev if x["severity"] == "medium"),
            "low": sum(1 for x in sdev if x["severity"] == "low"),
        })
    by_station.sort(key=lambda s: (s["compliance_pct"] if s["compliance_pct"] is not None else 101))
    by_type = dict(Counter(r["type"] for r in dr))
    sev_rank = {"high": 0, "medium": 1, "low": 2}
    top = sorted(dr, key=lambda r: (sev_rank.get(r["severity"], 9), r["ts"]))[:5]
    facts = {
        "date": date, "shift": shift or "all", "units": len(fr), "compliance_pct": _pct(fr),
        "by_station": by_station, "deviations_by_type": by_type,
        "top_deviations": [{k: r[k] for k in ("deviation_id", "station", "worker", "type", "severity", "detail")} for r in top],
    }
    narrative = llm.narrate("Summarize this shift for the operations manager.", facts) or (
        f"On {date} ({'shift ' + shift if shift else 'all shifts'}) {len(fr)} units were assembled with "
        f"{facts['compliance_pct']}% compliance and {len(dr)} deviations. "
        + (f"Lowest station: {by_station[0]['station']} at {by_station[0]['compliance_pct']}%." if by_station else "")
    )
    return {**facts, "narrative": narrative, "narrative_source": "model" if llm.enabled() and narrative and "units were assembled" not in narrative else "rules"}


@mcp.tool()
def list_deviations(date: str = "", shift: str = "", station: str = "", worker: str = "",
                    severity: str = "", type: str = "", limit: int = 50) -> dict:
    """List deviations with optional filters (date, shift, station, worker, severity high|medium|low, type missed_step|out_of_order|wrong_tool|over_time|posture_strain|fatigue_indicator). Each row carries its evidence clip reference."""
    d = _deviations()
    date = date or _latest_date(_findings())
    rows = [r for r in d if _match(r, date=date, shift=shift, station=station, worker=worker, severity=severity, type=type)]
    sev_rank = {"high": 0, "medium": 1, "low": 2}
    rows.sort(key=lambda r: (sev_rank.get(r["severity"], 9), r["station"], r["ts"]))
    return {"date": date, "count": len(rows), "returned": min(len(rows), int(limit)), "deviations": rows[: int(limit)]}


@mcp.tool()
def get_worker_scorecard(worker: str, date_from: str = "", date_to: str = "") -> dict:
    """Scorecard for one worker over a date range: units, compliance percent, cycle time, deviations by type, and strain events. Dates are YYYY-MM-DD; both default to the full range."""
    f = [r for r in _findings() if _match(r, worker=worker)]
    d = [r for r in _deviations() if _match(r, worker=worker)]
    if date_from:
        f = [r for r in f if r["date"] >= date_from]; d = [r for r in d if r["date"] >= date_from]
    if date_to:
        f = [r for r in f if r["date"] <= date_to]; d = [r for r in d if r["date"] <= date_to]
    cycle = [float(r["cycle_seconds"]) for r in f if r.get("cycle_seconds")]
    by_day = defaultdict(list)
    for r in f:
        by_day[r["date"]].append(r)
    facts = {
        "worker": worker, "units": len(f), "compliance_pct": _pct(f),
        "avg_cycle_seconds": round(sum(cycle) / len(cycle), 1) if cycle else None,
        "deviations_by_type": dict(Counter(r["type"] for r in d)),
        "strain_events": sum(1 for r in d if r["type"] == "posture_strain"),
        "fatigue_indicators": sum(1 for r in d if r["type"] == "fatigue_indicator"),
        "by_date": [{"date": k, "units": len(v), "compliance_pct": _pct(v)} for k, v in sorted(by_day.items())],
        "stations": sorted({r["station"] for r in f}),
    }
    facts["narrative"] = llm.narrate("Summarize this worker's scorecard neutrally.", facts) or (
        f"{worker}: {len(f)} units, {facts['compliance_pct']}% compliance, "
        f"{facts['strain_events']} strain events, {facts['fatigue_indicators']} fatigue indicators."
    )
    return facts


@mcp.tool()
def get_station_trend(station: str = "", days: int = 14) -> dict:
    """Daily compliance percent and deviation count for a station (or all stations when empty) over the last N days. Good for a line chart."""
    f = _findings()
    d = _deviations()
    if station:
        f = [r for r in f if _match(r, station=station)]
        d = [r for r in d if _match(r, station=station)]
    dates = sorted({r["date"] for r in f})[-int(days):]
    series = []
    for dt in dates:
        fr = [r for r in f if r["date"] == dt]
        dr = [r for r in d if r["date"] == dt]
        series.append({"date": dt, "units": len(fr), "compliance_pct": _pct(fr), "deviations": len(dr),
                       "high": sum(1 for x in dr if x["severity"] == "high")})
    return {"station": station or "all", "days": len(series), "series": series}


@mcp.tool()
def get_clip_evidence(deviation_id: str) -> dict:
    """Evidence for one deviation: the step, what was seen, and the camera clip reference with a time-limited link when the clip exists in storage."""
    row = next((r for r in _deviations() if r["deviation_id"] == deviation_id), None)
    if not row:
        return {"error": f"no deviation with id {deviation_id}"}
    clip = row.get("clip_ref") or ""
    if clip.endswith("/"):  # a missed step points at the unit's whole clip set
        keys = sorted(store().list_keys(clip))[:12]
        return {
            **row,
            "clip_available": bool(keys),
            "clip_urls": [store().presign(k) for k in keys],
            "note": None if keys else "No clips present under this unit (sample data carries references only).",
        }
    available = bool(clip) and store().exists(clip)
    return {
        **row,
        "clip_available": available,
        "clip_url": store().presign(clip) if available else None,
        "note": None if available else "Clip not present in storage (sample data carries references only).",
    }


@mcp.tool()
def get_procedure() -> dict:
    """The standard procedure in force: steps in order with tool and target seconds, thresholds, and the severity assigned to each deviation type."""
    return pipeline.load_procedure(store())


@mcp.tool()
def analyze_pending() -> dict:
    """Run the rules engine over units in the actions table that have not been analyzed yet, and append the results to the findings tables."""
    return pipeline.analyze(store(), only_new=True)


def _robot() -> dict:
    return store().planning_override() or allocation.load_robot()


def _human_model() -> dict[int, dict]:
    return allocation.human_step_model(store().read_csv(ACTIONS_KEY), _deviations(),
                                       pipeline.load_procedure(store()))


@mcp.tool()
def get_step_profile() -> dict:
    """Per procedure step: measured mean worker seconds against target, strain events per 100 units, and the robot cycle time and success rate where the robot table has the step. Use for questions about which steps are slow, strenuous, or candidates for a robot."""
    human, robot = _human_model(), _robot()
    caps = {int(k): v for k, v in robot["steps"].items()}
    rows = [{
        "step_id": sid, "step": h["name"], "tool": h["tool"] or "none",
        "worker_mean_seconds": h["mean_seconds"], "target_seconds": h["target_seconds"],
        "observations": h["observations"],
        "strain_events_per_100_units": round(100 * h["strain_per_unit"], 1),
        "robot_cycle_seconds": caps.get(sid, {}).get("cycle_seconds"),
        "robot_success_rate": caps.get(sid, {}).get("success_rate"),
    } for sid, h in sorted(human.items())]
    return {"steps": rows, "robot_data_source": robot.get("source", "unknown"),
            "robot_data_is_placeholder": bool(robot.get("placeholder"))}


@mcp.tool()
def compare_task_allocation(robot_steps: list[int] | None = None, parallel: bool = False) -> dict:
    """Compare ways to split procedure steps between the worker and a robot. With no arguments, returns the standard scenarios from the robot table. With robot_steps (step ids), evaluates that split against the all-human baseline; parallel=true lets robot and worker steps overlap. Worker timings are measured; robot timings come from the digital twin table and are flagged when they are placeholders."""
    human, robot = _human_model(), _robot()
    if robot_steps:
        scenarios = [{"name": "Baseline: all human", "robot_steps": []},
                     {"name": f"Robot does steps {sorted(robot_steps)}" + (", overlapped" if parallel else ""),
                      "robot_steps": robot_steps, "parallel": parallel}]
    else:
        scenarios = robot["scenarios"]
    try:
        rows = allocation.compare(human, robot, scenarios)
    except ValueError as exc:
        return {"error": str(exc), "robot_steps_available": sorted(int(k) for k in robot["steps"])}
    out = {"scenarios": rows, "robot_data_is_placeholder": bool(robot.get("placeholder")),
           "robot_data_source": robot.get("source", "unknown")}
    if robot.get("placeholder"):
        out["caution"] = ("Robot cycle times and success rates are placeholders, not simulation results. "
                          "Treat this comparison as illustrative.")
    return out


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
