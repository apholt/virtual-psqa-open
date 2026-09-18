"""
migrate_add_position_mu.py -- add MU-weighted spot-position columns.

Why
---
beam_deliveries already stores pos_max_radial_mm and the tolerance pass rates,
but nothing records HOW MUCH DOSE landed out of tolerance. That distinction is
what separates a harmless aborted spot from a real positioning fault, and
without it services/evidence.py cannot tell them apart -- it currently
discards any row with pos_max_radial_mm > 10 mm as a
"spot-matching reconstruction artifact".

That assumption is now known to be wrong. On plan 8 fx 17 beam LP, one spot of
2814 was prescribed 0.0217 MU at (12.53, 3.44) and delivered 0.0020 MU at
(132.56, -154.91): the machine aborted the spot at ~9% of its meterset and
logged an off-field position. Layer counts matched the plan exactly and every
neighbouring spot was within 0.4 mm, so the pairing was correct -- the event
was real. It is currently filtered out as an artifact.

New columns
-----------
pos_n_beyond_05mm   count of matched spots with radial deviation > 0.5 mm
pos_n_beyond_20mm   count of matched spots with radial deviation > 2.0 mm
pos_mu_beyond_20mm  delivered MU in those spots, as a PERCENT of the beam's
                    total delivered MU

The last is the clinically meaningful one. For the event above it is about
0.0008% -- dosimetrically nothing, which is why gamma read 99.9%. A genuine
mispositioning of the same magnitude would carry normal MU and show percent-
level values.

Idempotent: existing columns are skipped. Backfill is NOT attempted; the new
values require per-spot data that is not stored, so they populate on the next
reconstruction. Re-run reprocess_log_qa.py to fill them for past fractions.

Run from the backend directory:
    ..\\python\\python.exe migrate_add_position_mu.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, ".")

NEW_COLUMNS = [
    ("pos_n_beyond_05mm", "INTEGER"),
    ("pos_n_beyond_20mm", "INTEGER"),
    ("pos_mu_beyond_20mm", "REAL"),
]


def _db_path() -> str:
    try:
        from database import engine
        url = str(engine.url)
        if url.startswith("sqlite"):
            p = url.split("///", 1)[-1]
            if os.path.exists(p):
                return p
    except Exception:  # noqa: BLE001
        pass
    for c in (os.environ.get("PSQA_DB", ""), os.path.join("data", "psqa.db")):
        if c and os.path.exists(c):
            return c
    return "./data/psqa.db"


def main() -> None:
    path = _db_path()
    if not os.path.exists(path):
        print(f"FAIL: database not found at {path}")
        sys.exit(1)
    print(f"Database: {path}")

    cx = sqlite3.connect(path)
    try:
        existing = {r[1] for r in cx.execute(
            "PRAGMA table_info(beam_deliveries)").fetchall()}
        if not existing:
            print("FAIL: beam_deliveries table not found.")
            sys.exit(1)

        added = 0
        for name, sqltype in NEW_COLUMNS:
            if name in existing:
                print(f"  present: {name}")
                continue
            cx.execute(
                f"ALTER TABLE beam_deliveries ADD COLUMN {name} {sqltype}")
            print(f"  added:   {name} {sqltype}")
            added += 1
        cx.commit()

        if added == 0:
            print("\nAlready applied. Nothing to do.")
            return

        n_rows = cx.execute("SELECT COUNT(*) FROM beam_deliveries").fetchone()[0]
        print(f"\nAdded {added} column(s). {n_rows} existing row(s) have NULL "
              f"for them.")
        print("These cannot be backfilled from stored data -- the values need "
              "per-spot offsets, which are not persisted.")
        print("Re-run reprocess_log_qa.py to populate them for past fractions.")
    except sqlite3.Error as exc:
        cx.rollback()
        print(f"FAIL: {exc}")
        sys.exit(1)
    finally:
        cx.close()


if __name__ == "__main__":
    main()
