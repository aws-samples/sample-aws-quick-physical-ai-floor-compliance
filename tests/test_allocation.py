import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from agent.planning import allocation  # noqa: E402

from agent import pipeline, rules  # noqa: E402
from agent.store import Store  # noqa: E402
from perception import labels_mode  # noqa: E402


def test_allocation_scenarios_from_sample_data():
    store = Store(data_dir=tempfile.mkdtemp())
    acts, _ = labels_mode.read_manifest(ROOT / "data" / "sample_manifest.csv")
    pipeline.ingest_actions(store, acts)
    pipeline.analyze(store)
    proc = rules.load_procedure()
    human = allocation.human_step_model(store.read_csv("actions/actions.csv"),
                                        store.read_csv("deviations/deviations.csv"), proc)
    assert set(human) == {s["id"] for s in proc["steps"]}
    assert 15 < human[4]["mean_seconds"] < 30          # near the 20 s target
    assert human[3]["strain_per_unit"] > 0             # ST-03 strain lands on step 3
    assert human[4]["observations"] > 1000
    robot = allocation.load_robot()
    rows = allocation.compare(human, robot, robot["scenarios"])
    base = rows[0]
    assert base["robot_steps"] == [] and base["worker_wait_seconds"] == 0 and base["human_retries_per_100_units"] == 0
    seq = next(r for r in rows if r["scenario"].startswith("Robot does all fastening (4"))
    par = next(r for r in rows if "overlapped" in r["scenario"])
    assert par["units_per_hour"] > seq["units_per_hour"]          # overlap helps throughput
    assert seq["worker_busy_seconds"] < base["worker_busy_seconds"]  # robot takes work off the worker
    assert seq["human_retries_per_100_units"] == round(100 * (0.04 + 0.05 + 0.05), 1)
    assert all(r["robot_data_source"].startswith("PLACEHOLDER") for r in rows)
