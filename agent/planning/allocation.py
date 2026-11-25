"""Human-robot task allocation calculator (whitepaper section 5).

Combines measured human step timings (from the actions table) with a robot
capability table (cycle time and success rate per step, measured in the
digital twin) and reports, per allocation scenario: units per hour, worker
busy and waiting seconds per unit, strain events remaining per 100 units, and
expected human retries per 100 units.

The bundled robot_capability.json holds PLACEHOLDER numbers. Replace them with
simulation results before using any output in a decision.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from agent import rules

DEFAULT_ROBOT = Path(__file__).parent / "robot_capability.json"


def load_robot(path: str | Path | None = None) -> dict:
    return json.loads(Path(path or DEFAULT_ROBOT).read_text(encoding="utf-8"))


def human_step_model(actions: list[dict], deviations: list[dict], procedure: dict) -> dict[int, dict]:
    """Mean seconds per step and strain events per unit per step, from measured data."""
    by_unit: dict[str, list[dict]] = defaultdict(list)
    for a in actions:
        by_unit[a["unit_id"]].append(a)
    durations: dict[int, list[float]] = defaultdict(list)
    for acts in by_unit.values():
        acts = sorted(acts, key=lambda a: float(a["t_start"]))
        used: set[int] = set()
        max_seen = 0
        for a in acts:
            obj = (a.get("object") or "").strip().lower() or None
            step, _ = rules._match_step(a["action"], a.get("tool") or None, obj,
                                        procedure["steps"], used, max_seen)
            if step is None:
                continue
            used.add(step["id"])
            max_seen = max(max_seen, step["id"])
            durations[step["id"]].append(float(a["t_end"]) - float(a["t_start"]))
    n_units = max(len(by_unit), 1)
    strain: dict[int, int] = defaultdict(int)
    for d in deviations:
        if d["type"] == "posture_strain" and d.get("step_id"):
            strain[int(float(d["step_id"]))] += 1
    model = {}
    for s in procedure["steps"]:
        ds = durations.get(s["id"], [])
        model[s["id"]] = {
            "name": s["name"],
            "tool": s.get("tool"),
            "observations": len(ds),
            "mean_seconds": round(sum(ds) / len(ds), 1) if ds else float(s["target_seconds"]),
            "target_seconds": float(s["target_seconds"]),
            "strain_per_unit": strain[s["id"]] / n_units,
        }
    return model


def evaluate(human: dict[int, dict], robot: dict, scenario: dict) -> dict:
    """scenario = {"name": str, "robot_steps": [ids], "parallel": bool}

    Sequential by default: the worker waits while the robot works. With
    parallel=True the robot's steps overlap the worker's steps and the unit
    time is the larger of the two shares.
    """
    robot_steps = {int(s) for s in scenario.get("robot_steps", [])}
    caps = {int(k): v for k, v in robot["steps"].items()}
    missing = sorted(s for s in robot_steps if s not in caps)
    if missing:
        raise ValueError(f"robot capability missing for steps {missing}")
    unknown = sorted(s for s in robot_steps if s not in human)
    if unknown:
        raise ValueError(f"steps {unknown} are not in the procedure")
    human_time = sum(h["mean_seconds"] for sid, h in human.items() if sid not in robot_steps)
    robot_time = sum(caps[s]["cycle_seconds"] for s in robot_steps)
    retry_rate = sum(1 - caps[s]["success_rate"] for s in robot_steps)
    retry_time = sum((1 - caps[s]["success_rate"]) * human[s]["mean_seconds"] for s in robot_steps)
    if scenario.get("parallel"):
        unit_time = max(human_time, robot_time) + retry_time
        wait = max(0.0, robot_time - human_time)
    else:
        unit_time = human_time + robot_time + retry_time
        wait = robot_time
    strain = sum(h["strain_per_unit"] for sid, h in human.items() if sid not in robot_steps)
    return {
        "scenario": scenario.get("name") or f"Robot does steps {sorted(robot_steps)}",
        "robot_steps": sorted(robot_steps),
        "unit_seconds": round(unit_time, 1),
        "units_per_hour": round(3600 / unit_time, 1) if unit_time else None,
        "worker_busy_seconds": round(human_time + retry_time, 1),
        "worker_wait_seconds": round(wait, 1),
        "strain_events_per_100_units": round(100 * strain, 1),
        "human_retries_per_100_units": round(100 * retry_rate, 1),
        "robot_data_source": robot.get("source", "unknown"),
    }


def compare(human: dict[int, dict], robot: dict, scenarios: list[dict]) -> list[dict]:
    return [evaluate(human, robot, s) for s in scenarios]
