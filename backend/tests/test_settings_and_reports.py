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


@pytest.fixture(autouse=True)
def enable_mc_mock_mode(monkeypatch):
    """Enable mock simulation mode so MC simulation runs in unit tests without CT dataset."""
    monkeypatch.setattr(settings, "MCSQUARE_SIMULATION_MODE", True)


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
    assert "Field Spatial Dose &amp; Gamma Verification (at Isocenter)" in rep_html.text
    assert "Secondary Dose &amp; Robustness Dosimetric Criteria Summary" not in rep_html.text

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


def test_secondary_dose_report_deduplication(client):
    """
    Verify that if multiple runs, criteria, or fractional scopes produce multiple
    mcSquare_vs_TPS rows per beam (e.g. 4 rows each), the secondary dose report
    deduplicates them to exactly 1 row per field.
    """
    import re
    from models.gamma_result import GammaResult

    db = SessionLocal()
    tmp = tempfile.mkdtemp(prefix="test_dedup_report_")
    write_synthetic_dicom_set(tmp, n_fields=2)
    result = ingest_dicom_directory(tmp, db)
    plan_id = result["plan_id"]

    # Insert 4 gamma rows for Beam 1 and 4 gamma rows for Beam 2
    # simulating 4 historical runs / criteria (e.g. 1%/1mm, 2%/2mm, 3%/3mm, 2%/1mm)
    criteria = [(1.0, 1.0), (2.0, 2.0), (3.0, 3.0), (2.0, 1.0)]
    for beam_num, name in [(1, "Field 1"), (2, "Field 2")]:
        for dd, dta in criteria:
            row = GammaResult(
                plan_id=plan_id,
                fraction_number=None,
                field_name=name,
                beam_number=beam_num,
                comparison_type="mcSquare_vs_TPS",
                dd_percent=dd,
                dta_mm=dta,
                passing_rate=95.0,
                threshold=90.0,
                passed=True,
            )
            db.add(row)

    # Also insert fractional rows that should NOT appear in pre-treatment report
    for beam_num, name in [(1, "Field 1"), (2, "Field 2")]:
        db.add(GammaResult(
            plan_id=plan_id,
            fraction_number=1,
            field_name=name,
            beam_number=beam_num,
            comparison_type="mcSquare_vs_TPS",
            dd_percent=3.0,
            dta_mm=3.0,
            passing_rate=98.0,
            threshold=90.0,
            passed=True,
        ))
    db.commit()

    # Total rows for mcSquare_vs_TPS in DB for this plan is 10 (5 per beam)
    assert db.query(GammaResult).filter_by(plan_id=plan_id, comparison_type="mcSquare_vs_TPS").count() == 10

    # Request report
    res = client.get(f"/api/reports/{plan_id}/secondary-dose")
    assert res.status_code == 200
    html = res.text

    # Verify table contains exactly 2 rows (one for each field), not 8 or 10
    match = re.search(r'Field-by-Field Secondary Dose.*?<tbody>(.*?)</tbody>', html, re.DOTALL)
    assert match is not None
    table_body = match.group(1)
    rows = re.findall(r'<tr>(.*?)</tr>', table_body, re.DOTALL)
    assert len(rows) == 2, f"Expected 2 field rows in report table, got {len(rows)}"

    db.close()


def test_secondary_dose_report_separated_dvh_plots_and_roi_selection(client):
    """
    Verify that:
    1. The secondary dose report renders separate Target and OAR plots side-by-side.
    2. Only selected ROIs are plotted when the ?rois= query parameter is specified.
    3. The entire quantitative dosimetric metrics table remains complete with all ROIs.
    """
    db = SessionLocal()
    tmp = tempfile.mkdtemp(prefix="test_dvh_separate_report_")
    write_synthetic_dicom_set(tmp, n_fields=2, include_rtstruct=True)
    result = ingest_dicom_directory(tmp, db)
    plan_id = result["plan_id"]

    run_stage1(plan_id, background=False)

    # 1. Fetch report with default ROI selection
    res_default = client.get(f"/api/reports/{plan_id}/secondary-dose")
    assert res_default.status_code == 200
    html_default = res_default.text

    # Both Target and OAR plot panels should be rendered
    assert "Target Structures" in html_default
    assert "Organs at Risk (OARs)" in html_default
    assert "PTV_High" in html_default
    assert "SpinalCord" in html_default
    # Full metrics table should contain all structures (including External)
    assert "External" in html_default
    assert "openMCsquare D95%" in html_default

    # 2. Fetch report with ONLY the target selected (ROI 1: PTV_High)
    res_target_only = client.get(f"/api/reports/{plan_id}/secondary-dose?rois=1")
    assert res_target_only.status_code == 200
    html_target = res_target_only.text

    assert "Target Structures" in html_target
    assert "PTV_High" in html_target
    # The OAR plot panel should NOT be plotted because only Target was selected
    assert "Organs at Risk (OARs)" not in html_target
    # But the FULL table MUST STILL REMAIN with all structures!
    assert "SpinalCord" in html_target  # still in table
    assert "External" in html_target    # still in table

    # 3. Fetch report with ONLY the OAR selected (ROI 2: SpinalCord)
    res_oar_only = client.get(f"/api/reports/{plan_id}/secondary-dose?rois=2")
    assert res_oar_only.status_code == 200
    html_oar = res_oar_only.text

    assert "Organs at Risk (OARs)" in html_oar
    assert "SpinalCord" in html_oar
    # Target Structures panel should not appear
    assert "Target Structures" not in html_oar
    # Full table remains
    assert "PTV_High" in html_oar
    assert "External" in html_oar

    # 4. Fetch report with BOTH Target and OAR selected (ROI 1 and 2)
    res_both = client.get(f"/api/reports/{plan_id}/secondary-dose?rois=1,2")
    assert res_both.status_code == 200
    html_both = res_both.text

    assert "Target Structures" in html_both
    assert "Organs at Risk (OARs)" in html_both
    assert "dvh-plots-grid" in html_both
    assert "PTV_High" in html_both
    assert "SpinalCord" in html_both
    # Full table remains
    assert "External" in html_both

    db.close()
