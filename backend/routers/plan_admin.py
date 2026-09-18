"""
Plan administration endpoints -- delete/cleanup.

DELETE /api/plans/{plan_id}
    Removes a plan and everything hanging off it: QAJob, GammaResult,
    MLPrediction, GammaPrediction, Fraction rows, and the results folder
    data\\results\\plan_<id>\\.

    Guards:
      - 404 if the plan does not exist
      - 409 if any job for the plan is running/queued (cancel it first --
        deleting a plan out from under an active MCsquare run would strand
        the worker and corrupt job state)

    Deliberately NOT touched: the patient row (other plans may reference it)
    and the DICOM store folder (source data).

    BEAM DELIVERIES ARE RETAINED, NOT DELETED.
    -----------------------------------------
    A beam-delivery row records what the machine did on a given date. Its QA
    value is as machine history: it feeds the room systematic model, room-level
    SPC, and the machine-state layer of the delivery gate, all of which pool
    across patients. Deleting one patient's 30 fractions can therefore change
    the evidence used to clear a DIFFERENT patient -- silently, and with the
    plan-17 style excursions that justify per-fraction verification being
    exactly what disappears.

    So the rows stay, with plan_id negated and retained_at set. That breaks
    the patient linkage, makes every plan-scoped query (plan_id = N) miss them
    automatically, and prevents id reuse from re-attaching them: plans.id is
    INTEGER PRIMARY KEY without AUTOINCREMENT, so SQLite hands the id of a
    deleted plan to the next one ingested.

    Run migrate_retain_beam_deliveries.py once before relying on this.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models.gamma_result import GammaResult
from models.plan import Plan
from models.qa_job import QAJob

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/plans", tags=["plan-admin"])


@router.delete("/{plan_id}")
async def delete_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    active = (
        db.query(QAJob)
        .filter(QAJob.plan_id == plan_id, QAJob.status.in_(["running", "queued"]))
        .count()
    )
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"{active} job(s) still running/queued for this plan. "
                   "Cancel them before deleting.",
        )

    deleted = {
        "gamma_results": db.query(GammaResult)
        .filter_by(plan_id=plan_id).delete(synchronize_session=False),
        "qa_jobs": db.query(QAJob)
        .filter_by(plan_id=plan_id).delete(synchronize_session=False),
    }
    # Plan-scoped predictions carry no machine-history value on their own --
    # they describe THIS plan's expected delivery -- so they go with the plan.
    # Left behind they would also be inherited by the next plan to reuse the id.
    try:
        deleted["gamma_predictions"] = db.execute(
            text("DELETE FROM gamma_predictions WHERE plan_id = :pid"),
            {"pid": plan_id},
        ).rowcount
    except Exception:  # table may not exist yet
        pass

    # Optional models -- present in some schema versions only.
    try:
        from models.ml_prediction import MLPrediction
        deleted["ml_predictions"] = (
            db.query(MLPrediction)
            .filter_by(plan_id=plan_id).delete(synchronize_session=False))
    except Exception:
        pass
    try:
        from models.fraction import Fraction
        deleted["fractions"] = (
            db.query(Fraction)
            .filter_by(plan_id=plan_id).delete(synchronize_session=False))
    except Exception:
        pass

    # Retain the delivery records as machine history. See the module docstring.
    retained = 0
    try:
        cols = [r[1] for r in db.execute(
            text("PRAGMA table_info(beam_deliveries)")).fetchall()]
        if "retained_at" in cols:
            retained = db.execute(
                text("UPDATE beam_deliveries "
                     "SET retained_at = datetime('now'), plan_id = -:pid "
                     "WHERE plan_id = :pid"),
                {"pid": plan_id},
            ).rowcount
        else:
            # Migration has not been run. Negate anyway so id reuse cannot
            # re-attach these rows to a future plan; the timestamp is the only
            # thing lost.
            retained = db.execute(
                text("UPDATE beam_deliveries SET plan_id = -:pid "
                     "WHERE plan_id = :pid"),
                {"pid": plan_id},
            ).rowcount
            logger.warning(
                "beam_deliveries.retained_at missing -- run "
                "migrate_retain_beam_deliveries.py. Rows were still detached.")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Could not retain beam deliveries for plan {plan_id}: "
                     f"{exc}")

    plan_name = getattr(plan, "plan_name", str(plan_id))
    db.delete(plan)
    db.commit()

    results_dir = Path(settings.RESULTS_PATH) / f"plan_{plan_id}"
    removed_files = False
    if results_dir.exists():
        shutil.rmtree(results_dir, ignore_errors=True)
        removed_files = True

    logger.info(
        f"Deleted plan {plan_id} ('{plan_name}'): rows={deleted} "
        f"beam_deliveries_retained={retained} "
        f"results_dir_removed={removed_files}"
    )
    return {
        "plan_id": plan_id,
        "plan_name": plan_name,
        "deleted_rows": deleted,
        "beam_deliveries_retained": retained,
        "results_dir_removed": removed_files,
        "note": (
            f"{retained} beam delivery record(s) retained as machine history "
            "with the patient linkage removed. They continue to contribute to "
            "room-level control charts and the machine systematic model, and "
            "no longer appear under any plan."
        ) if retained else None,
    }
