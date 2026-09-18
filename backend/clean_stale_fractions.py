"""
clean_stale_fractions.py (v2) -- clear the orphaned state for plan 5 so a
re-drop lands cleanly:
  * fraction rows stuck at fx=0 / 'running' (these block the main.py dedupe)
  * stale log_vs_Rx gamma rows collapsed to fx=1 (phantom fraction in trend)
KEEPS the legitimate mcSquare_vs_TPS gamma rows untouched.

Run from backend\ . DRY-RUN by default; pass --apply to delete.
    ..\python\python.exe clean_stale_fractions.py
    ..\python\python.exe clean_stale_fractions.py --apply
"""
import sqlite3
import sys

DB = "./data/psqa.db"
PLAN = 5
APPLY = "--apply" in sys.argv


def main():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # 1) orphaned fraction rows (never completed) -> block the re-drop dedupe
    stale = cur.execute(
        "SELECT id, plan_id, fraction_number, rtrecord_uid, qa_status "
        "FROM fractions WHERE fraction_number = 0 AND qa_status = 'running' "
        "ORDER BY id"
    ).fetchall()
    print(f"[fractions] stale rows (fx=0, running): {len(stale)}")
    for r in stale:
        uid = r["rtrecord_uid"] or ""
        print(f"    id={r['id']:>2}  plan={r['plan_id']}  uid=...{uid[-24:]}")
    uids = sorted({r["rtrecord_uid"] for r in stale if r["rtrecord_uid"]})
    print(f"    distinct record UIDs: {len(uids)}")

    # 2) stale log gamma rows collapsed to fx=1
    log_rows = cur.execute(
        "SELECT id, fraction_number, field_name, comparison_type, passing_rate "
        "FROM gamma_results WHERE plan_id = ? AND comparison_type = 'log_vs_Rx' "
        "ORDER BY id",
        (PLAN,),
    ).fetchall()
    print(f"\n[gamma_results] stale log_vs_Rx rows for plan {PLAN}: "
          f"{len(log_rows)}")
    for r in log_rows:
        print(f"    id={r['id']}  fx={r['fraction_number']}  "
              f"field={r['field_name']}  rate={r['passing_rate']}")

    # 3) what we explicitly KEEP
    keep = cur.execute(
        "SELECT id, field_name, comparison_type, passing_rate "
        "FROM gamma_results WHERE plan_id = ? AND comparison_type != 'log_vs_Rx' "
        "ORDER BY id",
        (PLAN,),
    ).fetchall()
    print(f"\n[gamma_results] KEEPING {len(keep)} non-log row(s) for plan "
          f"{PLAN}:")
    for r in keep:
        print(f"    id={r['id']}  field={r['field_name']}  "
              f"{r['comparison_type']}  rate={r['passing_rate']}")

    # --- delete or dry-run ---
    if APPLY:
        if stale:
            cur.executemany("DELETE FROM fractions WHERE id = ?",
                            [(r["id"],) for r in stale])
        if log_rows:
            cur.executemany("DELETE FROM gamma_results WHERE id = ?",
                            [(r["id"],) for r in log_rows])
        con.commit()
        print(f"\nDELETED {len(stale)} fraction row(s) and {len(log_rows)} "
              f"log_vs_Rx gamma row(s). KEPT {len(keep)} non-log gamma row(s).")
    else:
        print("\nDRY-RUN: nothing deleted. Re-run with --apply to remove the "
              "[fractions] rows and the [gamma_results] log_vs_Rx rows above. "
              "The KEEPING list is left untouched.")

    con.close()


if __name__ == "__main__":
    main()