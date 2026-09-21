from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc
from sqlalchemy.orm import Session

from database import get_db
from models.patient import Patient
from models.plan import Plan
from schemas.patient import PatientResponse, PatientWithLatestPlan
from schemas.plan import PlanSummary

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
