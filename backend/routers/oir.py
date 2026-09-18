"""
backend/routers/oir.py -- REST API endpoints for Offline Image Review (OIR).
Provides Planning CT vs daily CBCT comparisons, rigid registration evaluations,
RTSTRUCT contour overlay extraction, and 5-fraction physicist chart checks.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models.plan import Plan
from services.oir_service import (
    get_chart_checks,
    get_oir_plan_info,
    get_oir_slice,
    save_chart_check,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/oir", tags=["oir"])


class ChartCheckCreateRequest(BaseModel):
    fraction_number: int = Field(..., ge=1)
    reviewer_name: str = Field("Medical Physicist")
    status: str = Field("pass")  # "pass", "acceptable", "flagged"
    notes: str = Field("")
    shifts_verified: bool = Field(True)
    contours_verified: bool = Field(True)


@router.get("/{plan_id}/info")
def get_oir_info(plan_id: int, db: Session = Depends(get_db)):
    """Retrieve planning CT metadata, available fractions, CBCT series, registrations, and ROIs."""
    try:
        return get_oir_plan_info(plan_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error(f"Failed to get OIR info for plan {plan_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"OIR error: {exc}")


@router.get("/{plan_id}/slice/{slice_idx}")
def get_oir_slice_data(
    plan_id: int,
    slice_idx: int,
    fraction_number: int = Query(1, ge=1),
    registration_id: str = Query(""),
    cbct_series_uid: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Retrieve fused Planning CT and resampled CBCT slice with RTSTRUCT contours and shift details."""
    try:
        return get_oir_slice(
            plan_id=plan_id,
            slice_idx=slice_idx,
            fraction_number=fraction_number,
            registration_id=registration_id,
            cbct_series_uid=cbct_series_uid,
            db=db,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(f"Failed to get OIR slice {slice_idx} for plan {plan_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"OIR slice error: {exc}")


@router.get("/{plan_id}/chart-checks")
def get_oir_chart_checks(plan_id: int, db: Session = Depends(get_db)):
    """List all recorded physicist chart check reviews for this plan."""
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    return get_chart_checks(plan_id)


@router.post("/{plan_id}/chart-check")
def create_chart_check(
    plan_id: int,
    payload: ChartCheckCreateRequest,
    db: Session = Depends(get_db),
):
    """Record a physicist chart check review for a fraction."""
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    try:
        return save_chart_check(
            plan_id=plan_id,
            fraction_number=payload.fraction_number,
            reviewer_name=payload.reviewer_name,
            status=payload.status,
            notes=payload.notes,
            shifts_verified=payload.shifts_verified,
            contours_verified=payload.contours_verified,
        )
    except Exception as exc:
        logger.error(f"Failed to save chart check for plan {plan_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to save chart check: {exc}")
