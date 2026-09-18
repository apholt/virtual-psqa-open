"""
migrate_add_machine_beam.py  --  make delivery data queryable.

Adds two things the analysis has repeatedly needed and had to reconstruct by
globbing JSON:

  1. fractions.machine   -- which room delivered this fraction. Already present
                            in the RT Ion Record and in spot_stats_fx*.json,
                            but not queryable. Room turned out to be a first-
                            order variable in delivery deviation, so it needs
                            to be a column.

  2. beam_deliveries     -- one row per beam per delivered fraction, carrying
                            the per-beam MU deviation and spot position/MU
                            error statistics. This is where beam-level
                            systematics live (e.g. RP running +1.2 to +2.2% on
                            GR1 while LP and PA sit near zero in the same
                            sessions). Currently that only exists inside
                            per-fraction JSON files.

Both are backfilled from the spot_stats_fx*.json files already on disk, so
existing deliveries become queryable immediately.

Idempotent: safe to run more than once. Existing rows are replaced, not
duplicated.

Usage (from virtual-psqa\\backend):
    ..\\python\\python.exe migrate_add_machine_beam.py
    ..\\python\\python.exe migrate_add_machine_beam.py --dry-run
"""

import argparse
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import sys

DB = r"data\psqa.db"
RESULTS_GLOB = r"data\results\plan_*\log_output\spot_stats_fx*.json"

BEAM_TABLE = "beam_deliveries"

DDL = f"""
CREATE TABLE IF NOT EXISTS {BEAM_TABLE} (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id               INTEGER NOT NULL,
    fraction_number       INTEGER NOT NULL,
    fraction_id           INTEGER,
    machine               TEXT,
    treatment_date        TEXT,
    beam_name             TEXT NOT NULL,
    n_spots_prescribed    INTEGER,
    n_spots_delivered     INTEGER,
    n_spots_matched       INTEGER,
    mu_prescribed         REAL,
    mu_delivered          REAL,
    mu_deviation_pct      REAL,
    mu_err_mean_abs_pct   REAL,
    mu_err_max_abs_pct    REAL,
    pos_mean_dx_mm        REAL,
    pos_mean_dy_mm        REAL,
    pos_mean_radial_mm    REAL,
    pos_p95_radial_mm     REAL,
    pos_max_radial_mm     REAL,
    gamma_passing_rate    REAL,
    not_scored            TEXT,
    record_incomplete     INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL,
    UNIQUE (plan_id, fraction_number, beam_name)
);
"""

IDX = [
    f"CREATE INDEX IF NOT EXISTS ix_{BEAM_TABLE}_plan ON {BEAM_TABLE} (plan_id);",
    f"CREATE INDEX IF NOT EXISTS ix_{BEAM_TABLE}_machine ON {BEAM_TABLE} (machine);",
    f"CREATE INDEX IF NOT EXISTS ix_{BEAM_TABLE}_beam ON {BEAM_TABLE} (beam_name);",
]

FIELDS = [
    "n_spots_prescribed", "n_spots_delivered", "n_spots_matched",
    "mu_prescribed", "mu_delivered", "mu_deviation_pct",
    "mu_err_mean_abs_pct", "mu_err_max_abs_pct",
    "pos_mean_dx_mm", "pos_mean_dy_mm", "pos_mean_radial_mm",
    "pos_p95_radial_mm", "pos_max_radial_mm", "gamma_passing_rate",
]


def has_column(cx, table, col):
    return any(r[1] == col for r in cx.execute(f'PRAGMA table_info("{table}")'))


def add_machine_column(cx, dry):
    if has_column(cx, "fractions", "machine"):
        print("  fractions.machine: already present")
        return False
    print("  fractions.machine: ADDING")
    if not dry:
        cx.execute("ALTER TABLE fractions ADD COLUMN machine TEXT")
        cx.commit()
    return True


def create_beam_table(cx, dry):
    existed = cx.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (BEAM_TABLE,),
    ).fetchone()[0] > 0
    print(f"  {BEAM_TABLE}: {'already present' if existed else 'CREATING'}")
    if not dry:
        cx.execute(DDL)
        for s in IDX:
            cx.execute(s)
        cx.commit()
    return not existed


def parse_stats_path(path):
    """(plan_id, fraction_number) from .../plan_19/log_output/spot_stats_fx7.json"""
    m_plan = re.search(r"plan_(\d+)", path)
    m_fx = re.search(r"spot_stats_fx(\d+)\.json$", path)
    if not (m_plan and m_fx):
        return None, None
    return int(m_plan.group(1)), int(m_fx.group(1))


