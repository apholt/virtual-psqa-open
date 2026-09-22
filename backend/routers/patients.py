from datetime import datetime, timezone
import logging
from pathlib import Path
import shutil
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, text
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models.gamma_result import GammaResult
from models.patient import Patient
from models.plan import Plan
from models.qa_job import QAJob
from schemas.patient import PatientResponse, PatientWithLatestPlan
from schemas.plan import PlanSummary
from services.audit_service import log_audit_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/patients", tags=["patients"])


@router.get("", response_model=list[PatientWithLatestPlan])
async def list_patients(
    status: Optional[str] = Query(None, description="Filter by qa_status"),
    site: Optional[str] = Query(None, description="Filter by treatment_site"),
    search: Optional[str] = Query(None, description="Search patient ID or name"),
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """Returns patient list with their most recent plan's QA status."""
    query = db.query(Patient).order_by(desc(Patient.created_at))

    if search and isinstance(search, str):
        like = f"%{search}%"
        query = query.filter(
            Patient.patient_id.ilike(like) | Patient.patient_name.ilike(like)
        )

    patients = query.offset(skip).limit(limit).all()
    result = []
    now = datetime.now(timezone.utc)

    for patient in patients:
        # Get all plans for patient, newest first
        patient_plans = (
            db.query(Plan)
            .filter_by(patient_id=patient.id)
            .order_by(desc(Plan.created_at))
            .all()
        )
        latest_plan = patient_plans[0] if patient_plans else None

        if latest_plan:
            if status and isinstance(status, str) and latest_plan.qa_status != status:
                continue
            if site and isinstance(site, str) and (not latest_plan.treatment_site or site.lower() not in latest_plan.treatment_site.lower()):
                continue

        qa_status = latest_plan.qa_status if latest_plan else "pending"
        created_at = patient.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        days_since = (now - created_at).days

        result.append(
            PatientWithLatestPlan(
                id=patient.id,
                patient_id=patient.patient_id,
                patient_name=patient.patient_name,
                latest_plan_label=latest_plan.plan_label if latest_plan else None,
                latest_plan_site=latest_plan.treatment_site if latest_plan else None,
                qa_status=qa_status,
                days_since_created=days_since,
                number_of_fields=latest_plan.number_of_fields if latest_plan else None,
                plan_count=len(patient_plans),
                plans=[PlanSummary.model_validate(p) for p in patient_plans],
            )
        )

    return result


@router.get("/{patient_id}/plans", response_model=list[PlanSummary])
async def get_patient_plans(patient_id: int, db: Session = Depends(get_db)):
    """Returns all plans for a patient with QA status."""
    plans = (
        db.query(Plan)
        .filter_by(patient_id=patient_id)
        .order_by(desc(Plan.created_at))
        .all()
    )
    return plans


@router.delete("/{patient_id}")
async def delete_patient(patient_id: str, db: Session = Depends(get_db)):
    """
    Permanently removes a patient and all associated plans, QA jobs,
    fractions, synthetic CTs, chart checks, and gamma evaluations from the database.
    Retains beam deliveries as machine history (with plan_id negated).
    Removes calculated results and DICOM store files on disk.
    """
    patient = None
    if patient_id.isdigit():
        patient = db.query(Patient).filter_by(id=int(patient_id)).first()
    if not patient:
        patient = db.query(Patient).filter_by(patient_id=patient_id).first()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    plans = db.query(Plan).filter_by(patient_id=patient.id).all()
    plan_ids = [p.id for p in plans]

    if plan_ids:
        active = (
            db.query(QAJob)
            .filter(QAJob.plan_id.in_(plan_ids), QAJob.status.in_(["running", "queued"]))
            .count()
        )
        if active:
            raise HTTPException(
                status_code=409,
                detail=f"{active} job(s) still running/queued for this patient's plans. "
                       "Cancel them before deleting.",
            )

    deleted_counts = {
        "plans": len(plan_ids),
        "gamma_results": 0,
        "qa_jobs": 0,
        "fractions": 0,
        "synthetic_cts": 0,
        "chart_checks": 0,
    }

    if plan_ids:
        deleted_counts["gamma_results"] = (
            db.query(GammaResult)
            .filter(GammaResult.plan_id.in_(plan_ids))
            .delete(synchronize_session=False)
        )
        deleted_counts["qa_jobs"] = (
            db.query(QAJob)
            .filter(QAJob.plan_id.in_(plan_ids))
            .delete(synchronize_session=False)
        )
        try:
            from models.fraction import Fraction
            deleted_counts["fractions"] = (
                db.query(Fraction)
                .filter(Fraction.plan_id.in_(plan_ids))
                .delete(synchronize_session=False)
            )
        except Exception:
            pass

        try:
            from models.synthetic_ct import SyntheticCT
            deleted_counts["synthetic_cts"] = (
                db.query(SyntheticCT)
                .filter(SyntheticCT.plan_id.in_(plan_ids))
                .delete(synchronize_session=False)
            )
        except Exception:
            pass

        try:
            from models.chart_check import ChartCheck
            deleted_counts["chart_checks"] = (
                db.query(ChartCheck)
                .filter(ChartCheck.plan_id.in_(plan_ids))
                .delete(synchronize_session=False)
            )
        except Exception:
            pass

        try:
            for pid in plan_ids:
                db.execute(
                    text("DELETE FROM gamma_predictions WHERE plan_id = :pid"),
                    {"pid": pid},
                )
        except Exception:
            pass

        try:
            from models.ml_prediction import MLPrediction
            db.query(MLPrediction).filter(MLPrediction.plan_id.in_(plan_ids)).delete(synchronize_session=False)
        except Exception:
            pass

        # Retain beam deliveries as machine history (breaking patient linkage)
        retained = 0
        try:
            cols = [r[1] for r in db.execute(text("PRAGMA table_info(beam_deliveries)")).fetchall()]
            has_retained = "retained_at" in cols
            for pid in plan_ids:
                if has_retained:
                    retained += db.execute(
                        text("UPDATE beam_deliveries SET retained_at = datetime('now'), plan_id = -:pid WHERE plan_id = :pid"),
                        {"pid": pid},
                    ).rowcount
                else:
                    retained += db.execute(
                        text("UPDATE beam_deliveries SET plan_id = -:pid WHERE plan_id = :pid"),
                        {"pid": pid},
                    ).rowcount
            deleted_counts["beam_deliveries_retained"] = retained
        except Exception as exc:
            logger.warning(f"Could not retain beam deliveries for patient {patient.patient_id}: {exc}")

        # Delete all plans for this patient
        db.query(Plan).filter(Plan.patient_id == patient.id).delete(synchronize_session=False)

    pat_name = patient.patient_name
    pat_str_id = patient.patient_id
    db.delete(patient)
    db.commit()

    results_removed = 0
    for pid in plan_ids:
        results_dir = Path(settings.RESULTS_PATH) / f"plan_{pid}"
        if results_dir.exists():
            shutil.rmtree(results_dir, ignore_errors=True)
            results_removed += 1

    dicom_removed = False
    if pat_str_id:
        pat_dicom_dir = Path(settings.DICOM_STORE_PATH) / pat_str_id
        if pat_dicom_dir.exists():
            shutil.rmtree(pat_dicom_dir, ignore_errors=True)
            dicom_removed = True

    try:
        log_audit_event(
            username="system",
            action="PATIENT_DELETE",
            target_type="patient",
            target_id=pat_str_id,
            details={
                "patient_name": pat_name,
                "plans_deleted": len(plan_ids),
                "plan_ids": plan_ids,
            },
            db=db,
        )
    except Exception as exc:
        logger.warning(f"Failed to log audit event for deleted patient: {exc}")

    logger.info(
        f"Deleted patient {pat_str_id} ('{pat_name}'): {len(plan_ids)} plan(s), "
        f"results_dirs_removed={results_removed}, dicom_folder_removed={dicom_removed}"
    )

    return {
        "success": True,
        "patient_id": pat_str_id,
        "patient_name": pat_name,
        "deleted_rows": deleted_counts,
        "plans_deleted": len(plan_ids),
        "results_dirs_removed": results_removed,
        "dicom_folder_removed": dicom_removed,
        "message": f"Patient {pat_str_id} ({pat_name}) and all associated data deleted successfully.",
    }

