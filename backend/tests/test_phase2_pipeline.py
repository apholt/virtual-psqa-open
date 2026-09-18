"""
Phase 2 end-to-end pipeline smoke test (no HTTP, no real MCsquare binary).

Generates synthetic DICOM, ingests it, runs an MCsquare (mock) job and a log
reconstruction job through the orchestrator, and asserts dose outputs exist.
Run from the backend/ directory:  python -m tests.test_phase2_pipeline
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from database import SessionLocal
from models.qa_job import QAJob
from services.dicom_ingestor import ingest_dicom_directory
from services.dose_grid import DoseGrid
from services.job_runner import run_qa_job
from tests.synthetic_dicom_gen import write_synthetic_dicom_set


def main() -> None:
    db = SessionLocal()

    # 1. Generate + ingest synthetic DICOM
    tmp = tempfile.mkdtemp(prefix="phase2_test_")
    write_synthetic_dicom_set(tmp, n_fields=2)
    result = ingest_dicom_directory(tmp, db)
    plan_id = result["plan_id"]
    print(f"[ok] Ingested plan {plan_id}: {result['plan_label']} "
          f"({result['number_of_fields']} fields)")

    # 2. Run an MCsquare (mock) job
    mc_job = QAJob(plan_id=plan_id, job_type="mcSquare", status="queued")
    db.add(mc_job)
    db.commit()
    db.refresh(mc_job)
    run_qa_job(mc_job.id)
    db.refresh(mc_job)
    assert mc_job.status == "complete", f"MC job status={mc_job.status} err={mc_job.error_message}"
    assert mc_job.result_path and Path(mc_job.result_path).exists()
    mc_dose = DoseGrid.load(mc_job.result_path)
    print(f"[ok] MCsquare job complete → {mc_job.result_path} "
          f"shape={mc_dose.shape} max={mc_dose.max_dose:.4f} Gy")

    # 3. Run a log reconstruction job
    log_job = QAJob(plan_id=plan_id, job_type="log_reconstruction", status="queued")
    db.add(log_job)
    db.commit()
    db.refresh(log_job)
    run_qa_job(log_job.id)
    db.refresh(log_job)
    assert log_job.status == "complete", f"Log job status={log_job.status} err={log_job.error_message}"
    assert log_job.result_path and Path(log_job.result_path).exists()
    log_dose = DoseGrid.load(log_job.result_path)
    print(f"[ok] Log reconstruction complete → {log_job.result_path} "
          f"shape={log_dose.shape} max={log_dose.max_dose:.4f} Gy")

    assert mc_dose.shape == log_dose.shape, "MC and log grids must share geometry"
    print("\nPhase 2 pipeline smoke test PASSED")
    db.close()


if __name__ == "__main__":
    main()
