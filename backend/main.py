"""
Virtual PSQA -- FastAPI application entry point.

Binds to 0.0.0.0:8000 so any LAN machine can reach the app at
http://<workstation-IP>:8000
"""
from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import mimetypes

# Windows' registry-based MIME lookup is sometimes missing or wrong for
# these types, which makes StaticFiles serve JS as text/plain and the
# browser refuses to execute it as a module script. Force the correct
# types explicitly so this doesn't depend on the host machine's registry.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("image/svg+xml", ".svg")

# Ensure backend directory is on the path when run directly
sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from database import Base, engine

# Create all tables on startup
import models  # noqa: F401 -- import so models are registered with Base
Base.metadata.create_all(bind=engine)

from services.auth_service import ensure_initial_admin
ensure_initial_admin()

from routers import (
    auth_router,
    dashboard,
    events,
    jobs,
    monitoring,
    oir,
    orthanc_router,
    patients,
    plan_admin,
    plans,
    reports,
    results,
    settings_router,
    synthetic_ct,
)
from middleware.auth_middleware import AuthMiddleware
from services.folder_watcher import DicomFolderWatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

watcher: DicomFolderWatcher | None = None


def _reap_orphaned_jobs() -> None:
    """
    Jobs run as in-process background threads, so any job still marked
    running/queued at startup was killed by the previous shutdown/restart and
    can never complete. Mark them error and reset plans stuck at 'running' so
    dashboard counters reflect reality. (Same logic as fix_stale_jobs.py.)
    """
    from datetime import datetime

    from database import SessionLocal
    from models.plan import Plan
    from models.qa_job import QAJob

    db = SessionLocal()
    try:
        stale = (
            db.query(QAJob)
            .filter(QAJob.status.in_(["running", "queued"]))
            .all()
        )
        for j in stale:
            j.status = "error"
            j.error_message = "Orphaned: backend restarted while job was active"
            j.completed_at = datetime.utcnow()
            plan = db.query(Plan).filter_by(id=j.plan_id).first()
            if plan and plan.qa_status == "running":
                plan.qa_status = "pending"
        if stale:
            db.commit()
            logger.warning(
                f"Reaped {len(stale)} orphaned job(s) left over from previous run: "
                f"{[j.id for j in stale]}"
            )
    except Exception as exc:  # never block startup on cleanup
        logger.error(f"Orphaned-job reaper failed: {exc}")
    finally:
        db.close()


