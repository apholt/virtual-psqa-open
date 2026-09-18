"""
delete_plan.py -- remove a plan (and everything hanging off it) from VPSQA.

Usage (from backend\):
    ..\python\python.exe delete_plan.py <plan_id>            (dry run: shows what would go)
    ..\python\python.exe delete_plan.py <plan_id> --confirm  (actually deletes)

Deletes: QAJob, GammaResult, MLPrediction, Fraction rows for the plan, the
plan row itself, and the results folder data\results\plan_<id>\.
Leaves alone: the patient row (other plans may reference it) and the DICOM
store folder (source data -- delete manually if truly unwanted).
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from database import SessionLocal
from models.plan import Plan
from models.qa_job import QAJob
from models.gamma_result import GammaResult

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(1)

PLAN_ID = int(sys.argv[1])
CONFIRM = "--confirm" in sys.argv


def main() -> None:
    db = SessionLocal()
    try:
        plan = db.query(Plan).filter_by(id=PLAN_ID).first()
        if plan is None:
            print(f"Plan {PLAN_ID} not found.")
            return

        print(f"Plan {PLAN_ID}: '{getattr(plan, 'plan_name', '?')}' "
              f"patient_id={getattr(plan, 'patient_id', '?')} "
              f"qa_status={plan.qa_status}")

        counts = {
            "qa_jobs": db.query(QAJob).filter_by(plan_id=PLAN_ID).count(),
            "gamma_results": db.query(GammaResult).filter_by(plan_id=PLAN_ID).count(),
        }
        # Optional models -- present in some schema versions only.
        try:
            from models.ml_prediction import MLPrediction
            counts["ml_predictions"] = (
                db.query(MLPrediction).filter_by(plan_id=PLAN_ID).count())
        except Exception:
            MLPrediction = None
        try:
            from models.fraction import Fraction
            counts["fractions"] = (
                db.query(Fraction).filter_by(plan_id=PLAN_ID).count())
        except Exception:
            Fraction = None

        results_dir = Path(settings.RESULTS_PATH) / f"plan_{PLAN_ID}"
        for k, v in counts.items():
            print(f"  {k}: {v} row(s)")
        print(f"  results folder: {results_dir} "
              f"({'exists' if results_dir.exists() else 'absent'})")
        print(f"  DICOM store (NOT touched): {plan.dicom_store_path}")

        if not CONFIRM:
            print("\nDry run only. Re-run with --confirm to delete.")
            return

        db.query(GammaResult).filter_by(plan_id=PLAN_ID).delete(
            synchronize_session=False)
        db.query(QAJob).filter_by(plan_id=PLAN_ID).delete(
            synchronize_session=False)
        if MLPrediction is not None:
            db.query(MLPrediction).filter_by(plan_id=PLAN_ID).delete(
                synchronize_session=False)
        if Fraction is not None:
            db.query(Fraction).filter_by(plan_id=PLAN_ID).delete(
                synchronize_session=False)
        db.delete(plan)
        db.commit()

        if results_dir.exists():
            shutil.rmtree(results_dir, ignore_errors=True)
            print(f"Removed {results_dir}")

        print(f"Plan {PLAN_ID} deleted. Refresh the dashboard.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
