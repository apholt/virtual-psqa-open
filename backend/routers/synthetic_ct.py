"""
routers/synthetic_ct.py -- REST API endpoints for SyntheticQACT adaptive setup,
CBCT ingestion, synthetic CT generation, and dose QA.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional

import numpy as np
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models.plan import Plan
from models.qa_job import QAJob
from models.synthetic_ct import SyntheticCT
from services.job_control import request_cancel
from services.job_runner import run_qa_job
from services.synthetic_ct_service import (
    approve_external_and_calculate_dose,
    calculate_synthetic_ct_dvh,
    generate_synthetic_ct,
    get_cbct_image_plane,
    get_sct_dose_plane,
    get_sct_dose_diff_plane,
    get_sct_external_info,
    get_sct_external_plane,
    get_sct_gamma_plane,
    get_sct_image_plane,
    get_sct_reference_dose_plane,
    get_synthetic_ct_detail,
    ingest_cbct_series,
    ingest_synthetic_ct_files,
    list_synthetic_cts,
    recompute_fraction_external,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/plans/{plan_id}/synthetic-ct", tags=["synthetic-ct"])


@router.get("")
def get_plan_synthetic_cts(plan_id: int, db: Session = Depends(get_db)):
    """List all synthetic CT scans and QA statuses for a plan."""
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    return list_synthetic_cts(plan_id, db)


@router.get("/{fraction_number}")
def get_fraction_synthetic_ct(plan_id: int, fraction_number: int, db: Session = Depends(get_db)):
    """Get detailed synthetic CT metrics and metadata for a specific fraction."""
    detail = get_synthetic_ct_detail(plan_id, fraction_number, db)
    if not detail:
        raise HTTPException(
            status_code=404,
            detail=f"No synthetic CT found for plan {plan_id} fraction {fraction_number}",
        )
    return detail


@router.post("/import-cbct")
async def import_cbct_and_generate(
    plan_id: int,
    background_tasks: BackgroundTasks,
    fraction_number: int = Query(..., description="Treatment fraction number"),
    auto_generate: bool = Query(True, description="Automatically generate synthetic CT after import"),
    auto_calculate: bool = Query(True, description="Automatically calculate openMCsquare dose"),
    dir_method: str = Query("demons", description="DIR method: 'demons' or 'bspline_lcc'"),
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """
    Import daily CBCT series (DICOM files or .zip), associate with fraction,
    and optionally trigger synthetic CT generation and Monte Carlo dose calculation.
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"cbct_upload_plan{plan_id}_fx{fraction_number}_"))
    saved_paths: list[Path] = []
    try:
        for f in files:
            dest = tmp_dir / (f.filename or "slice.dcm")
            with open(dest, "wb") as buffer:
                shutil.copyfileobj(f.file, buffer)
            saved_paths.append(dest)

        sct, cbct_path = ingest_cbct_series(plan_id, fraction_number, saved_paths, db)

        job_id = None
        if auto_generate:
            job_type = "synthetic_ct_full" if auto_calculate else "synthetic_ct_generate"
            job = QAJob(
                plan_id=plan_id,
                job_type=job_type,
                status="queued",
                progress=0.0,
                fraction_number=fraction_number,
            )
            db.add(job)
            sct.status = "generating"
            db.commit()
            db.refresh(job)
            job_id = job.id
            background_tasks.add_task(run_qa_job, job.id)

        return {
            "status": "success",
            "fraction_number": fraction_number,
            "cbct_num_slices": sct.cbct_num_slices,
            "job_id": job_id,
        }
    except Exception as exc:
        logger.exception(f"CBCT import failed: {exc}")
        raise HTTPException(status_code=422, detail=str(exc))
    finally:
        if tmp_dir.exists():
            try:
                shutil.rmtree(tmp_dir)
            except Exception:
                pass


