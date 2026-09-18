"""
Test suite for Settings API, custom pathway validation, autodetection,
and Secondary Dose & Gamma Analysis report generation (HTML + PDF).
"""
from __future__ import annotations

import tempfile
import pytest
from fastapi.testclient import TestClient

from main import app
from database import SessionLocal
from tests.synthetic_dicom_gen import write_synthetic_dicom_set
from services.dicom_ingestor import ingest_dicom_directory
from services.pipeline import run_stage1


from config import settings
from services.auth_service import create_session_token


@pytest.fixture(scope="module")
def client():
    token = create_session_token("admin")
    return TestClient(app, cookies={settings.AUTH_SESSION_COOKIE: token})


def test_settings_endpoints(client):
    # 1. Get settings
    res = client.get("/api/settings")
    assert res.status_code == 200
    data = res.json()
    assert "paths" in data
    assert "system" in data
    assert "autodetect" in data

    # 2. Validate path
    val_res = client.post("/api/settings/validate-path", json={"path": "./MCsquare", "kind": "dir"})
    assert val_res.status_code == 200
    val_data = val_res.json()
    assert "exists" in val_data

    # 3. Autodetect
    auto_res = client.post("/api/settings/autodetect")
    assert auto_res.status_code == 200
    auto_data = auto_res.json()
    assert "mcsquare_homes" in auto_data
    assert "binaries" in auto_data

    # 4. Save settings
    save_res = client.post("/api/settings", json={"paths": {"mcsquare_home": "./MCsquare"}})
    assert save_res.status_code == 200
    assert save_res.json().get("status") == "success"


def test_secondary_dose_report_generation(client):
    db = SessionLocal()
    tmp = tempfile.mkdtemp(prefix="test_sec_report_")
    write_synthetic_dicom_set(tmp, n_fields=2)
    result = ingest_dicom_directory(tmp, db)
    plan_id = result["plan_id"]

    run_stage1(plan_id, background=False)

    # Test full QA report HTML
    rep_html = client.get(f"/api/reports/{plan_id}")
    assert rep_html.status_code == 200
    assert "PSQA report" in rep_html.text

    # Test Secondary Dose HTML Report
    sec_html = client.get(f"/api/reports/{plan_id}/secondary-dose")
    assert sec_html.status_code == 200
    assert "Secondary Dose Calculation" in sec_html.text
    assert "Gamma Analysis" in sec_html.text
    assert "openMCsquare Robustness Analysis" in sec_html.text
    assert "openMCsquare D95%" in sec_html.text

    # Test Secondary Dose PDF Report
    sec_pdf = client.get(f"/api/reports/{plan_id}/secondary-dose?format=pdf")
    assert sec_pdf.status_code == 200
    assert sec_pdf.headers.get("content-type") == "application/pdf"
    assert len(sec_pdf.content) > 1000

    db.close()
