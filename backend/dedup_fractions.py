"""
dedup_fractions.py -- collapse duplicate fractions rows to one per fraction.

Re-drops created extra fractions rows (old 'running' duplicates alongside the
new 'pass' row). This keeps, per (plan_id, fraction_number), the BEST row --
preferring a settled status (pass/measure_needed/failed) over 'running'/
'pending', and the highest id as a tiebreak -- and deletes the rest.

DRY-RUN by default. Run from backend\ :
    ..\python\python.exe dedup_fractions.py            (preview)
    ..\python\python.exe dedup_fractions.py --apply    (delete)
"""
import sqlite3
import sys

DB = "./data/psqa.db"
PLAN = 5
APPLY = "--apply" in sys.argv

# Lower number = better (kept). Settled outcomes beat in-progress placeholders.
_RANK = {
    "pass": 0, "measure_needed": 0, "failed": 0, "flagged": 0,
    "running": 5, "pending": 6,
}


def score(row):
    # prefer settled status, then higher id
    return (_RANK.get(row["qa_status"], 4), -row["id"])


def main():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    rows = cur.execute(
        "SELECT id, fraction_number, qa_status FROM fractions "
        "WHERE plan_id = ? ORDER BY fraction_number, id",
        (PLAN,),
    ).fetchall()

    by_fx = {}
    for r in rows:
        by_fx.setdefault(r["fraction_number"], []).append(r)

    keep_ids, drop_ids = [], []
    for fx, group in sorted(by_fx.items()):
        group_sorted = sorted(group, key=score)
        keep = group_sorted[0]
        keep_ids.append(keep["id"])
        print(f"fx {fx}: {len(group)} row(s) -> keep id={keep['id']} "
              f"({keep['qa_status']})")
        for r in group_sorted[1:]:
            drop_ids.append(r["id"])
            print(f"        drop id={r['id']} ({r['qa_status']})")

    print(f"\nKeep {len(keep_ids)}, drop {len(drop_ids)}.")

    if APPLY and drop_ids:
        cur.executemany("DELETE FROM fractions WHERE id = ?",
                        [(i,) for i in drop_ids])
        con.commit()
        print(f"DELETED {len(drop_ids)} duplicate row(s).")
    elif drop_ids:
        print("DRY-RUN: nothing deleted. Re-run with --apply.")
    else:
        print("No duplicates to remove.")

    con.close()


if __name__ == "__main__":
    main()
