"""
fix_stale_jobs.py -- run from backend\ with the backend service running or not:
    ..\python\python.exe fix_stale_jobs.py

Marks orphaned QA jobs (stuck in 'running' or 'queued' after a service
restart killed their background thread) as error, so dashboard counters and
plan statuses reflect reality. Only touches jobs, never results.

IMPORTANT: only run this when you know nothing is actually running (no
MCsquare exe in Task Manager, no job progressing in the UI).
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from database import SessionLocal
from models.qa_job import QAJob
from models.plan import Plan


def main() -> None:
    db = SessionLocal()
    try:
        stale = (
            db.query(QAJob)
            .filter(QAJob.status.in_(["running", "queued"]))
            .all()
        )
        if not stale:
            print("No stale jobs found -- nothing to do.")
            return
        for j in stale:
            print(f"  job {j.id}: plan={j.plan_id} type={j.job_type} "
                  f"status={j.status} started={j.started_at} -> error (orphaned)")
            j.status = "error"
            j.error_message = "Orphaned: backend restarted while job was active"
            j.completed_at = datetime.utcnow()
            # If the plan is still shown as 'running', put it back to pending
            # so it can be re-queued.
            plan = db.query(Plan).filter_by(id=j.plan_id).first()
            if plan and plan.qa_status == "running":
                plan.qa_status = "pending"
                print(f"    plan {j.plan_id}: qa_status running -> pending")
        db.commit()
        print(f"Marked {len(stale)} stale job(s) as error.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
