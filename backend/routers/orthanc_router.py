"""
backend/routers/orthanc_router.py -- REST API endpoints for Orthanc integration.

Exposes:
- Orthanc server status / connection testing
- Patient search and discovery in Orthanc
- Patient study and series metadata inspection
- Importing plans (RTPLAN, RTDOSE, RTSTRUCT, Planning CT)
- Querying and importing RT Treatment Records into plan fractions
- Querying and importing daily CBCTs and registrations into Offline Image Review (OIR)
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models.plan import Plan
from services.orthanc_service import (
    check_orthanc_connection,
    get_orthanc_patient_details,
    import_offline_images_from_orthanc,
    import_plan_from_orthanc,
    import_rtrecords_from_orthanc,
    list_orthanc_offline_images_for_plan,
    list_orthanc_rtrecords_for_plan,
    search_orthanc_patients,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/orthanc", tags=["orthanc"])


# ---------------------------------------------------------------------------
# Request Schemas
# ---------------------------------------------------------------------------

class ImportPlanRequest(BaseModel):
    plan_series_id: str
    dose_series_id: Optional[str] = None
    struct_series_id: Optional[str] = None
    ct_series_id: Optional[str] = None


class ImportRTRecordsRequest(BaseModel):
    series_ids: List[str] = Field(..., min_length=1)


class ImportOfflineImagesRequest(BaseModel):
    fraction_number: int = Field(..., ge=1)
    cbct_series_id: str
    reg_series_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/status")
def get_orthanc_status(
    url: Optional[str] = Query(None),
    username: Optional[str] = Query(None),
    password: Optional[str] = Query(None),
):
    """Test connection to Orthanc server and return system information."""
    return check_orthanc_connection(url=url, username=username, password=password)


@router.get("/patients")
def search_patients(
    query: str = Query("", description="Search term for Patient ID or Name"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Search for patients in the Orthanc PACS server."""
    try:
        return search_orthanc_patients(query=query, limit=limit, db=db)
    except Exception as exc:
        logger.error(f"Failed to search Orthanc patients: {exc}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Orthanc search failed: {exc}")


@router.get("/patients/{patient_orthanc_id}")
def get_patient_details(
    patient_orthanc_id: str,
    db: Session = Depends(get_db),
):
    """Retrieve full study and series breakdown for a patient in Orthanc."""
    try:
        return get_orthanc_patient_details(patient_orthanc_id=patient_orthanc_id, db=db)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error(f"Failed to get Orthanc patient details: {exc}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Orthanc query failed: {exc}")


@router.post("/import/plan")
def import_plan(
    payload: ImportPlanRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Import an RT Plan and associated Dose/Struct/Planning CT series from Orthanc.
    Saves DICOM files, creates DB entries, and triggers Stage 1 pipeline.
    """
    try:
        result = import_plan_from_orthanc(
            plan_series_id=payload.plan_series_id,
            dose_series_id=payload.dose_series_id,
            struct_series_id=payload.struct_series_id,
            ct_series_id=payload.ct_series_id,
            db=db,
            background_tasks=background_tasks,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.error(f"Failed to import plan from Orthanc: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Plan import failed: {exc}")


@router.get("/plans/{plan_id}/available-rtrecords")
def get_available_rtrecords(
    plan_id: int,
    db: Session = Depends(get_db),
):
    """List RT Treatment Records available in Orthanc matching this plan's patient."""
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    try:
        return list_orthanc_rtrecords_for_plan(plan_id=plan_id, db=db)
    except Exception as exc:
        logger.error(f"Failed to list RT records from Orthanc for plan {plan_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Failed to list RT records: {exc}")


@router.post("/plans/{plan_id}/import-rtrecords")
def import_rtrecords(
    plan_id: int,
    payload: ImportRTRecordsRequest,
    db: Session = Depends(get_db),
):
    """Import selected RT Treatment Records from Orthanc into plan fractions."""
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    try:
        result = import_rtrecords_from_orthanc(
            plan_id=plan_id,
            series_ids=payload.series_ids,
            db=db,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.error(f"Failed to import RT records for plan {plan_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"RT Record import failed: {exc}")


@router.get("/plans/{plan_id}/available-offline-images")
def get_available_offline_images(
    plan_id: int,
    db: Session = Depends(get_db),
):
    """List available CBCT scans and registrations in Orthanc for this plan's patient."""
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    try:
        return list_orthanc_offline_images_for_plan(plan_id=plan_id, db=db)
    except Exception as exc:
        logger.error(f"Failed to list offline images from Orthanc for plan {plan_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Failed to list offline images: {exc}")


@router.post("/plans/{plan_id}/import-offline-images")
def import_offline_images(
    plan_id: int,
    payload: ImportOfflineImagesRequest,
    db: Session = Depends(get_db),
):
    """Import a daily CBCT series and optional REG file for a specific fraction into OIR."""
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    try:
        result = import_offline_images_from_orthanc(
            plan_id=plan_id,
            fraction_number=payload.fraction_number,
            cbct_series_id=payload.cbct_series_id,
            reg_series_id=payload.reg_series_id,
            db=db,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.error(f"Failed to import offline images for plan {plan_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Offline image import failed: {exc}")
