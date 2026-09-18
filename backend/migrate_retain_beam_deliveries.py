"""
migrate_retain_beam_deliveries.py -- retain machine history across plan delete.

Run once, from the backend directory:

    ..\\python\\python.exe migrate_retain_beam_deliveries.py

WHY
---
A beam-delivery row records what the scanning magnets and dose monitors did on
a given date. That it happened during a particular patient's treatment is
incidental to its QA value: it is the evidence behind the room systematic
model, room-level SPC, and the machine-state layer of the delivery gate.
Deleting a patient should not erase the machine's history any more than
deleting a chart should erase that morning's output measurements.

It also cannot be allowed to erase it silently. Room-level SPC decides
machine_state for EVERY plan in a room, so removing one patient's 30 fractions
can change the evidence used to clear a different patient, with no trace.

WHAT THIS ADDS
--------------
  retained_at  -- timestamp set when the owning plan was deleted. NULL for
                  rows whose plan still exists.

Deletion then NEGATES plan_id rather than removing the row. That does three
things at once:
  * the patient linkage is broken (no positive plan id resolves to it),
  * every plan-scoped query (plan_id = N) stops matching it automatically,
  * and it can never be re-associated by id reuse.

The last point matters: plans.id is INTEGER PRIMARY KEY without AUTOINCREMENT,
so SQLite reuses the ids of deleted rows. Without this, deleting plan 20 and
ingesting a new plan would silently attach the old patient's deliveries to the
new one.
"""
from __future__ import annotations

import os
import sqlite3
import sys

DB = os.path.join("data", "psqa.db")


def main() -> int:
    if not os.path.exists(DB):
        print(f"FAIL: {DB} not found. Run from the backend directory.")
        return 1

    conn = sqlite3.connect(DB)
    try:
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(beam_deliveries)")]
        if "retained_at" in cols:
            print("Already migrated (beam_deliveries.retained_at exists).")
            return 0

        conn.execute("ALTER TABLE beam_deliveries ADD COLUMN retained_at TEXT")
        conn.commit()
        print("Added beam_deliveries.retained_at")

        n = conn.execute("SELECT COUNT(*) FROM beam_deliveries").fetchone()[0]
        orphan = conn.execute(
            "SELECT COUNT(*) FROM beam_deliveries "
            "WHERE plan_id NOT IN (SELECT id FROM plans)").fetchone()[0]
        print(f"{n} beam delivery row(s), {orphan} not matching a live plan.")
        if orphan:
            print("Marking those as retained (their plan was deleted before "
                  "this migration).")
            conn.execute(
                "UPDATE beam_deliveries "
                "SET retained_at = datetime('now'), "
                "    plan_id = -ABS(plan_id) "
                "WHERE plan_id NOT IN (SELECT id FROM plans) AND plan_id > 0")
            conn.commit()
    finally:
        conn.close()

    print("\nDone. Deleting a plan will now retain its delivery records as "
          "machine history,\nwith the patient linkage broken.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
