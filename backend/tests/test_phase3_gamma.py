"""
Phase 3 gamma analysis smoke test.

Generates synthetic DICOM, ingests it, runs MCsquare (mock) + log reconstruction,
then runs gamma analysis and asserts results + verdict are produced.
Run from backend/:  python -m tests.test_phase3_gamma
"""
from __future__ import annotations

import tempfile
import time

import numpy as np

from database import SessionLocal
from models.gamma_result import GammaResult
from models.qa_job import QAJob
from services.dicom_ingestor import ingest_dicom_directory
from services.gamma_analysis import compute_gamma_plane, load_plan_doses
from services.gamma_engine import gamma_2d
from services.job_runner import run_qa_job
from tests.synthetic_dicom_gen import write_synthetic_dicom_set


def _run(plan_id: int, job_type: str, db) -> QAJob:
    job = QAJob(plan_id=plan_id, job_type=job_type, status="queued")
    db.add(job)
    db.commit()
    db.refresh(job)
    run_qa_job(job.id)
    db.refresh(job)
    assert job.status == "complete", f"{job_type} -> {job.status}: {job.error_message}"
    return job


def test_gamma_engine_identity():
    """Identical doses must give 100% passing rate; a uniform shift must fail."""
    rng = np.random.default_rng(0)
    ref = rng.random((40, 40)).astype(np.float32) + 0.1
    _, rate_same = gamma_2d(ref, ref.copy(), 2.0, 2.0, 2.0)
    assert rate_same > 99.0, f"identity passing rate too low: {rate_same}"

    shifted = ref * 1.5  # 50% hot — should largely fail a 2%/2mm test
    _, rate_shift = gamma_2d(ref, shifted, 2.0, 2.0, 2.0)
    assert rate_shift < 50.0, f"large shift should fail, got {rate_shift}"
    print(f"[ok] gamma engine: identity={rate_same:.1f}%  +50%={rate_shift:.1f}%")


def main() -> None:
    test_gamma_engine_identity()

    db = SessionLocal()
    tmp = tempfile.mkdtemp(prefix="phase3_test_")
    write_synthetic_dicom_set(tmp, n_fields=2)
    result = ingest_dicom_directory(tmp, db)
    plan_id = result["plan_id"]
    print(f"[ok] Ingested plan {plan_id}")

    _run(plan_id, "mcSquare", db)
    _run(plan_id, "log_reconstruction", db)
    print("[ok] MC + log doses computed")

    doses = load_plan_doses(plan_id, db)
    assert set(doses) >= {"tps", "mcSquare", "log"}, f"missing doses: {list(doses)}"

    t0 = time.time()
    gamma_job = _run(plan_id, "gamma", db)
    dt = time.time() - t0
    print(f"[ok] Gamma job complete in {dt:.2f}s")

    rows = (
        db.query(GammaResult)
        .filter(GammaResult.plan_id == plan_id, GammaResult.fraction_number.is_(None))
        .all()
    )
    assert len(rows) == 3, f"expected 3 comparisons, got {len(rows)}"
    for r in rows:
        print(f"     {r.comparison_type}: {r.passing_rate:.1f}% "
              f"(thr {r.threshold:.0f}%, {r.dd_percent}%/{r.dta_mm}mm) "
              f"-> {'PASS' if r.passed else 'FAIL'}")

    from models.plan import Plan
    plan = db.query(Plan).filter_by(id=plan_id).first()
    print(f"[ok] Plan verdict: {plan.qa_status}")
    assert plan.qa_status in ("pass", "flagged", "measure_needed")

    # Live per-plane gamma (viewer path)
    gmap, rate = compute_gamma_plane(plan_id, "mcSquare_vs_TPS", doses["tps"].max_dose_plane_index(), db)
    print(f"[ok] Live gamma plane: shape={gmap.shape} passing={rate:.1f}%")

    print("\nPhase 3 gamma smoke test PASSED")
    db.close()


if __name__ == "__main__":
    main()