@router.post("/{fraction_number}/generate")
def trigger_synthetic_ct_generation(
    plan_id: int,
    fraction_number: int,
    background_tasks: BackgroundTasks,
    dir_method: str = Query("demons", description="DIR method: 'demons' or 'bspline_lcc'"),
    auto_calculate: bool = Query(True, description="Automatically calculate dose after generation"),
    db: Session = Depends(get_db),
):
    """
    Trigger synthetic CT generation from already ingested CBCT scan.
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    sct = (
        db.query(SyntheticCT)
        .filter_by(plan_id=plan_id, fraction_number=fraction_number)
        .first()
    )
    if not sct or not sct.cbct_dir:
        raise HTTPException(
            status_code=404,
            detail=f"No CBCT scan found for plan {plan_id} fraction {fraction_number}",
        )

    job_type = "synthetic_ct_full" if auto_calculate else "synthetic_ct_generate"
    job = QAJob(
        plan_id=plan_id,
        job_type=job_type,
        status="queued",
        progress=0.0,
        fraction_number=fraction_number,
    )
    db.add(job)
    sct.status = "generating"
    sct.error_message = None
    db.commit()
    db.refresh(job)

    background_tasks.add_task(run_qa_job, job.id)
    return {
        "job_id": job.id,
        "status": "queued",
        "fraction_number": fraction_number,
    }


@router.post("/upload")
async def upload_synthetic_ct(
    plan_id: int,
    background_tasks: BackgroundTasks,
    fraction_number: int = Query(..., description="The fraction number to associate with this scan"),
    auto_calculate: bool = Query(True, description="Automatically queue MCsquare dose calculation"),
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """
    Legacy endpoint: direct upload of pre-generated DICOM CT image series.
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"sct_upload_plan{plan_id}_fx{fraction_number}_"))
    saved_paths: list[Path] = []
    try:
        for f in files:
            dest = tmp_dir / (f.filename or "slice.dcm")
            with open(dest, "wb") as buffer:
                shutil.copyfileobj(f.file, buffer)
            saved_paths.append(dest)

        sct = ingest_synthetic_ct_files(plan_id, fraction_number, saved_paths, db)

        job_id = None
        if auto_calculate:
            job = QAJob(
                plan_id=plan_id,
                job_type="synthetic_ct_mcSquare",
                status="queued",
                progress=0.0,
                fraction_number=fraction_number,
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            job_id = job.id
            background_tasks.add_task(run_qa_job, job.id)

        return {
            "status": "success",
            "fraction_number": fraction_number,
            "num_slices": sct.num_slices,
            "dimensions": sct.dimensions,
            "job_id": job_id,
        }
    except Exception as exc:
        logger.exception(f"Synthetic CT upload failed: {exc}")
        raise HTTPException(status_code=422, detail=str(exc))
    finally:
        if tmp_dir.exists():
            try:
                shutil.rmtree(tmp_dir)
            except Exception:
                pass


@router.post("/{fraction_number}/calculate")
def trigger_synthetic_ct_calculation(
    plan_id: int,
    fraction_number: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Queue or recalculate openMCsquare proton dose on the fraction's synthetic CT.
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    sct = (
        db.query(SyntheticCT)
        .filter_by(plan_id=plan_id, fraction_number=fraction_number)
        .first()
    )
    if not sct or not sct.dicom_dir:
        raise HTTPException(
            status_code=404,
            detail=f"No synthetic CT uploaded or generated for plan {plan_id} fraction {fraction_number}",
        )

    job = QAJob(
        plan_id=plan_id,
        job_type="synthetic_ct_mcSquare",
        status="queued",
        progress=0.0,
        fraction_number=fraction_number,
    )
    db.add(job)
    sct.status = "running"
    sct.error_message = None
    db.commit()
    db.refresh(job)

    background_tasks.add_task(run_qa_job, job.id)
    return {
        "job_id": job.id,
        "status": "queued",
        "fraction_number": fraction_number,
    }


@router.get("/{fraction_number}/dose/plane/{z}")
def get_fraction_dose_plane(
    plan_id: int,
    fraction_number: int,
    z: int,
    db: Session = Depends(get_db),
):
    """Stream binary float32 slice of openMCsquare dose on the synthetic CT."""
    try:
        plane, max_dose = get_sct_dose_plane(plan_id, fraction_number, z, db)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    buf = plane.astype("<f4")
    return Response(
        content=buf.tobytes(),
        media_type="application/octet-stream",
        headers={
            "X-Rows": str(buf.shape[0]),
            "X-Cols": str(buf.shape[1]),
            "X-Max-Dose": str(round(max_dose, 4)),
            "Access-Control-Expose-Headers": "X-Rows,X-Cols,X-Max-Dose",
        },
    )


@router.get("/{fraction_number}/ct/plane/{z}")
def get_fraction_ct_plane(
    plan_id: int,
    fraction_number: int,
    z: int,
    db: Session = Depends(get_db),
):
    """Stream binary float32 slice of synthetic CT Hounsfield Units."""
    try:
        plane = get_sct_image_plane(plan_id, fraction_number, z, db)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    buf = plane.astype("<f4")
    return Response(
        content=buf.tobytes(),
        media_type="application/octet-stream",
        headers={
            "X-Rows": str(buf.shape[0]),
            "X-Cols": str(buf.shape[1]),
            "Access-Control-Expose-Headers": "X-Rows,X-Cols",
        },
    )


@router.get("/{fraction_number}/cbct/plane/{z}")
def get_fraction_cbct_plane(
    plan_id: int,
    fraction_number: int,
    z: int,
    db: Session = Depends(get_db),
):
    """Stream binary float32 slice of raw CBCT Hounsfield Units."""
    try:
        plane = get_cbct_image_plane(plan_id, fraction_number, z, db)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    buf = plane.astype("<f4")
    return Response(
        content=buf.tobytes(),
        media_type="application/octet-stream",
        headers={
            "X-Rows": str(buf.shape[0]),
            "X-Cols": str(buf.shape[1]),
            "Access-Control-Expose-Headers": "X-Rows,X-Cols",
        },
    )


@router.get("/{fraction_number}/gamma/plane/{z}")
def get_fraction_gamma_plane(
    plan_id: int,
    fraction_number: int,
    z: int,
    reference: str = Query("tps", description="Comparison reference: 'tps', 'mcsquare', or 'mcsquare_prev'"),
    db: Session = Depends(get_db),
):
    """Stream binary float32 slice of 3D Gamma evaluation against selected reference."""
    try:
        plane, passing_rate = get_sct_gamma_plane(plan_id, fraction_number, z, db, reference=reference)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    buf = plane.astype("<f4")
    return Response(
        content=buf.tobytes(),
        media_type="application/octet-stream",
        headers={
            "X-Rows": str(buf.shape[0]),
            "X-Cols": str(buf.shape[1]),
            "X-Passing-Rate": str(round(passing_rate, 2)),
            "X-Reference": reference,
            "Access-Control-Expose-Headers": "X-Rows,X-Cols,X-Passing-Rate,X-Reference",
        },
    )


@router.get("/{fraction_number}/ref-dose/plane/{z}")
def get_fraction_reference_dose_plane(
    plan_id: int,
    fraction_number: int,
    z: int,
    reference: str = Query("tps", description="Comparison reference: 'tps', 'mcsquare', or 'mcsquare_prev'"),
    db: Session = Depends(get_db),
):
    """Stream binary float32 slice of reference dose (TPS or Baseline MC) aligned to synthetic CT."""
    try:
        plane, max_dose, ref_name = get_sct_reference_dose_plane(plan_id, fraction_number, z, db, reference=reference)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    buf = plane.astype("<f4")
    return Response(
        content=buf.tobytes(),
        media_type="application/octet-stream",
        headers={
            "X-Rows": str(buf.shape[0]),
            "X-Cols": str(buf.shape[1]),
            "X-Max-Dose": str(round(max_dose, 4)),
            "X-Reference": ref_name,
            "Access-Control-Expose-Headers": "X-Rows,X-Cols,X-Max-Dose,X-Reference",
        },
    )


@router.get("/{fraction_number}/dose-diff/plane/{z}")
def get_fraction_dose_diff_plane(
    plan_id: int,
    fraction_number: int,
    z: int,
    reference: str = Query("tps", description="Comparison reference: 'tps', 'mcsquare', or 'mcsquare_prev'"),
    db: Session = Depends(get_db),
):
    """Stream binary float32 slice of relative % dose difference ((sCT - Ref) / MaxDose * 100)."""
    try:
        plane, max_diff, ref_name = get_sct_dose_diff_plane(plan_id, fraction_number, z, db, reference=reference)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    buf = plane.astype("<f4")
    return Response(
        content=buf.tobytes(),
        media_type="application/octet-stream",
        headers={
            "X-Rows": str(buf.shape[0]),
            "X-Cols": str(buf.shape[1]),
            "X-Max-Diff": str(round(max_diff, 2)),
            "X-Reference": ref_name,
            "Access-Control-Expose-Headers": "X-Rows,X-Cols,X-Max-Diff,X-Reference",
        },
    )



class RecomputeExternalRequest(BaseModel):
    source: str = Field("rtstruct", description="'rtstruct', 'auto', or 'convex_hull'")
    threshold_hu: float = Field(-350.0, description="HU threshold for tissue segmentation")
    closing_radius: int = Field(5, ge=1, le=31, description="Morphological closing radius")
    use_convex_hull: bool = Field(False, description="Use 2D convex envelope per slice")
    include_mask: bool = Field(True, description="Include thermoplastic immobilization mask and headrest in external contour")
    roi_name: Optional[str] = Field(None, description="Specific RTSTRUCT ROI name to use, e.g. 'External' or 'Skin_Surface'")


@router.get("/{fraction_number}/external/plane/{z}")
def get_fraction_external_plane(
    plan_id: int,
    fraction_number: int,
    z: int,
    target: str = Query("sct", description="'sct' for synthetic CT or 'cbct' for raw CBCT"),
    db: Session = Depends(get_db),
):
    """Stream binary uint8 slice (1=interior, 0=exterior) of external body contour mask."""
    try:
        plane = get_sct_external_plane(plan_id, fraction_number, z, db, target=target)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    buf = plane.astype(np.uint8)
    return Response(
        content=buf.tobytes(),
        media_type="application/octet-stream",
        headers={
            "X-Rows": str(buf.shape[0]),
            "X-Cols": str(buf.shape[1]),
            "Access-Control-Expose-Headers": "X-Rows,X-Cols",
        },
    )


@router.get("/{fraction_number}/external/info")
def get_fraction_external_info(
    plan_id: int,
    fraction_number: int,
    target: str = Query("sct", description="'sct' for synthetic CT or 'cbct' for raw CBCT"),
    db: Session = Depends(get_db),
):
    """Get metadata for the fraction's external contour mask."""
    return get_sct_external_info(plan_id, fraction_number, db, target=target)


@router.post("/{fraction_number}/recompute-external")
def post_recompute_fraction_external(
    plan_id: int,
    fraction_number: int,
    payload: RecomputeExternalRequest,
    db: Session = Depends(get_db),
):
    """Recompute external body contour with chosen source and parameters."""
    try:
        res = recompute_fraction_external(
            plan_id=plan_id,
            fraction_number=fraction_number,
            source=payload.source,
            threshold_hu=payload.threshold_hu,
            closing_radius=payload.closing_radius,
            use_convex_hull=payload.use_convex_hull,
            include_mask=payload.include_mask,
            selected_roi_name=payload.roi_name,
            db=db,
        )
        return res
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception(f"Recompute external contour failed: {exc}")
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/{fraction_number}/approve-external")
def post_approve_external(
    plan_id: int,
    fraction_number: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Approve external contour and queue openMCsquare dose calculation."""
    try:
        res = approve_external_and_calculate_dose(
            plan_id=plan_id,
            fraction_number=fraction_number,
            db=db,
            background_tasks=background_tasks,
        )
        return res
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception(f"Approve external failed: {exc}")
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/{fraction_number}/cancel")
def cancel_fraction_calculation(
    plan_id: int,
    fraction_number: int,
    db: Session = Depends(get_db),
):
    """Cancels any running openMCsquare dose calculation or sCT generation for this fraction."""
    active_jobs = (
        db.query(QAJob)
        .filter(
            QAJob.plan_id == plan_id,
            QAJob.fraction_number == fraction_number,
            QAJob.status.in_(["queued", "running", "generating"]),
        )
        .all()
    )
    other_jobs = (
        db.query(QAJob)
        .filter(
            QAJob.plan_id == plan_id,
            QAJob.status.in_(["queued", "running", "generating"]),
            QAJob.job_type.in_(["synthetic_ct_mcSquare", "synthetic_ct_generate", "synthetic_ct_full"]),
        )
        .all()
    )

    cancelled_ids = set()
    for job in active_jobs + other_jobs:
        if job.id not in cancelled_ids:
            cancelled_ids.add(job.id)
            request_cancel(job.id)
            job.status = "cancelled"
            job.error_message = "Cancelled by user"

    sct = (
        db.query(SyntheticCT)
        .filter_by(plan_id=plan_id, fraction_number=fraction_number)
        .first()
    )
    if sct:
        if sct.num_slices > 0:
            sct.status = "contour_check"
        elif sct.cbct_num_slices:
            sct.status = "cbct_uploaded"
        else:
            sct.status = "cancelled"
        sct.error_message = "Calculation cancelled by user"

    db.commit()
    logger.info(f"Cancelled calculation for plan {plan_id} fx {fraction_number} (jobs: {list(cancelled_ids)})")
    return {
        "status": "cancelled",
        "plan_id": plan_id,
        "fraction_number": fraction_number,
        "cancelled_jobs": list(cancelled_ids),
    }


@router.get("/{fraction_number}/dvh")
def get_fraction_synthetic_ct_dvh(
    plan_id: int,
    fraction_number: int,
    recompute: bool = Query(False, description="Force recompute of deformed target DVH"),
    db: Session = Depends(get_db),
):
    """
    Get cumulative Dose-Volume Histogram (DVH) curves and clinical target coverage
    metrics for the deformed targets and OARs on the Synthetic CT.
    """
    try:
        data = calculate_synthetic_ct_dvh(
            plan_id=plan_id,
            fraction_number=fraction_number,
            db=db,
            force_recompute=recompute,
        )
        return data
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception(f"Error computing synthetic CT DVH: {exc}")
        raise HTTPException(status_code=422, detail=str(exc))



