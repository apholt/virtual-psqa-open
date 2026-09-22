import logging
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
from services.dicom_ingestor import (
    ingest_dicom_directory,
    parse_rtplan_fields,
    ingest_rtrecord_files,
    RTRECORD_SOP_CLASSES,
    RTRECORD_MODALITIES,
)
import pydicom

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/plans", tags=["plans"])


@router.post("/upload", response_model=PlanIngestionResponse)
async def upload_dicom(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """
    Accepts multipart upload of multiple .dcm files or .zip archives.
    Saves to a temp directory, unpacks archives, runs ingestion, returns summary.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        saved_files: list[str] = []
        for idx, upload in enumerate(files):
            orig_name = Path(upload.filename or f"upload_{idx}.dcm").name
            dest = Path(tmpdir) / f"{idx}_{orig_name}"
            content = await upload.read()
            dest.write_bytes(content)
            saved_files.append(str(dest))

        # Unpack any uploaded zip archives
        for sf in list(saved_files):
            if sf.lower().endswith(".zip"):
                try:
                    extract_dir = Path(tmpdir) / f"extracted_{Path(sf).stem}"
                    extract_dir.mkdir(exist_ok=True)
                    with zipfile.ZipFile(sf, "r") as zf:
                        zf.extractall(str(extract_dir))
                except Exception as ze:
                    logger.warning(f"Could not extract zip archive {sf}: {ze}")

        ingest_dir = tmpdir

        try:
            result = ingest_dicom_directory(ingest_dir, db)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Ingestion failed: {exc}")

    # Auto-run pipelines (Stage 1 for plans, Stage 2 for standalone records)
    if settings.PIPELINE_AUTO_RUN:
        if result.get("is_all_duplicates"):
            logger.info(f"Skipping pipeline auto-run: all uploaded files were duplicates.")
        elif result.get("is_record_only"):
            from services.pipeline import run_stage2

            for pid, frac_num in result.get("updated_fractions", []):
                plan = db.query(Plan).filter_by(id=pid).first()
                if plan and plan.qa_status != "pending_plan":
                    background_tasks.add_task(run_stage2, pid, frac_num, False)
        else:
            plan_ids = result.get("plan_ids") or ([result["plan_id"]] if result.get("plan_id") else [])
            if plan_ids:
                from services.pipeline import run_stage1

                for pid in plan_ids:
                    plan = db.query(Plan).filter_by(id=pid).first()
                    if plan and plan.qa_status != "pending_plan":
                        background_tasks.add_task(run_stage1, pid, False)

    return PlanIngestionResponse(**result)


@router.post("/{plan_id}/upload-records", response_model=PlanIngestionResponse)
@router.post("/{plan_id}/upload-record", response_model=PlanIngestionResponse)
async def upload_plan_records(
    plan_id: int,
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """
    Accepts multipart upload of one or more RTRecord .dcm files or a .zip archive,
    specifically targeted for the given plan_id.
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    with tempfile.TemporaryDirectory() as tmpdir:
        saved_files: list[str] = []
        for idx, upload in enumerate(files):
            orig_name = Path(upload.filename or f"upload_{idx}.dcm").name
            dest = Path(tmpdir) / f"{idx}_{orig_name}"
            content = await upload.read()
            dest.write_bytes(content)
            saved_files.append(str(dest))

        # Unpack any uploaded zip archives
        for sf in list(saved_files):
            if sf.lower().endswith(".zip"):
                try:
                    extract_dir = Path(tmpdir) / f"extracted_{Path(sf).stem}"
                    extract_dir.mkdir(exist_ok=True)
                    with zipfile.ZipFile(sf, "r") as zf:
                        zf.extractall(str(extract_dir))
                except Exception as ze:
                    logger.warning(f"Could not extract zip archive {sf}: {ze}")

        search_dir = Path(tmpdir)

        # Collect candidate DICOM files
        record_paths: list[str] = []
        for p in search_dir.rglob("*"):
            if p.is_file() and not p.name.startswith(".") and not p.name.lower().endswith(".zip"):
                try:
                    d = pydicom.dcmread(str(p), stop_before_pixels=True, force=True)
                    mod = str(getattr(d, "Modality", "")).upper()
                    sop_class = str(getattr(d, "SOPClassUID", ""))
                    if mod in RTRECORD_MODALITIES or sop_class in RTRECORD_SOP_CLASSES:
                        record_paths.append(str(p))
                    elif mod not in ("CT", "RTDOSE", "RTPLAN", "RTSTRUCT", "REG") and sop_class not in ("1.2.840.10008.5.1.4.1.1.481.2", "1.2.840.10008.5.1.4.1.1.481.3", "1.2.840.10008.5.1.4.1.1.481.5", "1.2.840.10008.5.1.4.1.1.481.8"):
                        record_paths.append(str(p))
                except Exception:
                    pass

        if not record_paths:
            raise HTTPException(
                status_code=422,
                detail="No readable RTRecord (.dcm) files found in upload."
            )

        try:
            result = ingest_rtrecord_files(record_paths, db, target_plan_id=plan.id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"RT Record ingestion failed: {exc}")

    if settings.PIPELINE_AUTO_RUN and plan.qa_status != "pending_plan":
        from services.pipeline import run_stage2

        for pid, frac_num in result.get("updated_fractions", []):
            background_tasks.add_task(run_stage2, pid, frac_num, False)

    return PlanIngestionResponse(**result)


@router.get("/{plan_id}/fields", response_model=list[FieldSummary])
async def get_plan_fields(plan_id: int, db: Session = Depends(get_db)):
    """Returns per-field summary for the plan."""
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    dcm_dir = Path(plan.dicom_store_path)
    rtplan_files = [p for p in dcm_dir.rglob("*") if p.is_file() and not p.name.startswith(".")]
    if not rtplan_files:
        raise HTTPException(status_code=404, detail="DICOM files not found in store")

    fields = []
    for f in rtplan_files:
        try:
            dcm = pydicom.dcmread(str(f), stop_before_pixels=True)
            mod = str(dcm.get("Modality", "")).upper()
            sop_class = str(getattr(dcm, "SOPClassUID", ""))
            if (mod == "RTPLAN" or sop_class in ("1.2.840.10008.5.1.4.1.1.481.5", "1.2.840.10008.5.1.4.1.1.481.8")) and mod not in RTRECORD_MODALITIES and sop_class not in RTRECORD_SOP_CLASSES:
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


@router.get("/{plan_id}/dose-status")
async def get_plan_dose_status_endpoint(plan_id: int, db: Session = Depends(get_db)):
    """Returns detailed status of TPS RTDOSE files (plan-level & per-beam) for the plan."""
    from services.gamma_analysis import check_plan_dose_status
    try:
        return check_plan_dose_status(plan_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception(f"Error checking dose status for plan {plan_id}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


def _bg_recalculate_gamma(plan_id: int):
    from database import SessionLocal
    from services.gamma_analysis import run_gamma_analysis
    from services.pipeline import persist_gate
    session = SessionLocal()
    try:
        run_gamma_analysis(plan_id, session)
        persist_gate(plan_id)
    except Exception as exc:
        logger.warning(f"Background gamma recalculation for plan {plan_id} failed: {exc}")
    finally:
        session.close()


@router.post("/{plan_id}/upload-doses")
@router.post("/{plan_id}/upload-dicom")
async def upload_plan_doses(
    plan_id: int,
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    recalculate: bool = True,
    db: Session = Depends(get_db),
):
    """
    Uploads missing or supplemental RTDOSE (or other plan DICOM) files directly to this plan's
    DICOM store, clears caches, and optionally triggers a background gamma recalculation.
    """
    import hashlib
    from services.gamma_analysis import check_plan_dose_status, clear_dose_caches, _mc_output_dir
    from dicom.rtdose_parser import clean_store_duplicates

    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    dest_dir = Path(plan.dicom_store_path)
    dest_dir.mkdir(parents=True, exist_ok=True)

    saved_files: list[str] = []
    ignored_duplicates: list[str] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        raw_files = []
        for idx, upload in enumerate(files):
            orig_name = Path(upload.filename or f"upload_{idx}.dcm").name
            dest = Path(tmpdir) / f"{idx}_{orig_name}"
            content = await upload.read()
            dest.write_bytes(content)
            raw_files.append(dest)

        # Unpack any zip archives
        for rf in list(raw_files):
            if rf.suffix.lower() == ".zip":
                try:
                    extract_dir = Path(tmpdir) / f"extracted_{rf.stem}"
                    extract_dir.mkdir(exist_ok=True)
                    with zipfile.ZipFile(rf, "r") as zf:
                        zf.extractall(str(extract_dir))
                except Exception as ze:
                    logger.warning(f"Could not extract zip {rf.name}: {ze}")

        # Gather all valid DICOM files from tmpdir
        for p in Path(tmpdir).rglob("*"):
            if p.is_file() and not p.name.startswith(".") and not p.name.lower().endswith(".zip"):
                try:
                    dcm = pydicom.dcmread(str(p), stop_before_pixels=True, force=True)
                except Exception:
                    continue

                mod = str(getattr(dcm, "Modality", "")).upper()
                sop = str(getattr(dcm, "SOPInstanceUID", "") or "")
                p_bytes = p.read_bytes()
                p_hash = hashlib.sha256(p_bytes).hexdigest()

                # Check duplicate against existing store
                is_dup = False
                for ef in dest_dir.glob("*.dcm"):
                    try:
                        ef_bytes = ef.read_bytes()
                        if hashlib.sha256(ef_bytes).hexdigest() == p_hash:
                            is_dup = True
                            break
                        edcm = pydicom.dcmread(str(ef), stop_before_pixels=True, force=True)
                        if sop and str(getattr(edcm, "SOPInstanceUID", "")) == sop:
                            is_dup = True
                            break
                    except Exception:
                        pass

                if is_dup:
                    ignored_duplicates.append(p.name)
                    continue

                # Copy to plan DICOM store
                import re
                clean_stem = re.sub(r"^\d+_", "", p.stem)
                target_filename = f"{clean_stem}.dcm"
                target_dest = dest_dir / target_filename
                counter = 1
                while target_dest.exists():
                    target_dest = dest_dir / f"{clean_stem}_{counter}.dcm"
                    counter += 1

                shutil.copy2(str(p), str(target_dest))
                saved_files.append(target_dest.name)

                # If plan-level RTDOSE was uploaded, update plan.rtdose_uid
                if mod == "RTDOSE":
                    sum_type = str(dcm.get("DoseSummationType", "") or "").upper()
                    if sum_type == "PLAN" or not plan.rtdose_uid:
                        plan.rtdose_uid = sop
                        db.commit()

    if not saved_files and not ignored_duplicates:
        raise HTTPException(status_code=422, detail="No readable DICOM files found in upload.")

    # Invalidate caches
    clear_dose_caches()
    clean_store_duplicates(str(dest_dir))

    # Evaluate updated dose status
    status = check_plan_dose_status(plan_id, db)

    # If MC results exist and recalculate requested, auto-trigger gamma analysis
    recalculated = False
    mc_dir = _mc_output_dir(plan_id)
    if recalculate and mc_dir.exists() and (mc_dir / "mc_dose.npz").is_file():
        logger.info(f"Re-running gamma analysis for plan {plan_id} after dose file upload...")
        background_tasks.add_task(_bg_recalculate_gamma, plan_id)
        recalculated = True

    return {
        "success": True,
        "plan_id": plan_id,
        "saved_files": saved_files,
        "files_saved": saved_files,
        "ignored_duplicates": ignored_duplicates,
        "files_skipped": ignored_duplicates,
        "total_saved": len(saved_files),
        "dose_status": status,
        "recalculated": recalculated,
        "gamma_recalculated": recalculated,
        "message": f"Successfully added {len(saved_files)} file(s) to plan {plan.plan_label}."
    }
