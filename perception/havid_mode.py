"""Perception, HA-ViD mode: turn HA-ViD action segments into the manifest.

HA-ViD (https://iai-hrc.github.io/ha-vid) labels each action with subject,
action verb, manipulated object, target object and tool, with start and end
frames per video. This module converts a flat export of those segments into
the manifest the agent reads.

What you prepare (the authors' download format was not verified for this
repository, so the converter takes a neutral CSV rather than guessing):

1. segments CSV, one row per labeled action, columns:
       video_id, start_frame, end_frame, subject, verb, object, target, tool
   Extra columns are ignored. `null`/`none` values mean no object or tool.
2. plant context CSV, one row per recording, columns:
       video_id, worker, station, date, shift, start_time
   HA-ViD has no stations, shifts or dates. You assign them here, and the
   whitepaper states that they are assigned, not recorded.
3. optional vocabulary JSON mapping the dataset's label names to the names in
   your procedure file: {"verbs": {...}, "objects": {...}, "tools": {...}}.
   Verbs that are not in the procedure (for example idle or error markers)
   map to "" and are skipped.

HA-ViD is licensed CC BY-NC 4.0 (non-commercial). Check that your use fits
that license before downloading. This repository ships none of its data.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

from perception.labels_mode import norm

SEGMENT_FIELDS = ("video_id", "start_frame", "end_frame", "verb")
CONTEXT_FIELDS = ("video_id", "worker", "station", "date", "shift")


def _read(path: str | Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_vocab(path: str | Path | None) -> dict:
    v = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    return {k: {norm(a): norm(b) for a, b in (v.get(k) or {}).items()} for k in ("verbs", "objects", "tools")}


def segments_to_manifest(segments: list[dict], context: list[dict], fps: float = 30.0,
                         vocab: dict | None = None) -> tuple[list[dict], list[str]]:
    """Returns (manifest rows, problems). One recording becomes one unit."""
    vocab = vocab or {"verbs": {}, "objects": {}, "tools": {}}
    ctx = {c["video_id"]: c for c in context}
    problems: list[str] = []
    by_video: dict[str, list[dict]] = {}
    for i, s in enumerate(segments, start=2):
        if any(s.get(k) in (None, "") for k in SEGMENT_FIELDS):
            problems.append(f"segments line {i}: missing one of {SEGMENT_FIELDS}")
            continue
        if s["video_id"] not in ctx:
            problems.append(f"segments line {i}: video '{s['video_id']}' has no plant context row")
            continue
        by_video.setdefault(s["video_id"], []).append(s)

    rows: list[dict] = []
    for vid, segs in sorted(by_video.items()):
        c = ctx[vid]
        miss = [k for k in CONTEXT_FIELDS if not c.get(k)]
        if miss:
            problems.append(f"context for '{vid}': missing {miss}")
            continue
        segs.sort(key=lambda s: int(float(s["start_frame"])))
        f0 = int(float(segs[0]["start_frame"]))
        start = datetime.fromisoformat(f"{c['date']}T{c.get('start_time') or '06:00:00'}")
        last = max(int(float(s["end_frame"])) for s in segs)
        end = start + timedelta(seconds=(last - f0) / fps)
        unit_id = f"{c['station']}-{c['date']}-{c['worker']}-{norm(vid)}"
        seq = 0
        for s in segs:
            verb = norm(s["verb"])
            verb = vocab["verbs"].get(verb, verb)
            if not verb:
                continue
            obj, tgt, tool = norm(s.get("object")), norm(s.get("target")), norm(s.get("tool"))
            seq += 1
            a, b = int(float(s["start_frame"])), int(float(s["end_frame"]))
            rows.append({
                "unit_id": unit_id, "station": c["station"], "worker": c["worker"], "date": c["date"],
                "shift": c["shift"], "start_ts": start.isoformat(), "end_ts": end.isoformat(),
                "seq": seq, "subject": norm(s.get("subject")), "action": verb,
                "object": vocab["objects"].get(obj, obj), "target": vocab["objects"].get(tgt, tgt),
                "tool": vocab["tools"].get(tool, tool),
                "t_start": round((a - f0) / fps, 2), "t_end": round((b - f0) / fps, 2),
                "posture_score": "",  # HA-ViD action labels carry no posture score
                "clip_ref": f"clips/{vid}.mp4#t={round((a - f0) / fps, 1)},{round((b - f0) / fps, 1)}",
            })
    return rows, problems


def files_to_manifest(segments_csv: str | Path, context_csv: str | Path, fps: float = 30.0,
                      vocab_json: str | Path | None = None) -> tuple[list[dict], list[str]]:
    return segments_to_manifest(_read(segments_csv), _read(context_csv), fps, load_vocab(vocab_json))
