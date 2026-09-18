"""
QA job orchestration.
run_qa_job() is dispatched on a background thread (FastAPI BackgroundTasks).
It opens its own DB session, transitions the job through running -> complete/error,
and honours cancellation requests. On completion it asks the gate to re-derive
plan.qa_status (GATE_VERDICT_V1); it writes 'running' and 'failed' itself, as
those describe job state rather than a clinical verdict.
"""
from __future__ import annotations
import logging
from datetime import datetime
from pathlib import Path
from config import settings
from database import SessionLocal
from models.plan import Plan
from models.qa_job import QAJob
from services.job_control import JobCancelled, clear, is_cancelled
logger = logging.getLogger(__name__)
# Job types handled in Phases 2-3 + Adaptive Synthetic CT.
SUPPORTED_JOB_TYPES = {
    "mcSquare",
    "log_reconstruction",
    "gamma",
    "synthetic_ct_mcSquare",
    "synthetic_ct_generate",
    "synthetic_ct_full",
    "synthetic_qact",
}



def _set_plan_status(db, plan_id: int, status: str) -> None:
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan:
        plan.qa_status = status
        db.commit()


def run_qa_job(job_id: int) -> None:
    """Entry point for a background QA job. Manages its own DB session."""
    db = SessionLocal()
    try:
        job = db.query(QAJob).filter_by(id=job_id).first()
        if job is None:
            logger.error(f"run_qa_job: job {job_id} not found")
            return
        if job.job_type not in SUPPORTED_JOB_TYPES:
            job.status = "error"
            job.error_message = f"Unsupported job type: {job.job_type}"
            if job.job_type.startswith("synthetic_") and job.fraction_number:
                from models.synthetic_ct import SyntheticCT
                sct = (
                    db.query(SyntheticCT)
                    .filter_by(plan_id=job.plan_id, fraction_number=job.fraction_number)
                    .first()
                )
                if sct and sct.status in ("generating", "running", "queued"):
                    sct.status = "error"
                    sct.error_message = job.error_message
            db.commit()
            return
        job.status = "running"
        job.started_at = datetime.utcnow()
        job.progress = 0.0
        db.commit()
        _set_plan_status(db, job.plan_id, "running")
        if is_cancelled(job_id):
            raise JobCancelled()
        result_path = _dispatch(job, db)

        # Guard: a gamma job that "succeeds" but persisted zero result rows is
        # an error, not a completion. Without this, an empty run goes green and
        # the dashboard silently shows nothing (observed intermittently; likely
        # double-fire row wipe or stale module). Fail loudly instead.
        if job.job_type == "gamma":
            from models.gamma_result import GammaResult
            n_rows = (
                db.query(GammaResult)
                .filter_by(plan_id=job.plan_id)
                .count()
            )
            if n_rows == 0:
                raise RuntimeError(
                    "Gamma job finished but zero GammaResult rows exist for "
                    f"plan {job.plan_id} -- refusing to mark complete."
                )

        job.status = "complete"
        job.progress = 1.0
        job.result_path = result_path
        job.completed_at = datetime.utcnow()
        db.commit()

        # If an MCsquare job completes, automatically calculate gamma against TPS
        if job.job_type == "mcSquare":
            try:
                from services.gamma_analysis import run_gamma_analysis
                logger.info(f"Auto-running gamma analysis after MCsquare for plan {job.plan_id}...")
                run_gamma_analysis(job.plan_id, db)
            except Exception as exc:
                logger.warning(f"Auto gamma analysis after MCsquare for plan {job.plan_id} failed: {exc}")

        # GATE_VERDICT_V1 -- every completed job changes the evidence, so
        # re-evaluate the gate and let it own plan.qa_status. This covers jobs
        # run manually from the UI, which never pass through pipeline.py.
        # Gamma jobs no longer set a verdict of their own.
        try:
            from services.pipeline import persist_gate
            persist_gate(job.plan_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Gate evaluation after job {job_id} failed: {exc}")
            _set_plan_status(db, job.plan_id, "pending")
        logger.info(f"Job {job_id} ({job.job_type}) complete -> {result_path}")
    except JobCancelled:
        logger.info(f"Job {job_id} cancelled by user")
        job = db.query(QAJob).filter_by(id=job_id).first()
        if job:
            job.status = "cancelled"
            job.error_message = "Cancelled by user"
            job.completed_at = datetime.utcnow()
            if job.job_type.startswith("synthetic_") and job.fraction_number:
                from models.synthetic_ct import SyntheticCT
                sct = (
                    db.query(SyntheticCT)
                    .filter_by(plan_id=job.plan_id, fraction_number=job.fraction_number)
                    .first()
                )
                if sct and sct.status in ("generating", "running", "queued"):
                    sct.status = "cancelled"
                    sct.error_message = "Cancelled by user"
            db.commit()
            _set_plan_status(db, job.plan_id, "pending")
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Job {job_id} failed: {exc}")
        job = db.query(QAJob).filter_by(id=job_id).first()
        if job:
            job.status = "error"
            job.error_message = str(exc)
            job.completed_at = datetime.utcnow()
            if job.job_type.startswith("synthetic_") and job.fraction_number:
                from models.synthetic_ct import SyntheticCT
                sct = (
                    db.query(SyntheticCT)
                    .filter_by(plan_id=job.plan_id, fraction_number=job.fraction_number)
                    .first()
                )
                if sct and sct.status in ("generating", "running", "queued"):
                    sct.status = "error"
                    sct.error_message = str(exc)
            db.commit()
            _set_plan_status(db, job.plan_id, "failed")
    finally:
        clear(job_id)
        db.close()


def _dispatch(job: QAJob, db) -> str:
    """Runs the appropriate service for the job type and returns the result path."""
    from services.mcSquare_runner import build_mcSquare_input, run_mcSquare
    from services.log_reconstructor import reconstruct_dose_from_log
    from services.gamma_analysis import run_gamma_analysis
    plan_id = job.plan_id
    if job.job_type == "mcSquare":
        input_dir = build_mcSquare_input(plan_id, db)
        output_dir = str(Path(settings.RESULTS_PATH) / f"plan_{plan_id}" / "mcSquare_output")
        return run_mcSquare(input_dir, output_dir, job.id, db)
    if job.job_type == "log_reconstruction":
        # Pass the job's own fraction number (set by run_stage2 at creation)
        # so the log-vs-Rx gamma is stored against the true delivered fraction
        # instead of defaulting to fx=1 and overwriting prior fractions.
        return reconstruct_dose_from_log(
            plan_id, job.fraction_number, db, job_id=job.id
        )
    if job.job_type == "gamma":
        run_gamma_analysis(plan_id, db, job_id=job.id)
        return str(Path(settings.RESULTS_PATH) / f"plan_{plan_id}" / "gamma")
    if job.job_type == "synthetic_ct_mcSquare":
        from services.synthetic_ct_service import calculate_synthetic_ct_dose
        fx = job.fraction_number or 1
        return calculate_synthetic_ct_dose(plan_id, fx, db, job_id=job.id)
    if job.job_type == "synthetic_ct_generate":
        from services.synthetic_ct_service import generate_synthetic_ct
        fx = job.fraction_number or 1
        sct = generate_synthetic_ct(plan_id, fx, db, dir_method="demons", auto_calculate=False, job_id=job.id)
        return sct.dicom_dir or ""
    if job.job_type in ("synthetic_ct_full", "synthetic_qact"):
        from services.synthetic_ct_service import generate_synthetic_ct
        fx = job.fraction_number or 1
        sct = generate_synthetic_ct(plan_id, fx, db, dir_method="demons", auto_calculate=False, job_id=job.id)
        return sct.dicom_dir or ""
    raise ValueError(f"Unsupported job type: {job.job_type}")

