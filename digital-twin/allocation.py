"""Command-line wrapper for the allocation calculator in agent/planning.

    python digital-twin/allocation.py --actions data-local/actions/actions.csv \
        --deviations data-local/deviations/deviations.csv

The same calculation is served to AWS Quick by the agent's
compare_task_allocation tool. Robot numbers default to the bundled
placeholder table; pass --robot with simulation results to replace them.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import rules  # noqa: E402
from agent.planning.allocation import compare, evaluate, human_step_model, load_robot  # noqa: E402,F401


def _read(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions", required=True)
    ap.add_argument("--deviations", required=True)
    ap.add_argument("--robot", help="robot capability JSON from the digital twin (default: bundled placeholder)")
    ap.add_argument("--procedure", help="procedure JSON (default: bundled assembly-box.json)")
    a = ap.parse_args()
    proc = rules.load_procedure(a.procedure)
    robot = load_robot(a.robot)
    human = human_step_model(_read(a.actions), _read(a.deviations), proc)
    if robot.get("placeholder"):
        print("WARNING: robot capability table is a placeholder; results are illustrative only.\n")
    cols = ["scenario", "robot_steps", "units_per_hour", "worker_busy_seconds", "worker_wait_seconds",
            "strain_events_per_100_units", "human_retries_per_100_units"]
    print(" | ".join(cols))
    for r in compare(human, robot, robot["scenarios"]):
        print(" | ".join(str(r[c]) for c in cols))


if __name__ == "__main__":
    main()
