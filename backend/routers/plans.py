import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models.plan import Plan
from schemas.plan import FieldSummary, PlanIngestionResponse, PlanSummary
from services.dicom_ingestor import ingest_dicom_directory, parse_rtplan_fields
import pydicom

router = APIRouter(prefix="/api/plans", tags=["plans"])


@router.post("/upload", response_model=PlanIngestionResponse)
async def upload_dicom(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """
    Accepts multipart upload of multiple .dcm files or a single .zip.
    Saves to a temp directory, runs ingestion, returns summary.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        saved_files: list[str] = []
        for idx, upload in enumerate(files):
            orig_name = Path(upload.filename or f"upload_{idx}.dcm").name
            dest = Path(tmpdir) / f"{idx}_{orig_name}"
            content = await upload.read()
            dest.write_bytes(content)
            saved_files.append(str(dest))

        # If a single zip was uploaded, extract it
        if len(saved_files) == 1 and saved_files[0].lower().endswith(".zip"):
            extract_dir = Path(tmpdir) / "extracted"
            extract_dir.mkdir()
            with zipfile.ZipFile(saved_files[0], "r") as zf:
                zf.extractall(str(extract_dir))
            ingest_dir = str(extract_dir)
        else:
            ingest_dir = tmpdir

        try:
            result = ingest_dicom_directory(ingest_dir, db)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Ingestion failed: {exc}")

    # Auto-run the Stage 1 pipeline (complexity → MCsquare → gamma → ML).
    if settings.PIPELINE_AUTO_RUN and result.get("plan_id"):
        from services.pipeline import run_stage1

        background_tasks.add_task(run_stage1, result["plan_id"], False)

    return PlanIngestionResponse(**result)


@router.get("/{plan_id}/fields", response_model=list[FieldSummary])
async def get_plan_fields(plan_id: int, db: Session = Depends(get_db)):
    """Returns per-field summary for the plan."""
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    dcm_dir = Path(plan.dicom_store_path)
    rtplan_files = list(dcm_dir.glob("*.dcm"))
    if not rtplan_files:
        raise HTTPException(status_code=404, detail="DICOM files not found in store")

    fields = []
    for f in rtplan_files:
        try:
            dcm = pydicom.dcmread(str(f), stop_before_pixels=True)
            if dcm.get("Modality", "") in ("RTPLAN", "RTIBTR"):
                fields = parse_rtplan_fields(dcm)
                break
        except Exception:
            continue

    return [FieldSummary(**f) for f in fields]


@router.get("/{plan_id}", response_model=PlanSummary)
async def get_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan
