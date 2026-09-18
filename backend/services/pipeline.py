"""
Automated two-stage pipeline orchestration.

Stage 1 - plan arrival (RTPlan/RTDose/RTStruct settled):
    complexity extraction -> MCsquare secondary dose -> gamma vs TPS ->
    evidence gate -> SSE broadcast

Stage 2 - fraction delivery (RT Ion Record settled):
    log reconstruction (clinical: delivered-vs-prescribed gamma, computed and
    stored inside the log_reconstruction job itself) -> evidence gate update ->
    SSE broadcast

The verdict comes from services/gate.py, not from the retired complexity/ML
model. That model scored plan characteristics as if they predicted QA outcome;
they do not. Its virtual_approve branch was also unreachable in practice, since
it required confidence == high, which required at least 20 scored plans AND
AUC >= 0.80 from a model whose leave-one-plan-out skill measured at or below
zero.

Each stage runs in its own daemon thread so ingestion (HTTP upload or folder
watcher) never blocks. Individual steps reuse the QAJob machinery in
job_runner so progress, cancellation and error handling are consistent.
"""
from __future__ import annotations

# GATE_PIPELINE_PREDICTION_V1 -- prediction is computed at ingest, not on demand.
import logging
import os
import threading
from datetime import datetime
from typing import Optional

# GATE_PIPELINE_V1 -- verdict comes from services/gate.py, not ml_predictor.

from database import SessionLocal
from models.qa_job import QAJob
from models.plan import Plan

logger = logging.getLogger(__name__)


def _create_job(plan_id: int, job_type: str,
                fraction_number: Optional[int] = None) -> int:
    """Create a queued QAJob row and return its id.

    fraction_number is stored on the job so a log_reconstruction job carries
    the delivered fraction it belongs to (None for plan-level jobs).
    """
    db = SessionLocal()
    try:
        job = QAJob(
            plan_id=plan_id,
            job_type=job_type,
            status="queued",
            progress=0.0,
            fraction_number=fraction_number,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id
    finally:
        db.close()


def _job_succeeded(job_id: int) -> bool:
    db = SessionLocal()
    try:
        job = db.query(QAJob).filter_by(id=job_id).first()
        return bool(job and job.status == "complete")
    finally:
        db.close()


def _run_step(plan_id: int, job_type: str,
              fraction_number: Optional[int] = None) -> bool:
    """Create and synchronously run a QA job. Returns True on success."""
    from services.job_runner import run_qa_job

    job_id = _create_job(plan_id, job_type, fraction_number)
    run_qa_job(job_id)  # runs to completion in this thread
    ok = _job_succeeded(job_id)
    if not ok:
        logger.warning(f"Pipeline step '{job_type}' for plan {plan_id} did not complete")
    return ok


def _db_path() -> str:
    """Resolve the SQLite file the app uses (matches routers/monitoring.py)."""
    try:
        from database import engine
        url = str(engine.url)
        if url.startswith("sqlite"):
            p = url.split("///", 1)[-1]
            if os.path.exists(p):
                return p
    except Exception:  # noqa: BLE001
        pass
    for c in (os.environ.get("PSQA_DB", ""),
              os.path.join("data", "psqa.db")):
        if c and os.path.exists(c):
            return c
    return "./data/psqa.db"


def _store_prediction(plan_id: int) -> int:
    """Compute and store the per-field log-QA gamma prediction. Best effort.

    Never raises: the prediction is supporting pre-treatment evidence and must
    not be able to fail an ingest. A plan with no prediction is still gated
    normally on the four evidence layers.
    """
    try:
        from services.prediction_model import store_plan_prediction
        n = store_plan_prediction(plan_id, _db_path())
        if n:
            logger.info(f"[Stage 1] stored {n} beam prediction(s) for "
                        f"plan {plan_id}")
        else:
            logger.info(f"[Stage 1] no prediction stored for plan {plan_id} "
                        f"(no room model yet, or plan file unreadable)")
        return n
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Prediction failed for plan {plan_id}: {exc}")
        return 0


def _evaluate_gate(plan_id: int):
    """Evaluate the delivery gate. Returns a GateDecision or None."""
    try:
        from services.evidence import plan_gate
        decision, _evidence = plan_gate(plan_id, _db_path())
        return decision
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Gate evaluation failed for plan {plan_id}: {exc}")
        return None


# GATE_VERDICT_V1 ----------------------------------------------------------
# plan.qa_status has exactly one writer of VERDICTS: this function.
#
# It previously had two. services/gamma_analysis.py::compute_verdict wrote
# pass / flagged / measure_needed from gamma rows alone, while gate.py computed
# cleared / verified / investigate / escalate / measure / incomplete and
# persisted nothing. Different vocabularies, different inputs, same column --
# so one plan could read "verified" on the dashboard, "investigate" on its
# detail page and "pending" on dose comparison, all at the same moment.
#
# qa_status now holds a GateStatus value verbatim. Two non-verdict states
# remain legitimate and are still written by job_runner:
#   "running" -- a job is in flight
#   "failed"  -- a job errored; no evidence to gate on
# "pending" stays the ingest default, meaning "the gate has not run yet".
# Those describe machine state, not a clinical conclusion, so the gate does
# not overwrite "failed".

_NON_VERDICT_STATES = {"running"}


def persist_gate(plan_id: int):
    """Evaluate the gate and write its status to plan.qa_status.

    Returns the GateDecision, or None if evaluation failed. Never raises: a
    gate that cannot be evaluated must leave the stored status alone rather
    than overwrite it with a guess.
    """
    decision = _evaluate_gate(plan_id)
    if decision is None:
        return None

    db = SessionLocal()
    try:
        plan = db.query(Plan).filter_by(id=plan_id).first()
        if plan is None:
            return decision
        if plan.qa_status in _NON_VERDICT_STATES:
            logger.info(f"Plan {plan_id} is '{plan.qa_status}' -- not "
                        f"overwriting while job in flight")
            return decision
        previous = plan.qa_status
        plan.qa_status = decision.status.value
        db.commit()
        if previous != plan.qa_status:
            logger.info(f"Plan {plan_id} qa_status {previous} -> "
                        f"{plan.qa_status}")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Could not persist gate status for plan {plan_id}: {exc}")
        db.rollback()
    finally:
        db.close()
    return decision


def _broadcast_plan_update(plan_id: int, decision=None) -> None:
    """Push a worklist/dashboard update over SSE (best effort).

    The dashboard refetches on any event, so this payload is a notification
    rather than the source of truth; the authoritative verdict comes from
    /api/dashboard, which evaluates the gate itself.
    """
    db = SessionLocal()
    try:
        from routers.events import broadcast_new_plan_sync

        plan = db.query(Plan).filter_by(id=plan_id).first()
        if plan is None:
            return
        payload = {
            "plan_id": plan.id,
            "patient_id": plan.patient.patient_id if plan.patient else "",
            "patient_name": plan.patient.patient_name if plan.patient else "",
            "plan_label": plan.plan_label,
            "qa_status": plan.qa_status,
            "verdict": decision.status.value if decision else None,
            "dry_run_waived": (bool(decision.dry_run_waived)
                               if decision else False),
        }
        broadcast_new_plan_sync(payload)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"SSE broadcast skipped: {exc}")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Stage 1
