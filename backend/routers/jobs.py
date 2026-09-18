from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models.plan import Plan
from models.qa_job import QAJob
from schemas.qa_job import QAJobCreate, QAJobResponse
from services.job_control import request_cancel
from services.job_runner import SUPPORTED_JOB_TYPES, run_qa_job

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.post("", response_model=QAJobResponse)
async def create_job(payload: QAJobCreate, db: Session = Depends(get_db)):
    """Creates a QA job record (without starting it)."""
    job = QAJob(
        plan_id=payload.plan_id,
        job_type=payload.job_type,
        fraction_number=payload.fraction_number,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


@router.post("/run", response_model=QAJobResponse)
async def run_job(
    payload: QAJobCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Creates a QA job and immediately dispatches it on a background thread.
    Poll GET /api/jobs/{job_id} for progress.
    """
    if payload.job_type not in SUPPORTED_JOB_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported job type '{payload.job_type}'. "
            f"Supported: {sorted(SUPPORTED_JOB_TYPES)}",
        )
    plan = db.query(Plan).filter_by(id=payload.plan_id).first()
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    job = QAJob(
        plan_id=payload.plan_id,
        job_type=payload.job_type,
        fraction_number=payload.fraction_number,
        status="queued",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    background_tasks.add_task(run_qa_job, job.id)
    return job


@router.post("/{job_id}/cancel", response_model=QAJobResponse)
async def cancel_job(job_id: int, db: Session = Depends(get_db)):
    """Requests cooperative cancellation of a running job."""
    job = db.query(QAJob).filter_by(id=job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in ("complete", "error", "cancelled"):
        raise HTTPException(
            status_code=409, detail=f"Job already finished (status: {job.status})"
        )
    request_cancel(job_id)
    return job


@router.get("/{job_id}", response_model=QAJobResponse)
async def get_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(QAJob).filter_by(id=job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/plan/{plan_id}", response_model=list[QAJobResponse])
async def list_plan_jobs(plan_id: int, db: Session = Depends(get_db)):
    return (
        db.query(QAJob)
        .filter_by(plan_id=plan_id)
        .order_by(QAJob.id.desc())
        .all()
    )
