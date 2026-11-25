import copy

import pytest

from agent import rules

PROC = rules.load_procedure()
UNIT = {"unit_id": "U1", "station": "ST-01", "worker": "W-101", "date": "2026-11-01",
        "shift": "A", "start_ts": "2026-11-01T06:00:00", "end_ts": "2026-11-01T06:02:00"}


def perfect_actions():
    t = 0.0
    out = []
    for i, s in enumerate(PROC["steps"], start=1):
        out.append({"seq": i, "action": s["action"], "object": s["object"], "tool": s["tool"] or "", "t_start": t,
                    "t_end": t + s["target_seconds"], "posture_score": 0.3, "clip_ref": f"c{i}"})
        t += s["target_seconds"] + 1
    return out


def test_procedure_validates():
    rules.validate_procedure(PROC)
    assert len(PROC["steps"]) == 9


def test_perfect_unit_is_compliant():
    f = rules.evaluate_unit(UNIT, perfect_actions(), PROC)
    assert f.deviations == []
    assert f.compliant and f.steps_matched == 9


def test_missed_step_is_high():
    acts = [a for a in perfect_actions() if a["seq"] != 4]
    f = rules.evaluate_unit(UNIT, acts, PROC)
    types = [d.type for d in f.deviations]
    assert types == ["missed_step"]
    assert f.deviations[0].step_id == 4 and f.deviations[0].severity == "high"
    assert not f.compliant


def test_wrong_tool_detected():
    acts = perfect_actions()
    acts[5]["tool"] = "screwdriver"  # step 6 expects hex_key
    f = rules.evaluate_unit(UNIT, acts, PROC)
    wt = [d for d in f.deviations if d.type == "wrong_tool"]
    assert len(wt) == 1 and wt[0].step_id == 6 and wt[0].severity == "medium"
    assert not any(d.type == "missed_step" for d in f.deviations)


def test_out_of_order_detected():
    acts = perfect_actions()
    acts[1], acts[2] = acts[2], acts[1]           # swap steps 2 and 3
    acts[1]["t_start"], acts[2]["t_start"] = 8.0, 20.0
    acts[1]["t_end"], acts[2]["t_end"] = 20.0, 26.0
    f = rules.evaluate_unit(UNIT, acts, PROC)
    assert any(d.type == "out_of_order" and d.step_id == 2 for d in f.deviations)
    assert not any(d.type == "missed_step" for d in f.deviations)


def test_over_time_uses_ratio_from_procedure():
    acts = perfect_actions()
    acts[3]["t_end"] = acts[3]["t_start"] + PROC["steps"][3]["target_seconds"] * 1.6
    f = rules.evaluate_unit(UNIT, acts, PROC)
    ot = [d for d in f.deviations if d.type == "over_time"]
    assert len(ot) == 1 and ot[0].severity == "low"
    assert f.compliant  # low severity does not count against the score


def test_posture_strain_needs_score_and_duration():
    acts = perfect_actions()
    acts[2]["posture_score"] = 0.8                     # 12 s step, long enough
    acts[1]["posture_score"] = 0.9                     # 6 s step, long enough
    acts[8]["posture_score"] = 0.9; acts[8]["t_end"] = acts[8]["t_start"] + 3  # too short
    f = rules.evaluate_unit(UNIT, acts, PROC)
    ps = sorted(d.step_id for d in f.deviations if d.type == "posture_strain")
    assert ps == [2, 3]


def test_severity_comes_from_procedure_not_code():
    proc = copy.deepcopy(PROC)
    proc["severity"]["over_time"] = "high"
    acts = perfect_actions()
    acts[3]["t_end"] = acts[3]["t_start"] + 100
    f = rules.evaluate_unit(UNIT, acts, proc)
    assert [d.severity for d in f.deviations if d.type == "over_time"] == ["high"]
    assert not f.compliant


def test_fatigue_indicator_clusters_late_in_shift():
    units, acts = [], {}
    for i in range(6):
        u = dict(UNIT, unit_id=f"U{i}", start_ts=f"2026-11-01T{6 + i:02d}:00:00",
                 end_ts=f"2026-11-01T{6 + i:02d}:30:00")
        a = perfect_actions()
        if i >= 4:  # last two units slow on three steps each
            for k in (3, 5, 7):
                a[k]["t_end"] = a[k]["t_start"] + PROC["steps"][k]["target_seconds"] * 2
        units.append(u); acts[u["unit_id"]] = a
    fs = rules.evaluate_units(units, acts, PROC)
    fat = [d for f in fs for d in f.deviations if d.type == "fatigue_indicator"]
    assert len(fat) == 1 and fat[0].unit_id == "U5" and fat[0].worker == "W-101"


def test_deviation_ids_are_stable():
    acts = [a for a in perfect_actions() if a["seq"] != 9]
    a = rules.evaluate_unit(UNIT, acts, PROC).deviations[0].deviation_id
    b = rules.evaluate_unit(UNIT, acts, PROC).deviations[0].deviation_id
    assert a == b and a.startswith("dev-")


def test_invalid_procedure_rejected():
    bad = copy.deepcopy(PROC)
    bad["severity"]["missed_step"] = "critical"
    with pytest.raises(ValueError):
        rules.validate_procedure(bad)


def test_missed_step_four_does_not_cascade_into_wrong_tool():
    acts = [a for a in perfect_actions() if a["seq"] != 4]
    f = rules.evaluate_unit(UNIT, acts, PROC)
    assert [(d.type, d.step_id) for d in f.deviations] == [("missed_step", 4)]


def test_missed_step_six_and_eight():
    for missing in (6, 8):
        acts = [a for a in perfect_actions() if a["seq"] != missing]
        f = rules.evaluate_unit(UNIT, acts, PROC)
        assert [(d.type, d.step_id) for d in f.deviations] == [("missed_step", missing)]


def test_wrong_tool_on_step_six_is_a_single_deviation():
    acts = perfect_actions()
    acts[5]["tool"] = "screwdriver"
    f = rules.evaluate_unit(UNIT, acts, PROC)
    assert [(d.type, d.step_id) for d in f.deviations] == [("wrong_tool", 6)]


def test_verb_only_labels_still_match():
    """Rows without a manipulated object (verb and tool only) are matched on verb and order."""
    acts = perfect_actions()
    for a in acts:
        a["object"] = ""
    f = rules.evaluate_unit(UNIT, acts, PROC)
    assert f.deviations == [] and f.steps_matched == 9


def test_object_disambiguates_same_verb():
    """Three 'place' steps exist; the object decides which one an action is, so a swap is out of order, not a miss."""
    acts = perfect_actions()
    acts[0]["object"], acts[6]["object"] = acts[6]["object"], acts[0]["object"]
    f = rules.evaluate_unit(UNIT, acts, PROC)
    assert "missed_step" not in [d.type for d in f.deviations]
    assert "out_of_order" in [d.type for d in f.deviations]
