"""Generate the bundled sample manifest.

Deterministic (seeded) action sequences for 4 stations, 8 workers, 14 days,
labeled the way HA-ViD labels actions (subject, verb, manipulated object, target
object, tool) and following the bundled assembly-box procedure. It contains no
HA-ViD data.
Deviations are injected at known rates so the dashboard and chat have
something to show before a plant's own clips are available. Station ST-02 is
the worst performer by design; workers W-104 and W-107 slow down late in the
shift so the fatigue rule fires.

Usage: python data/generate_sample.py [--out data/sample_manifest.csv] [--units-per-shift 16]
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
PROCEDURE = HERE.parent / "agent" / "procedure" / "assembly-box.json"

STATIONS = ["ST-01", "ST-02", "ST-03", "ST-04"]
WORKERS = ["W-101", "W-102", "W-103", "W-104", "W-105", "W-106", "W-107", "W-108"]
SHIFTS = {"A": "06:00:00", "B": "14:00:00", "C": "22:00:00"}
SHIFT_HOURS = 8

MISSED_RATE = {"ST-01": 0.04, "ST-02": 0.18, "ST-03": 0.05, "ST-04": 0.03}
OUT_OF_ORDER_RATE = 0.04
WRONG_TOOL_RATE = 0.05
STRAIN_STATION = "ST-03"
FATIGUE_WORKERS = {"W-104", "W-107"}


def build(seed: int = 42, days: int = 14, start_date: str = "2026-11-01", units_per_shift: int = 16) -> list[dict]:
    rng = random.Random(seed)
    proc = json.loads(PROCEDURE.read_text(encoding="utf-8"))
    steps = proc["steps"]
    rows: list[dict] = []
    d0 = datetime.fromisoformat(start_date)
    for day in range(days):
        date = (d0 + timedelta(days=day)).date().isoformat()
        for wi, worker in enumerate(WORKERS):
            station = STATIONS[(wi + day) % len(STATIONS)]
            shift = list(SHIFTS)[(wi // 3 + day) % 3]
            shift_start = datetime.fromisoformat(f"{date}T{SHIFTS[shift]}")
            spacing = SHIFT_HOURS * 3600 / units_per_shift
            for u in range(units_per_shift):
                unit_id = f"{station}-{date}-{worker}-{u + 1:03d}"
                t_unit = shift_start + timedelta(seconds=u * spacing + rng.uniform(0, spacing * 0.3))
                frac = u / max(units_per_shift - 1, 1)  # 0 at shift start, 1 at end
                seq_steps = [dict(s) for s in steps]

                # Inject deviations
                if rng.random() < MISSED_RATE[station]:
                    drop = rng.choice([4, 6, 8, 9])
                    seq_steps = [s for s in seq_steps if s["id"] != drop]
                if rng.random() < OUT_OF_ORDER_RATE:
                    i2 = next(i for i, s in enumerate(seq_steps) if s["id"] == 2)
                    i3 = next((i for i, s in enumerate(seq_steps) if s["id"] == 3), None)
                    if i3 is not None:
                        seq_steps[i2], seq_steps[i3] = seq_steps[i3], seq_steps[i2]
                wrong_tool_on = 6 if rng.random() < WRONG_TOOL_RATE else None

                t = 0.0
                for seq, s in enumerate(seq_steps, start=1):
                    target = float(s["target_seconds"])
                    mult = rng.lognormvariate(0.0, 0.18)
                    if worker in FATIGUE_WORKERS and frac > 0.75:
                        mult *= rng.uniform(1.4, 1.9)
                    elif frac > 0.85:
                        mult *= rng.uniform(1.0, 1.25)
                    dur = round(target * mult, 1)
                    tool = s["tool"] or ""
                    if wrong_tool_on and s["id"] == wrong_tool_on:
                        tool = "screwdriver"
                    posture = round(rng.uniform(0.15, 0.55), 2)
                    if station == STRAIN_STATION and s["id"] in (3, 5) and rng.random() < 0.05:
                        posture = round(rng.uniform(0.72, 0.92), 2)
                        dur = max(dur, 6.0)
                    rows.append({
                        "unit_id": unit_id, "station": station, "worker": worker, "date": date,
                        "shift": shift, "start_ts": t_unit.isoformat(),
                        "end_ts": "",  # filled after the loop
                        "seq": seq, "subject": "both_hands" if s["action"] == "screw" else "right_hand",
                        "action": s["action"], "object": s.get("object") or "", "target": s.get("target") or "",
                        "tool": tool,
                        "t_start": round(t, 1), "t_end": round(t + dur, 1),
                        "posture_score": posture,
                        "clip_ref": f"clips/{date}/{station}/{unit_id}/{seq:02d}_{s['action']}.mp4",
                    })
                    t += dur + rng.uniform(0.5, 2.0)
                end_ts = (t_unit + timedelta(seconds=t)).isoformat()
                for r in rows[-len(seq_steps):]:
                    r["end_ts"] = end_ts
    return rows


FIELDS = ["unit_id", "station", "worker", "date", "shift", "start_ts", "end_ts",
          "seq", "subject", "action", "object", "target", "tool", "t_start", "t_end", "posture_score", "clip_ref"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "sample_manifest.csv"))
    ap.add_argument("--units-per-shift", type=int, default=16)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    rows = build(seed=a.seed, days=a.days, units_per_shift=a.units_per_shift)
    with open(a.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    units = len({r["unit_id"] for r in rows})
    print(f"wrote {len(rows)} action rows for {units} units to {a.out}")


if __name__ == "__main__":
    main()