# ---------------------------------------------------------------------------

def _stage1(plan_id: int) -> None:
    logger.info(f"[Stage 1] starting pipeline for plan {plan_id}")

    # Announce arrival. No verdict yet: before the secondary dose calculation
    # runs there is no evidence to gate on, and the gate does not guess.
    _broadcast_plan_update(plan_id)

    # MCsquare secondary dose calculation.
    if not _run_step(plan_id, "mcSquare"):
        logger.warning(f"[Stage 1] MCsquare failed for plan {plan_id}; continuing")

    # Gamma vs TPS (runs every comparison whose doses are present).
    _run_step(plan_id, "gamma")

    # Per-field pre-treatment prediction, from the room's measured behaviour
    # applied to this plan's spot list. Stored once; read many times.
    _store_prediction(plan_id)

    # Evaluate the gate now that Monte-Carlo evidence is available.
    decision = persist_gate(plan_id)
    _broadcast_plan_update(plan_id, decision)

    verdict = decision.status.value if decision else "n/a"
    logger.info(f"[Stage 1] complete for plan {plan_id} -> gate={verdict}")


def run_stage1(plan_id: int, background: bool = True) -> None:
    """Kick off the Stage 1 pipeline (optionally on a daemon thread)."""
    if background:
        threading.Thread(target=_stage1, args=(plan_id,), daemon=True).start()
    else:
        _stage1(plan_id)


# ---------------------------------------------------------------------------
# Stage 2
# ---------------------------------------------------------------------------

def _stage2(plan_id: int, fraction_number: Optional[int]) -> None:
    logger.info(f"[Stage 2] starting log pipeline for plan {plan_id} (fx {fraction_number})")

    # The log_reconstruction job performs the full clinical log QA: it
    # reconstructs delivered vs prescribed dose from the RT Ion Record and
    # computes + stores the per-beam log-vs-Rx gamma (comparison_type
    # "log_vs_Rx") directly in GammaResult. There is NO separate RTDose-based
    # gamma step for the log path - the reference is the prescription, not a
    # TPS RTDose, so the old _run_fraction_gamma step does not apply here.
    #
    # fraction_number is threaded onto the job so the reconstruction stores its
    # gamma against the true delivered fraction instead of defaulting to fx=1.
    if not _run_step(plan_id, "log_reconstruction", fraction_number):
        logger.warning(f"[Stage 2] log reconstruction failed for plan {plan_id}")
        return

    # Re-evaluate now that this fraction's log evidence is available.
    decision = persist_gate(plan_id)
    _broadcast_plan_update(plan_id, decision)

    verdict = decision.status.value if decision else "n/a"
    logger.info(f"[Stage 2] complete for plan {plan_id} -> gate={verdict}")


def run_stage2(plan_id: int, fraction_number: Optional[int] = None, background: bool = True) -> None:
    """Kick off the Stage 2 pipeline (optionally on a daemon thread)."""
    if background:
        threading.Thread(target=_stage2, args=(plan_id, fraction_number), daemon=True).start()
    else:
        _stage2(plan_id, fraction_number)
