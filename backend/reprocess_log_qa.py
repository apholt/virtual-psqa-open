"""
reprocess_log_qa.py  --  regenerate all log-QA results with the fixed resolver.

Every spot_stats_fx*.json written before the record-resolution fix is
contaminated: the old _find_plan_and_record() globbed the plan's store
directory and took the first RTRECORD it found, so every fraction of a plan was
reconstructed against the same arbitrary record. The signature is identical
per-beam statistics repeated across a plan's fractions (e.g. plan 5: 8
fractions, 1 distinct value for LA and RA).

This re-runs reconstruct_dose_from_log() for every fraction that has a record
on disk, using the corrected resolver, which rewrites both the JSON and the
beam_deliveries rows.

Run AFTER deploying the fixed services/log_reconstructor.py and AFTER
migrate_add_machine_beam.py.

Usage (from virtual-psqa\\backend):
    ..\\python\\python.exe reprocess_log_qa.py --dry-run
    ..\\python\\python.exe reprocess_log_qa.py
    ..\\python\\python.exe reprocess_log_qa.py --plan 5
    ..\\python\\python.exe reprocess_log_qa.py --purge-beam-rows
"""

import argparse
import logging
import os
import sqlite3
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("reprocess")

DB = r"data\psqa.db"


def get_session_factory():
    """Locate the app's SQLAlchemy session factory, whatever it is called."""
    candidates = [
        ("database", "SessionLocal"),
        ("database", "Session"),
        ("db", "SessionLocal"),
        ("core.database", "SessionLocal"),
        ("services.database", "SessionLocal"),
    ]
    errors = []
    for mod_name, attr in candidates:
        try:
            mod = __import__(mod_name, fromlist=[attr])
        except Exception as e:
            errors.append(f"{mod_name}: {e}")
            continue
        factory = getattr(mod, attr, None)
        if factory is not None:
            log.info(f"session factory: {mod_name}.{attr}")
            return factory
        errors.append(f"{mod_name}: no attribute {attr}")
    sys.exit("Could not locate a session factory. Tried:\n  "
             + "\n  ".join(errors))


def fractions_to_process(plan_filter):
    cx = sqlite3.connect(DB)
    q = ("SELECT id, plan_id, fraction_number, rtrecord_path FROM fractions "
         "WHERE rtrecord_path IS NOT NULL")
    if plan_filter:
        q += f" AND plan_id = {int(plan_filter)}"
    q += " ORDER BY plan_id, fraction_number"
    rows = cx.execute(q).fetchall()
    cx.close()
    return rows


def purge_beam_rows(plan_filter):
    cx = sqlite3.connect(DB, timeout=30.0)
    cx.execute("PRAGMA busy_timeout=30000")
    q = "DELETE FROM beam_deliveries"
    if plan_filter:
        q += f" WHERE plan_id = {int(plan_filter)}"
    n = cx.execute(q).rowcount
    cx.commit()
    cx.close()
    log.info(f"purged {n} beam_deliveries row(s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--purge-beam-rows", action="store_true",
                    help="delete beam_deliveries first, so stale beam names "
                         "from mis-resolved records do not survive")
    args = ap.parse_args()

    if not os.path.exists(DB):
        sys.exit(f"FATAL: {DB} not found - run from virtual-psqa\\backend")

    rows = fractions_to_process(args.plan)
    log.info(f"{len(rows)} fraction(s) with a record path")

    if args.dry_run:
        for fid, pid, fx, path in rows:
            exists = "ok " if os.path.exists(path) else "MISSING"
            log.info(f"  [{exists}] plan {pid} fx {fx} (fraction {fid})")
        log.info("dry run - nothing reprocessed")
        return

    if args.purge_beam_rows:
        purge_beam_rows(args.plan)

    from services.log_reconstructor import reconstruct_dose_from_log

    SessionLocal = get_session_factory()

    ok, incomplete, failed = 0, 0, []
    for fid, pid, fx, path in rows:
        if not os.path.exists(path):
            log.warning(f"plan {pid} fx {fx}: record missing on disk, skipped")
            failed.append((pid, fx, "record missing"))
            continue

        db = SessionLocal()
        try:
            reconstruct_dose_from_log(pid, fx, db)
            ok += 1
        except Exception as e:
            db.rollback()
            failed.append((pid, fx, str(e)[:120]))
            log.error(f"plan {pid} fx {fx}: {e}")
            if os.environ.get("REPROCESS_TRACE"):
                traceback.print_exc()
        finally:
            db.close()

    print("\n" + "=" * 66)
    print(f"reprocessed ok : {ok}")
    print(f"failed         : {len(failed)}")
    for pid, fx, err in failed:
        print(f"   plan {pid} fx {fx}: {err}")

    # ---- did the duplication go away? ----
    cx = sqlite3.connect(DB)
    print("\nDISTINCT-VALUE CHECK (n > 2; distinct == 1 means duplicated data)")
    print(f"  {'machine':<24}{'beam':<8}{'n':>5}{'distinct':>10}")
    for r in cx.execute(
        "SELECT machine, beam_name, COUNT(*), COUNT(DISTINCT mu_deviation_pct) "
        "FROM beam_deliveries WHERE record_incomplete=0 "
        "GROUP BY machine, beam_name HAVING COUNT(*) > 2 ORDER BY 4"
    ):
        flag = "   <<< duplicated" if r[3] == 1 else ""
        print(f"  {str(r[0]):<24}{str(r[1]):<8}{r[2]:>5}{r[3]:>10}{flag}")

    print("\nPER-BEAM MU DEVIATION BY MACHINE")
    print(f"  {'machine':<24}{'beam':<8}{'n':>5}{'mean%':>9}{'min%':>9}{'max%':>9}")
    for r in cx.execute(
        "SELECT machine, beam_name, COUNT(*), ROUND(AVG(mu_deviation_pct),3), "
        "ROUND(MIN(mu_deviation_pct),3), ROUND(MAX(mu_deviation_pct),3) "
        "FROM beam_deliveries WHERE record_incomplete=0 "
        "GROUP BY machine, beam_name ORDER BY 4 DESC"
    ):
        print(f"  {str(r[0]):<24}{str(r[1]):<8}{r[2]:>5}"
              f"{r[3]:>9}{r[4]:>9}{r[5]:>9}")
    cx.close()


if __name__ == "__main__":
    main()
