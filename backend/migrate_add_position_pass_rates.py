"""
migrate_add_position_pass_rates.py -- tolerance-based spot position pass rates.

Run once, from the backend directory:

    ..\\python\\python.exe migrate_add_position_pass_rates.py

WHY
---
The per-beam position summary currently stores signed MEANS (pos_mean_dx_mm,
pos_mean_dy_mm) and radial statistics. A signed mean hides a symmetric spread:
on RT Lung SBRT the vendor's report showed X-axis pass rates of 58-64% at
0.5 mm across all three fields, with individual spots out to 1.8-2.3 mm, while
pos_mean_dx_mm for the same deliveries reads about 0.05 mm. The deviations
cancel in the mean and disappear.

Gamma at 2%/2mm is insensitive to this -- every field passed, as did the
vendor's -- so it is not a QA failure. It is a machine characterisation that
the current metrics cannot express.

Tolerance-based pass rates are what the vendor reports, so storing them makes
per-fraction output directly comparable to a PRONOVA log-based QA report on
every fraction, rather than only when someone runs one by hand.

COLUMNS ADDED (all nullable; existing rows stay NULL until reprocessed)
    pos_x_pass_05mm, pos_x_pass_20mm    % of spots within tolerance in x
    pos_y_pass_05mm, pos_y_pass_20mm    ... in y
    pos_mag_pass_05mm, pos_mag_pass_20mm ... in radial magnitude
    pos_max_abs_dx_mm, pos_max_abs_dy_mm largest single-spot excursion
"""
from __future__ import annotations

import os
import sqlite3
import sys

DB = os.path.join("data", "psqa.db")

NEW_COLUMNS = [
    ("pos_x_pass_05mm", "REAL"),
    ("pos_x_pass_20mm", "REAL"),
    ("pos_y_pass_05mm", "REAL"),
    ("pos_y_pass_20mm", "REAL"),
    ("pos_mag_pass_05mm", "REAL"),
    ("pos_mag_pass_20mm", "REAL"),
    ("pos_max_abs_dx_mm", "REAL"),
    ("pos_max_abs_dy_mm", "REAL"),
]


def main() -> int:
    if not os.path.exists(DB):
        print(f"FAIL: {DB} not found. Run from the backend directory.")
        return 1

    conn = sqlite3.connect(DB)
    try:
        existing = {r[1] for r in conn.execute(
            "PRAGMA table_info(beam_deliveries)")}
        added = []
        for name, sqltype in NEW_COLUMNS:
            if name in existing:
                continue
            conn.execute(
                f"ALTER TABLE beam_deliveries ADD COLUMN {name} {sqltype}")
            added.append(name)
        conn.commit()

        if not added:
            print("Already migrated -- all position pass-rate columns exist.")
            return 0

        print(f"Added {len(added)} column(s): {', '.join(added)}")
        n = conn.execute("SELECT COUNT(*) FROM beam_deliveries").fetchone()[0]
        print(f"\n{n} existing beam delivery row(s) have NULL pass rates.")
        print("They are computed during reconstruction, so existing rows stay")
        print("NULL until their fractions are reprocessed. New deliveries get")
        print("them automatically.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