def fraction_id_for(cx, plan_id, fx):
    r = cx.execute(
        "SELECT id FROM fractions WHERE plan_id=? AND fraction_number=? "
        "ORDER BY id DESC LIMIT 1", (plan_id, fx)
    ).fetchone()
    return r[0] if r else None


def backfill(cx, dry):
    files = sorted(glob.glob(RESULTS_GLOB))
    print(f"\n  found {len(files)} spot_stats file(s)")
    now = dt.datetime.now().isoformat(timespec="seconds")
    n_frac, n_beam, n_skip = 0, 0, 0

    for path in files:
        plan_id, fx = parse_stats_path(path)
        if plan_id is None:
            n_skip += 1
            continue
        try:
            payload = json.load(open(path))
        except Exception as e:
            print(f"    unreadable {path}: {e}")
            n_skip += 1
            continue

        machine = payload.get("machine")
        tdate = payload.get("treatment_date")
        incomplete = 1 if payload.get("record_incomplete") else 0
        fid = fraction_id_for(cx, plan_id, fx)

        if machine and fid is not None and not dry:
            cx.execute("UPDATE fractions SET machine=? WHERE id=?",
                       (machine, fid))
        if machine and fid is not None:
            n_frac += 1

        for b in payload.get("beams", []) or []:
            name = b.get("beam_name")
            if not name:
                continue
            vals = [b.get(k) for k in FIELDS]
            if not dry:
                cx.execute(
                    f"INSERT OR REPLACE INTO {BEAM_TABLE} "
                    "(plan_id, fraction_number, fraction_id, machine, "
                    " treatment_date, beam_name, "
                    + ", ".join(FIELDS) +
                    ", not_scored, record_incomplete, created_at) "
                    "VALUES (?,?,?,?,?,?," + ",".join("?" * len(FIELDS)) +
                    ",?,?,?)",
                    [plan_id, fx, fid, machine, tdate, name] + vals
                    + [b.get("not_scored"), incomplete, now],
                )
            n_beam += 1

    if not dry:
        cx.commit()
    print(f"  fractions updated with machine : {n_frac}")
    print(f"  beam rows written              : {n_beam}")
    if n_skip:
        print(f"  files skipped                  : {n_skip}")


def summary(cx):
    print("\n" + "=" * 66)
    print("RESULT")
    print("=" * 66)
    try:
        rows = cx.execute(
            "SELECT machine, COUNT(*) FROM fractions GROUP BY machine"
        ).fetchall()
        print("  fractions by machine:")
        for m, n in rows:
            print(f"    {str(m):<28}{n}")
    except sqlite3.Error as e:
        print(f"  fractions: {e}")

    try:
        rows = cx.execute(
            f"SELECT machine, beam_name, COUNT(*), "
            f"ROUND(AVG(mu_deviation_pct),3), ROUND(MAX(mu_deviation_pct),3) "
            f"FROM {BEAM_TABLE} WHERE record_incomplete=0 "
            f"GROUP BY machine, beam_name ORDER BY machine, beam_name"
        ).fetchall()
        print(f"\n  per-beam MU deviation by machine "
              f"(the RP/GR1 question, now one query):")
        print(f"    {'machine':<24}{'beam':<8}{'n':>5}{'mean%':>9}{'max%':>9}")
        for m, b, n, avg, mx in rows:
            print(f"    {str(m):<24}{str(b):<8}{n:>5}"
                  f"{(avg if avg is not None else 0):>9.3f}"
                  f"{(mx if mx is not None else 0):>9.3f}")
    except sqlite3.Error as e:
        print(f"  {BEAM_TABLE}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(DB):
        sys.exit(f"FATAL: {DB} not found - run from virtual-psqa\\backend")

    cx = sqlite3.connect(DB, timeout=30.0)
    cx.execute("PRAGMA busy_timeout=30000")

    print(f"DB: {os.path.abspath(DB)}")
    if args.dry_run:
        print("DRY RUN - no changes will be written\n")

    print("schema:")
    add_machine_column(cx, args.dry_run)
    create_beam_table(cx, args.dry_run)

    print("\nbackfill from spot_stats JSON:")
    backfill(cx, args.dry_run)

    if not args.dry_run:
        summary(cx)
    cx.close()


if __name__ == "__main__":
    main()