def handle_new_dicom_folder(folder_path: str) -> None:
    """
    Called by the folder watcher when a new batch of DICOM files has settled.

    Branches on DICOM modality:
      - A batch containing an RTPlan triggers Stage 1 (plan arrival).
      - A batch containing an RT Ion Record triggers Stage 2 (fraction
        delivery): the record is matched to its plan and the log pipeline runs.

    These are independent 'if' branches (not if/elif) so a folder that happens
    to contain BOTH a plan and a record runs both pipelines rather than
    silently ignoring the record.
    """
    import shutil

    from database import SessionLocal
    from services.dicom_ingestor import classify_dicom_files, ingest_dicom_directory
    from routers.events import broadcast_new_plan_sync

    db = SessionLocal()
    try:
        classified = classify_dicom_files(folder_path)
        has_plan = bool(classified.get("RTPLAN") or classified.get("RTIBTR"))
        record_paths = classified.get("RTRECORD", [])

        if has_plan:
            # Stage 1 -- plan arrival.
            result = ingest_dicom_directory(folder_path, db)
            logger.info(
                f"Auto-ingested plan: {result['plan_label']} "
                f"for patient {result['patient_id']}"
            )
            broadcast_new_plan_sync(result)
            if settings.PIPELINE_AUTO_RUN and result.get("plan_id"):
                from services.pipeline import run_stage1

                run_stage1(result["plan_id"], background=True)

        if record_paths:
            # Stage 2 -- fraction delivery (RT Ion Record).
            import pydicom

            from models.fraction import Fraction
            from models.plan import Plan
            from services.record_matcher import (
                _find_plan_dicom,
                identify_plan_from_rtrecord,
                record_fraction_number as _record_fraction_number,
                record_delivery_type as _record_delivery_type,
            )
            from services.interruption_detector import detect_record_interruption

            for rec_path in record_paths:
                try:
                    dcm = pydicom.dcmread(rec_path, stop_before_pixels=True, force=True)
                    plan_id = identify_plan_from_rtrecord(dcm, db)
                    plan = db.query(Plan).filter_by(id=plan_id).first()
                    deliv_type = _record_delivery_type(dcm)
                    frac_num = 0 if deliv_type == "verification" else _record_fraction_number(dcm)

                    sop_uid = str(dcm.get("SOPInstanceUID", "") or "")
                    uid_suffix = sop_uid.replace(".", "_")[-12:] if sop_uid else "rec"
                    if deliv_type == "verification":
                        dest_filename = f"RTRecord_verification_{uid_suffix}.dcm"
                    else:
                        dest_filename = f"RTRecord_fx{frac_num or 1}_{uid_suffix}.dcm"
                    dest = Path(plan.dicom_store_path) / dest_filename
                    if Path(rec_path).resolve() != dest.resolve():
                        shutil.copy2(rec_path, dest)

                    fx_key = 0 if deliv_type == "verification" else (frac_num or 1)
                    frac = (
                        db.query(Fraction)
                        .filter_by(plan_id=plan_id, fraction_number=fx_key)
                        .order_by(Fraction.id.desc())
                        .first()
                    )
                    if frac is None:
                        frac = Fraction(plan_id=plan_id, fraction_number=fx_key)
                        db.add(frac)

                    raw_date = str(dcm.get("TreatmentDate", "") or dcm.get("SeriesDate", "") or dcm.get("InstanceCreationDate", "") or "")
                    parsed_date = None
                    if len(raw_date) == 8 and raw_date.isdigit():
                        try:
                            parsed_date = datetime.strptime(raw_date, "%Y%m%d").date()
                        except Exception:
                            pass

                    frac.delivery_date = parsed_date
                    frac.delivery_type = deliv_type
                    frac.rtrecord_uid = sop_uid
                    frac.rtrecord_path = str(dest)

                    # Interruption check
                    plan_dcm = _find_plan_dicom(plan.dicom_store_path)
                    interruption_info = detect_record_interruption(dcm, plan_dcm)
                    frac.is_interrupted = interruption_info["is_interrupted"]
                    frac.interruption_reason = interruption_info["interruption_reason"]
                    if interruption_info["is_interrupted"]:
                        frac.qa_status = "interrupted"
                        logger.warning(
                            f"RT Ion Record for plan {plan_id} fx {fx_key} is INTERRUPTED: {interruption_info['interruption_reason']}"
                        )
                    else:
                        frac.qa_status = "running"
                    db.commit()
                    logger.info(
                        f"RT Ion Record matched to plan {plan_id} ({deliv_type.upper()}, fx {fx_key}, interrupted={frac.is_interrupted})"
                    )

                    # RECORDCLEANUP_V1: the record is committed and safely
                    # copied into the organised store -- remove the source
                    # from the watch folder so P:\PSQA stays clean. Stage 2
                    # reads from frac.rtrecord_path (the store copy), never
                    # the watch-folder original, so deletion here is safe.
                    try:
                        _src = Path(rec_path).resolve()
                        _watch = Path(settings.DICOM_WATCH_FOLDER).resolve()
                        _in_watch = str(_src).lower().startswith(str(_watch).lower())
                        _store_ok = (
                            dest.exists()
                            and _src.exists()
                            and dest.stat().st_size == _src.stat().st_size
                        )
                        if _in_watch and _store_ok and _src != dest.resolve():
                            _src.unlink()
                            logger.info(
                                f"Removed ingested record from watch folder: {_src.name}"
                            )
                        elif not _in_watch:
                            logger.info(
                                f"Record {_src.name} not in watch folder -- leaving in place"
                            )
                    except Exception as del_exc:
                        logger.warning(
                            f"Could not remove {rec_path} from watch folder: {del_exc}"
                        )

                    if settings.PIPELINE_AUTO_RUN:
                        from services.pipeline import run_stage2

                        run_stage2(plan_id, frac_num, background=True)
                except Exception as rec_exc:
                    logger.error(
                        f"Failed to process RT Record {Path(rec_path).name}: {rec_exc}"
                    )

        ct_paths = classified.get("CT", [])
        if ct_paths and not has_plan and not record_paths:
            # Stage 3 -- SyntheticQACT arrival for an existing plan
            import pydicom
            import re
            from models.patient import Patient
            from models.qa_job import QAJob
            from services.synthetic_ct_service import ingest_synthetic_ct_files
            from services.job_runner import run_qa_job
            import threading

            try:
                sample_dcm = pydicom.dcmread(ct_paths[0], stop_before_pixels=True, force=True)
                pid = str(getattr(sample_dcm, "PatientID", "") or "")
                patient = db.query(Patient).filter_by(patient_id=pid).first()
                if patient and patient.plans:
                    latest_plan = patient.plans[-1]
                    folder_name = Path(folder_path).name.lower()
                    desc = str(getattr(sample_dcm, "SeriesDescription", "") or "").lower()
                    fx_num = 1
                    m = re.search(r"(?:fx|fraction|qact)[_\-\s]*(\d+)", f"{folder_name} {desc}")
                    if m:
                        fx_num = int(m.group(1))
                    logger.info(f"Auto-detected SyntheticQACT for plan {latest_plan.id} fx {fx_num} ({len(ct_paths)} slices)")
                    ingest_synthetic_ct_files(latest_plan.id, fx_num, ct_paths, db)
                    if settings.PIPELINE_AUTO_RUN:
                        job = QAJob(
                            plan_id=latest_plan.id,
                            job_type="synthetic_ct_mcSquare",
                            status="queued",
                            fraction_number=fx_num,
                        )
                        db.add(job)
                        db.commit()
                        db.refresh(job)
                        threading.Thread(target=run_qa_job, args=(job.id,), daemon=True).start()
                else:
                    logger.warning(f"Settled CT folder {folder_path} does not match any existing patient plan -- skipped")
            except Exception as ct_exc:
                logger.error(f"Failed to process Synthetic CT {folder_path}: {ct_exc}")
        elif not has_plan and not record_paths:
            logger.warning(
                f"Settled folder {folder_path} has no RTPlan, RT Record, or Synthetic CT -- skipped"
            )
    except Exception as exc:
        logger.error(f"Auto-ingestion failed for {folder_path}: {exc}")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global watcher
    # Clean up jobs orphaned by the previous shutdown BEFORE anything else
    # (watcher/pipeline may queue new jobs immediately).
    _reap_orphaned_jobs()
    if settings.DICOM_WATCH_FOLDER and Path(settings.DICOM_WATCH_FOLDER).exists():
        watcher = DicomFolderWatcher(
            watch_path=settings.DICOM_WATCH_FOLDER,
            ingest_callback=handle_new_dicom_folder,
        )
        watcher.start()
        logger.info(f"Folder watcher active on: {settings.DICOM_WATCH_FOLDER}")
    else:
        if settings.DICOM_WATCH_FOLDER:
            logger.warning(
                f"DICOM_WATCH_FOLDER set to '{settings.DICOM_WATCH_FOLDER}' "
                "but path does not exist -- watcher disabled."
            )
    yield
    if watcher:
        watcher.stop()


