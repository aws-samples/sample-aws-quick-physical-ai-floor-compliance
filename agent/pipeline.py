"""Glue between perception output, the rules engine and the store."""
from __future__ import annotations

from collections import defaultdict

from . import rules
from .store import (ACTION_FIELDS, ACTIONS_KEY, DEVIATION_FIELDS, DEVIATIONS_KEY,
                    FINDING_FIELDS, FINDINGS_KEY, Store)


def load_procedure(store: Store) -> dict:
    override = store.procedure_override()
    if override:
        rules.validate_procedure(override)
        return override
    return rules.load_procedure()


def ingest_actions(store: Store, actions: list[dict]) -> int:
    """Append action rows to actions/actions.csv, skipping rows already present."""
    existing = store.read_csv(ACTIONS_KEY)
    seen = {(r["unit_id"], str(r["seq"])) for r in existing}
    new = [a for a in actions if (a["unit_id"], str(a["seq"])) not in seen]
    store.write_csv(ACTIONS_KEY, existing + new, ACTION_FIELDS)
    return len(new)


def analyze(store: Store, only_new: bool = True) -> dict:
    """Run the rules over units in the actions table and write findings.

    Fatigue indicators are computed within the batch being analyzed. Load a
    whole shift (or day) per batch so late-shift patterns are visible.
    """
    proc = load_procedure(store)
    actions = store.read_csv(ACTIONS_KEY)
    done = store.processed_units() if only_new else set()
    by_unit: dict[str, list[dict]] = defaultdict(list)
    for a in actions:
        if a["unit_id"] not in done:
            by_unit[a["unit_id"]].append(a)
    if not by_unit:
        return {"units_analyzed": 0, "deviations": 0, "procedure_id": proc["procedure_id"]}

    units = []
    for uid, rows in by_unit.items():
        first = rows[0]
        units.append({
            "unit_id": uid, "station": first["station"], "worker": first["worker"],
            "date": first["date"], "shift": first["shift"],
            "start_ts": first.get("start_ts", ""),
            "end_ts": max(r.get("end_ts", "") for r in rows),
        })
    findings = rules.evaluate_units(units, by_unit, proc)

    f_rows = [f.to_row() for f in findings]
    d_rows = [d.to_row() for f in findings for d in f.deviations]
    if only_new:
        store.append_csv(FINDINGS_KEY, f_rows, FINDING_FIELDS, dedupe_on="unit_id")
        store.append_csv(DEVIATIONS_KEY, d_rows, DEVIATION_FIELDS, dedupe_on="deviation_id")
    else:
        store.write_csv(FINDINGS_KEY, f_rows, FINDING_FIELDS)
        store.write_csv(DEVIATIONS_KEY, d_rows, DEVIATION_FIELDS)
    store.mark_processed([u["unit_id"] for u in units])
    return {
        "units_analyzed": len(units),
        "deviations": len(d_rows),
        "compliance_pct": rules.compliance_score(findings),
        "procedure_id": proc["procedure_id"],
    }
