"""Findings store: Amazon S3 in the deployed stack, a local folder in tests.

Layout (same keys in both backends):
    actions/actions.csv          perception output, one row per action
    findings/findings.csv        one row per unit
    deviations/deviations.csv    one row per deviation
    state/processed_units.json   unit ids already analyzed
    procedure/<file>.json        optional override of the bundled procedure
    planning/robot_capability.json  optional override of the robot capability table
    clips/...                    evidence clips (optional)
"""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

ACTIONS_KEY = "actions/actions.csv"
FINDINGS_KEY = "findings/findings.csv"
DEVIATIONS_KEY = "deviations/deviations.csv"
STATE_KEY = "state/processed_units.json"
PROCEDURE_PREFIX = "procedure/"

ACTION_FIELDS = [
    "unit_id", "station", "worker", "date", "shift", "start_ts", "end_ts",
    "seq", "subject", "action", "object", "target", "tool", "t_start", "t_end",
    "posture_score", "clip_ref",
]
FINDING_FIELDS = [
    "unit_id", "station", "worker", "date", "shift", "start_ts", "end_ts",
    "cycle_seconds", "steps_matched", "steps_total", "deviation_count",
    "high_count", "medium_count", "low_count", "compliant", "procedure_id",
]
DEVIATION_FIELDS = [
    "deviation_id", "unit_id", "station", "worker", "date", "shift", "step_id",
    "step_name", "type", "severity", "detail", "observed_seconds",
    "target_seconds", "clip_ref", "ts",
]


class Store:
    def __init__(self, bucket: str | None = None, data_dir: str | None = None, prefix: str = ""):
        self.bucket = bucket or os.environ.get("FINDINGS_BUCKET") or None
        self.prefix = prefix or os.environ.get("FINDINGS_PREFIX", "")
        self.data_dir = Path(data_dir or os.environ.get("DATA_DIR", "./data-local"))
        self._s3 = None
        if self.bucket:
            import boto3  # imported lazily so tests run without AWS SDK config
            self._s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION"))
        else:
            self.data_dir.mkdir(parents=True, exist_ok=True)

    # ---- primitives -------------------------------------------------
    def _k(self, key: str) -> str:
        return f"{self.prefix}{key}" if self.prefix else key

    def exists(self, key: str) -> bool:
        if self._s3:
            try:
                self._s3.head_object(Bucket=self.bucket, Key=self._k(key))
                return True
            except Exception:
                return False
        return (self.data_dir / key).exists()

    def read_text(self, key: str) -> str | None:
        if self._s3:
            try:
                obj = self._s3.get_object(Bucket=self.bucket, Key=self._k(key))
                return obj["Body"].read().decode("utf-8")
            except self._s3.exceptions.NoSuchKey:
                return None
            except Exception as exc:  # pragma: no cover - network path
                if "NoSuchKey" in str(exc) or "404" in str(exc):
                    return None
                raise
        p = self.data_dir / key
        return p.read_text(encoding="utf-8") if p.exists() else None

    def write_text(self, key: str, text: str, content_type: str = "text/plain") -> None:
        if self._s3:
            self._s3.put_object(Bucket=self.bucket, Key=self._k(key),
                                Body=text.encode("utf-8"), ContentType=content_type)
            return
        p = self.data_dir / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def list_keys(self, prefix: str) -> list[str]:
        if self._s3:
            keys, token = [], None
            while True:
                kw = {"Bucket": self.bucket, "Prefix": self._k(prefix)}
                if token:
                    kw["ContinuationToken"] = token
                resp = self._s3.list_objects_v2(**kw)
                keys += [o["Key"][len(self.prefix):] if self.prefix else o["Key"]
                         for o in resp.get("Contents", [])]
                token = resp.get("NextContinuationToken")
                if not token:
                    return keys
        base = self.data_dir / prefix
        if not base.exists():
            return []
        return [str(p.relative_to(self.data_dir)).replace(os.sep, "/")
                for p in base.rglob("*") if p.is_file()]

    def presign(self, key: str, expires: int = 900) -> str:
        if self._s3:
            return self._s3.generate_presigned_url(
                "get_object", Params={"Bucket": self.bucket, "Key": self._k(key)}, ExpiresIn=expires)
        return str((self.data_dir / key).resolve())

    # ---- csv / json helpers -------------------------------------------
    def read_csv(self, key: str) -> list[dict]:
        text = self.read_text(key)
        if not text:
            return []
        return list(csv.DictReader(io.StringIO(text)))

    def write_csv(self, key: str, rows: list[dict], fieldnames: list[str]) -> None:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
        self.write_text(key, buf.getvalue(), "text/csv")

    def append_csv(self, key: str, rows: list[dict], fieldnames: list[str], dedupe_on: str | None = None) -> int:
        existing = self.read_csv(key)
        if dedupe_on:
            seen = {r[dedupe_on] for r in existing}
            rows = [r for r in rows if str(r.get(dedupe_on)) not in seen]
        self.write_csv(key, existing + rows, fieldnames)
        return len(rows)

    def read_json(self, key: str, default=None):
        text = self.read_text(key)
        return json.loads(text) if text else default

    def write_json(self, key: str, obj) -> None:
        self.write_text(key, json.dumps(obj, indent=2, default=str), "application/json")

    # ---- domain helpers ----------------------------------------------
    def processed_units(self) -> set[str]:
        return set(self.read_json(STATE_KEY, default=[]))

    def mark_processed(self, unit_ids: list[str]) -> None:
        self.write_json(STATE_KEY, sorted(self.processed_units() | set(unit_ids)))

    def procedure_override(self) -> dict | None:
        keys = [k for k in self.list_keys(PROCEDURE_PREFIX) if k.endswith(".json")]
        if not keys:
            return None
        return self.read_json(sorted(keys)[0])

    def planning_override(self) -> dict | None:
        return self.read_json("planning/robot_capability.json")