app = FastAPI(title="Virtual PSQA", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # LAN-only deployment -- no public exposure
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AuthMiddleware)

# API routers
app.include_router(auth_router.router)
app.include_router(patients.router)
app.include_router(plans.router)
app.include_router(plan_admin.router)
app.include_router(jobs.router)
app.include_router(results.router)
app.include_router(reports.router)
app.include_router(events.router)
app.include_router(dashboard.router)
app.include_router(settings_router.router)
app.include_router(monitoring.router)
app.include_router(synthetic_ct.router)
app.include_router(oir.router)
app.include_router(orthanc_router.router)


# Serve built React frontend
# Assets go to /assets (prevents collision with /api routes)
# A SPA catch-all serves index.html for all other paths (react-router handles client-side routing)
_frontend_dist = Path(__file__).parent.parent / "frontend" / "dist"
if _frontend_dist.exists():
    _assets_dir = _frontend_dist / "assets"
    if _assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")
    logger.info(f"Serving frontend from: {_frontend_dist}")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        """Serve index.html for all non-API routes so React Router works."""
        if full_path.startswith("api/") or full_path == "api":
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="API endpoint not found")
        return FileResponse(str(_frontend_dist / "index.html"))
else:
    logger.info("Frontend dist not found -- run 'npm run build' in frontend/ for production serving.")


if __name__ == "__main__":
    import uvicorn

    ssl_kwargs = {}
    if settings.SSL_ENABLED and settings.SSL_KEYFILE and settings.SSL_CERTFILE:
        cert_p = Path(settings.SSL_CERTFILE)
        key_p = Path(settings.SSL_KEYFILE)
        if not cert_p.is_absolute():
            cert_p = Path(__file__).parent / cert_p
        if not key_p.is_absolute():
            key_p = Path(__file__).parent / key_p
        if cert_p.exists() and key_p.exists():
            ssl_kwargs["ssl_certfile"] = str(cert_p)
            ssl_kwargs["ssl_keyfile"] = str(key_p)
            logger.info("Starting Virtual PSQA with HTTPS / TLS encryption active")

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        **ssl_kwargs,
    )
