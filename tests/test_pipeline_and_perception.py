import csv
from pathlib import Path

from agent import pipeline
from agent.store import ACTIONS_KEY, DEVIATIONS_KEY, FINDINGS_KEY, Store
from agent import rules
from perception import labels_mode

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "sample_manifest.csv"


def test_sample_manifest_is_valid_and_deterministic():
    from data.generate_sample import build
    a = build(seed=42, days=2, units_per_shift=4)
    b = build(seed=42, days=2, units_per_shift=4)
    assert a == b and len(a) > 0
    acts, problems = labels_mode.manifest_rows_to_actions(a)
    assert problems == [] and len(acts) == len(a)


def test_bundled_manifest_loads():
    acts, problems = labels_mode.read_manifest(SAMPLE)
    assert problems == []
    assert len({a["unit_id"] for a in acts}) == 1792


def test_manifest_validation_reports_problems():
    rows = [{"unit_id": "u", "station": "s", "worker": "w", "date": "d", "shift": "A",
             "t_start": "0", "t_end": "x", "action": "place", "tool": "", "seq": "1"},
            {"unit_id": "u", "station": "s", "worker": "w", "date": "d", "shift": "A",
             "t_start": "0", "t_end": "5", "action": "dance", "tool": "", "seq": "2"},
            {"unit_id": "u", "station": "s", "worker": "w", "date": "d", "shift": "A",
             "t_start": "0", "t_end": "5", "action": "Screw", "tool": "Hex Key", "seq": "3"}]
    acts, problems = labels_mode.manifest_rows_to_actions(rows, rules.load_procedure())
    assert len(acts) == 1 and acts[0]["action"] == "screw" and acts[0]["tool"] == "hex_key"
    assert len(problems) == 2


def test_havid_segments_to_manifest():
    from perception import havid_mode
    segs = [
        {"video_id": "S01A02I01", "start_frame": "30", "end_frame": "270", "subject": "right hand", "verb": "Place", "object": "Base Plate", "target": "box frame", "tool": "null"},
        {"video_id": "S01A02I01", "start_frame": "300", "end_frame": "480", "subject": "left hand", "verb": "insert", "object": "shaft", "target": "gear plate", "tool": ""},
        {"video_id": "S01A02I01", "start_frame": "500", "end_frame": "560", "subject": "", "verb": "null", "object": "", "target": "", "tool": ""},
        {"video_id": "S01A02I01", "start_frame": "600", "end_frame": "1200", "subject": "both", "verb": "screw", "object": "nut", "target": "shaft", "tool": "Wrench"},
        {"video_id": "S99", "start_frame": "0", "end_frame": "10", "verb": "place"},
    ]
    ctx = [{"video_id": "S01A02I01", "worker": "W-101", "station": "ST-01", "date": "2026-11-01", "shift": "A", "start_time": "06:10:00"}]
    vocab = {"verbs": {"null": ""}, "objects": {"shaft": "gear_shaft", "nut": "shaft_nut"}, "tools": {"wrench": "nut_wrench"}}
    vocab = {k: {labels_mode.norm(a): labels_mode.norm(b) for a, b in v.items()} for k, v in vocab.items()}
    rows, problems = havid_mode.segments_to_manifest(segs, ctx, fps=30.0, vocab=vocab)
    assert len(problems) == 1 and "S99" in problems[0]
    assert [r["action"] for r in rows] == ["place", "insert", "screw"]
    assert rows[0]["object"] == "base_plate" and rows[1]["object"] == "gear_shaft"
    assert rows[2]["tool"] == "nut_wrench" and rows[2]["t_start"] == 19.0 and rows[2]["t_end"] == 39.0
    assert len({r["unit_id"] for r in rows}) == 1 and rows[0]["station"] == "ST-01"
    acts, problems = labels_mode.manifest_rows_to_actions(rows, rules.load_procedure())
    assert problems == [] and len(acts) == 3
    f = rules.evaluate_unit(labels_mode.units_from_actions(acts)[0], acts, rules.load_procedure())
    assert f.steps_matched == 3 and [d.step_id for d in f.deviations if d.type == "missed_step"] == [3, 5, 6, 7, 8, 9]


def test_pipeline_end_to_end_local(tmp_path):
    store = Store(data_dir=str(tmp_path))
    acts, _ = labels_mode.read_manifest(SAMPLE)
    assert pipeline.ingest_actions(store, acts) == len(acts)
    assert pipeline.ingest_actions(store, acts) == 0  # idempotent
    res = pipeline.analyze(store)
    assert res["units_analyzed"] == 1792
    assert pipeline.analyze(store)["units_analyzed"] == 0  # nothing pending
    findings = store.read_csv(FINDINGS_KEY)
    devs = store.read_csv(DEVIATIONS_KEY)
    assert len(findings) == res["units_analyzed"]
    types = {d["type"] for d in devs}
    # The sample data is built so every rule fires.
    assert set(types) == {"missed_step", "out_of_order", "wrong_tool", "over_time", "posture_strain", "fatigue_indicator"}
    worst = min((s for s in {f["station"] for f in findings}),
                key=lambda s: sum(1 for f in findings if f["station"] == s and f["compliant"] == "true")
                / sum(1 for f in findings if f["station"] == s))
    assert worst == "ST-02"
    fatigue = [x["worker"] for x in devs if x["type"] == "fatigue_indicator"]
    assert fatigue.count("W-104") >= 10 and fatigue.count("W-107") >= 10
    assert store.read_csv(ACTIONS_KEY)


def test_procedure_override_from_store(tmp_path):
    store = Store(data_dir=str(tmp_path))
    proc = pipeline.load_procedure(store)
    proc["procedure_id"] = "plant-override"
    proc["severity"]["over_time"] = "high"
    store.write_json("procedure/plant.json", proc)
    assert pipeline.load_procedure(store)["procedure_id"] == "plant-override"
