"""
Unit tests for openMCsquare Robustness Analysis & DVH Predictions.
"""
from __future__ import annotations

import tempfile
import pytest
import numpy as np
from fastapi.testclient import TestClient

from config import settings
from database import SessionLocal
from main import app
from models.qa_job import QAJob
from services.dicom_ingestor import ingest_dicom_directory
from services.job_runner import run_qa_job
from services.dvh_service import (
    calculate_plan_dvh_and_robustness,
    generate_robustness_scenarios,
    compute_cumulative_dvh,
    extract_percentile_metrics,
)
from tests.synthetic_dicom_gen import write_synthetic_dicom_set


@pytest.fixture(autouse=True)
def enable_mc_mock_mode(monkeypatch):
    """Enable mock simulation mode so MC simulation runs in unit tests without CT dataset."""
    monkeypatch.setattr(settings, "MCSQUARE_SIMULATION_MODE", True)


def _run_job(plan_id: int, job_type: str, db) -> QAJob:
    job = QAJob(plan_id=plan_id, job_type=job_type, status="queued")
    db.add(job)
    db.commit()
    db.refresh(job)
    run_qa_job(job.id)
    db.refresh(job)
    assert job.status == "complete", f"{job_type} failed: {job.error_message}"
    return job


def test_dvh_percentiles_and_curve():
    """Verify exact percentile calculation and cumulative curve monotonicity."""
    dose = np.linspace(0.0, 60.0, 1000).reshape((10, 10, 10)).astype(np.float32)
    mask = np.ones((10, 10, 10), dtype=bool)

    metrics = extract_percentile_metrics(dose, mask, rx_dose=50.0)
    assert metrics["d98"] < metrics["d95"] < metrics["d50"] < metrics["d2"]
    assert metrics["d_min"] == 0.0
    assert metrics["d_max"] == 60.0
    assert 29.0 < metrics["d_mean"] < 31.0

    dose_axis = np.linspace(0.0, 65.0, 50)
    curve = compute_cumulative_dvh(dose, mask, dose_axis)
    assert curve[0] == 100.0
    assert curve[-1] == 0.0
    # Cumulative DVH must be non-increasing
    assert np.all(np.diff(curve) <= 0.0)


def test_plan_dvh_and_robustness_pipeline_and_api():
    """Test full ingestion -> MC simulation -> automatic DVH -> cached retrieval & API endpoints."""
    db = SessionLocal()
    tmp = tempfile.mkdtemp(prefix="dvh_test_")
    # Generate synthetic set WITH RTSTRUCT
    paths = write_synthetic_dicom_set(tmp, n_fields=2, include_rtstruct=True)
    assert "RS" in paths

    ingest_res = ingest_dicom_directory(tmp, db)
    plan_id = ingest_res["plan_id"]

    # Run MCsquare job (auto-triggers gamma and DVH)
    _run_job(plan_id, "mcSquare", db)

    # Fetch calculated DVH directly from service
    dvh_res = calculate_plan_dvh_and_robustness(
        plan_id,
        db,
        setup_uncertainty_mm=3.0,
        range_uncertainty_pct=3.0,
        num_scenarios=9,
    )

    assert dvh_res.plan_id == plan_id
    assert dvh_res.num_scenarios == 9
    assert len(dvh_res.scenario_names) == 9
    assert len(dvh_res.rois) >= 2  # PTV, SpinalCord, External

    # Check PTV ROI data
    ptv_roi = next((r for r in dvh_res.rois if r.is_target or "PTV" in r.name), None)
    assert ptv_roi is not None, f"No target ROI found in {[r.name for r in dvh_res.rois]}"
    assert ptv_roi.volume_cc > 0.0
    assert len(ptv_roi.dvh.dose_bins_gy) > 0
    assert len(ptv_roi.dvh.mc_nominal_volume_pct) == len(ptv_roi.dvh.dose_bins_gy)
    assert len(ptv_roi.dvh.mc_min_volume_pct) == len(ptv_roi.dvh.dose_bins_gy)
    assert len(ptv_roi.dvh.mc_max_volume_pct) == len(ptv_roi.dvh.dose_bins_gy)

    # Verify min envelope <= nominal <= max envelope (within numerical roundoff)
    min_arr = np.array(ptv_roi.dvh.mc_min_volume_pct)
    nom_arr = np.array(ptv_roi.dvh.mc_nominal_volume_pct)
    max_arr = np.array(ptv_roi.dvh.mc_max_volume_pct)

    assert np.all(min_arr <= nom_arr + 0.1)
    assert np.all(nom_arr <= max_arr + 0.1)

    # Check metric intervals
    d95 = ptv_roi.metrics.d95
    assert d95.mc_min <= d95.mc_nominal <= d95.mc_max
    assert d95.tps is not None

    # Test API Endpoints via TestClient
    from services.auth_service import create_session_token
    token = create_session_token("admin")
    client = TestClient(app, cookies={settings.AUTH_SESSION_COOKIE: token})

    # 1. GET /api/results/plan/{plan_id}/dvh
    resp = client.get(f"/api/results/plan/{plan_id}/dvh")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan_id"] == plan_id
    assert len(body["rois"]) == len(dvh_res.rois)
    assert body["has_mc_dose"] is True
    assert body["has_tps_dose"] is True

    # 2. POST /api/results/plan/{plan_id}/dvh/calculate (recalculate with 21 scenarios)
    calc_resp = client.post(
        f"/api/results/plan/{plan_id}/dvh/calculate",
        json={
            "setup_uncertainty_mm": 5.0,
            "range_uncertainty_pct": 3.5,
            "num_scenarios": 21,
        },
    )
    assert calc_resp.status_code == 200, calc_resp.text
    calc_body = calc_resp.json()
    assert calc_body["num_scenarios"] == 21
    assert len(calc_body["scenario_names"]) == 21
    assert calc_body["setup_uncertainty_mm"] == 5.0

    db.close()
