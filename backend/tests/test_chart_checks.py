import json
import tempfile
from starlette.testclient import TestClient
import main
from config import settings
from database import SessionLocal
from models.fraction import Fraction
from models.gamma_result import GammaResult
from models.plan import Plan
from services.auth_service import create_session_token
from services.chart_check_service import (
    get_plan_chart_checks,
    record_chart_check,
    build_chart_check_report_html,
    delete_chart_check,
)
from services.dicom_ingestor import ingest_dicom_directory
from services.report_generator import build_report_html
from tests.synthetic_dicom_gen import write_synthetic_dicom_set


def test_fractional_qa_report_and_chart_checks():
    db = SessionLocal()
    tmp = tempfile.mkdtemp(prefix="test_chart_checks_")
    write_synthetic_dicom_set(tmp, n_fields=2)
    result = ingest_dicom_directory(tmp, db)
    plan_id = result["plan_id"]
    plan = db.query(Plan).filter_by(id=plan_id).first()
    plan.number_of_fractions = 30
    db.commit()

    # Add delivered fractions (e.g. Fx 1, 2, 3)
    f1 = Fraction(plan_id=plan_id, fraction_number=1, delivery_type="curative", is_interrupted=False, machine="ProNova G1")
    f2 = Fraction(plan_id=plan_id, fraction_number=2, delivery_type="curative", is_interrupted=False, machine="ProNova G1")
    f3 = Fraction(plan_id=plan_id, fraction_number=3, delivery_type="curative", is_interrupted=False, machine="ProNova G1")
    db.add_all([f1, f2, f3])

    # Add gamma results for fractions
    gr1 = GammaResult(
        plan_id=plan_id,
        fraction_number=1,
        field_name="Field 1",
        beam_number=1,
        comparison_type="log_vs_Rx",
        dd_percent=2.0,
        dta_mm=2.0,
        passing_rate=98.5,
        threshold=90.0,
        passed=True,
    )
    gr2 = GammaResult(
        plan_id=plan_id,
        fraction_number=1,
        field_name="Field 2",
        beam_number=2,
        comparison_type="log_vs_Rx",
        dd_percent=2.0,
        dta_mm=2.0,
        passing_rate=99.1,
        threshold=90.0,
        passed=True,
    )
    gr3 = GammaResult(
        plan_id=plan_id,
        fraction_number=2,
        field_name="Composite",
        comparison_type="log_vs_Rx",
        dd_percent=2.0,
        dta_mm=2.0,
        passing_rate=97.8,
        threshold=90.0,
        passed=True,
    )
    gr4 = GammaResult(
        plan_id=plan_id,
        fraction_number=3,
        field_name="Composite",
        comparison_type="log_vs_Rx",
        dd_percent=2.0,
        dta_mm=2.0,
        passing_rate=98.0,
        threshold=90.0,
        passed=True,
    )
    db.add_all([gr1, gr2, gr3, gr4])
    db.commit()

    # 1. Test Streamlined Fractional QA Report
    html = build_report_html(plan_id, db)
    assert "Fractional QA & Delivery Verification Report" in html or "PSQA report" in html
    assert "Fractional Delivery Assurance Overview" in html
    assert "Fractional Gamma Passing Rate Trend" in html
    assert "Field-by-Field Breakdown" in html
    assert "Field Spatial Dose &amp; Gamma Verification (at Isocenter)" in html
    assert "Secondary Dose &amp; Robustness" not in html
    assert "ELECTRONIC OMR INTEGRATION" in html
    # Ensure physical sign-off signature box is removed
    assert 'class="signoff"' not in html
    assert "Physicist sign-off" not in html
    print("✓ Test 1: Streamlined Fractional QA Report generated correctly with field spatial dose and without secondary dose/sign-off")

    # 2. Test Chart Check Tally Initial State
    tally = get_plan_chart_checks(plan_id, db)
    assert tally["total_completed"] == 0
    assert tally["next_due"]["check_number"] == 1
    assert tally["next_due"]["suggested_start_fraction"] == 1
    assert tally["next_due"]["suggested_end_fraction"] == 3
    print("✓ Test 2: Initial chart check tally correctly suggests Fx 1–3")

    # 3. Record First Chart Check (Fractions 1–3)
    check1 = record_chart_check(
        plan_id=plan_id,
        fraction_numbers=[1, 2, 3],
        reviewer_name="Dr. Jane Doe, DABR",
        notes="All table shifts and spot alignments within TG-218 tolerances.",
        checklist=tally["default_checklist"],
        db=db,
    )
    assert check1.check_number == 1
    assert check1.fractions_covered == "1–3"

    # 4. Check Updated Tally (Should suggest next 5 fractions: 4–8)
    tally2 = get_plan_chart_checks(plan_id, db)
    assert tally2["total_completed"] == 1
    assert tally2["next_due"]["check_number"] == 2
    assert tally2["next_due"]["suggested_start_fraction"] == 4
    assert tally2["next_due"]["suggested_end_fraction"] == 8
    print("✓ Test 3: Tally updated to 1 completed; next check correctly advances to Fx 4–8")

    # 5. Build Chart Check HTML Report
    cc_html = build_chart_check_report_html(
        plan_id=plan_id,
        fraction_numbers=[1, 2, 3],
        db=db,
        check_number=1,
        reviewer_name="Dr. Jane Doe, DABR",
        notes="All table shifts and spot alignments within TG-218 tolerances.",
        checklist=tally["default_checklist"],
    )
    assert "Weekly Physics Chart Check Report" in cc_html
    assert "6-DoF Table Positions" in cc_html
    assert "Machine Delivery Logs" in cc_html
    assert "Offline Image Review" in cc_html
    assert "Patient Chart Document Review Checklist" in cc_html
    assert "Dr. Jane Doe, DABR" in cc_html
    assert "ELECTRONIC OMR DOCUMENT" in cc_html
    # Ensure no physical signature block
    assert "class=\"sign-line\"" not in cc_html
    print("✓ Test 4: Chart check report generated with 6-DoF couch, logs, OIR, documents, and OMR note")

    # 6. Test HTTP Endpoints with Authenticated Client
    token = create_session_token("admin")
    client = TestClient(main.app, cookies={settings.AUTH_SESSION_COOKIE: token})

    # GET chart-checks
    res = client.get(f"/api/reports/{plan_id}/chart-checks")
    assert res.status_code == 200
    data = res.json()
    assert data["total_completed"] == 1
    assert len(data["checks"]) == 1

    # GET chart-check-report HTML
    res_rep = client.get(f"/api/reports/{plan_id}/chart-check-report?check_id={check1.id}")
    assert res_rep.status_code == 200
    assert "Physics Chart Check" in res_rep.text

    # GET chart-check-report PDF
    res_pdf = client.get(f"/api/reports/{plan_id}/chart-check-report?check_id={check1.id}&format=pdf")
    assert res_pdf.status_code == 200
    assert res_pdf.headers.get("content-type") == "application/pdf"
    assert len(res_pdf.content) > 1000
    print("✓ Test 5: HTTP endpoints and PDF generation verified")

    # 7. Record Second Chart Check (Fractions 4–8)
    check2 = record_chart_check(
        plan_id=plan_id,
        fraction_numbers=[4, 5, 6, 7, 8],
        reviewer_name="Dr. Jane Doe, DABR",
        notes="Continuing weekly check 2.",
        checklist=tally["default_checklist"],
        db=db,
    )
    assert check2.check_number == 2
    tally3 = get_plan_chart_checks(plan_id, db)
    assert tally3["total_completed"] == 2
    assert tally3["next_due"]["check_number"] == 3
    assert tally3["next_due"]["suggested_start_fraction"] == 9
    assert tally3["next_due"]["suggested_end_fraction"] == 13
    print("✓ Test 6: Running tally correctly tracks second check and advances to Fx 9–13")

    # 8. Delete check
    del_ok = delete_chart_check(plan_id, check2.id, db)
    assert del_ok is True
    tally4 = get_plan_chart_checks(plan_id, db)
    assert tally4["total_completed"] == 1

    db.close()


def test_oir_sign_offs_and_chart_check_section_3():
    from services.oir_service import get_chart_checks, save_chart_check

    db = SessionLocal()
    tmp = tempfile.mkdtemp(prefix="test_oir_chart_check_")
    write_synthetic_dicom_set(tmp, n_fields=2)
    result = ingest_dicom_directory(tmp, db)
    plan_id = result["plan_id"]
    plan = db.query(Plan).filter_by(id=plan_id).first()
    plan.number_of_fractions = 10
    db.commit()

    f1 = Fraction(plan_id=plan_id, fraction_number=1, delivery_type="curative", machine="ProNova G1")
    f2 = Fraction(plan_id=plan_id, fraction_number=2, delivery_type="curative", machine="ProNova G1")
    db.add_all([f1, f2])
    db.commit()

    token = create_session_token("admin")
    client = TestClient(main.app, cookies={settings.AUTH_SESSION_COOKIE: token})

    # 1. Medical Physicist signs off Fx 1
    resp_phys = client.post(
        f"/api/oir/{plan_id}/sign-off",
        json={
            "fraction_number": 1,
            "reviewer_name": "Adam Holt, MS, DABR",
            "status": "pass",
            "notes": "Target coverage intact, table shifts within 1.5 mm.",
            "shifts_verified": True,
            "contours_verified": True,
            "reviewer_role": "physicist",
        },
    )
    assert resp_phys.status_code == 200
    phys_data = resp_phys.json()
    assert phys_data["physics_reviewed"] is True
    assert phys_data["physics_reviewer"] == "Adam Holt, MS, DABR"
    assert phys_data["physician_reviewed"] is False

    # 2. Check chart check report before Physician signs off
    html_1 = build_chart_check_report_html(
        plan_id=plan_id,
        fraction_numbers=[1, 2],
        db=db,
        check_number=1,
    )
    # Ensure alignment shifts and adaptive dose gamma columns are removed
    assert "Alignment Shifts (Lat / Long / Vert)" not in html_1
    assert "Adaptive Dose Gamma" not in html_1
    # Ensure Physics Review / Sign-Off and Physician Sign-Off columns exist
    assert "Physics Review / Sign-Off" in html_1
    assert "Physician Sign-Off" in html_1
    assert "Adam Holt, MS, DABR" in html_1
    assert "PHYSICS REVIEWED (MD PENDING)" in html_1
    assert "PENDING PHYSICIAN" in html_1
    assert "PENDING REVIEW" in html_1  # For Fx 2

    # 3. Radiation Oncologist (Physician) signs off Fx 1
    resp_md = client.post(
        f"/api/oir/{plan_id}/sign-off",
        json={
            "fraction_number": 1,
            "reviewer_name": "Dr. Sarah Lin, MD",
            "status": "pass",
            "notes": "Clinically reviewed and approved.",
            "reviewer_role": "physician",
        },
    )
    assert resp_md.status_code == 200
    md_data = resp_md.json()
    assert md_data["physics_reviewed"] is True
    assert md_data["physics_reviewer"] == "Adam Holt, MS, DABR"
    assert md_data["physician_reviewed"] is True
    assert md_data["physician_reviewer"] == "Dr. Sarah Lin, MD"

    # 4. Check chart check report after Physician signs off
    html_2 = build_chart_check_report_html(
        plan_id=plan_id,
        fraction_numbers=[1, 2],
        db=db,
        check_number=1,
    )
    assert "Dr. Sarah Lin, MD" in html_2
    assert "COMPLETE &bull; APPROVED" in html_2

    # 5. Physician also signs off Fx 2
    save_chart_check(
        plan_id=plan_id,
        fraction_number=2,
        reviewer_name="Dr. Sarah Lin, MD",
        status="pass",
        notes="Approved Fx 2",
        reviewer_role="physician",
    )
    save_chart_check(
        plan_id=plan_id,
        fraction_number=2,
        reviewer_name="Adam Holt, MS, DABR",
        status="pass",
        notes="Physics ok",
        reviewer_role="physicist",
    )

    # 6. Both Fx 1 and 2 are signed off by Physician -> oir_physician_approved should be verified in chart check
    check = record_chart_check(
        plan_id=plan_id,
        fraction_numbers=[1, 2],
        reviewer_name="Adam Holt, MS",
        notes="Full check",
        checklist=None,
        db=db,
    )
    cl = json.loads(check.checklist_json)
    oir_item = next((item for item in cl if item["key"] == "oir_physician_approved"), None)
    assert oir_item is not None
    assert oir_item["verified"] is True
    assert check.oir_status == "pass"

    # 7. Test backward-compatibility normalization of legacy record
    from services.oir_service import _normalize_oir_record
    legacy = {
        "id": "cc_legacy",
        "fraction_number": 3,
        "reviewer_name": "Legacy Reviewer",
        "status": "acceptable",
        "notes": "Old record",
        "timestamp": "2026-01-01T00:00:00Z",
    }
    normalized = _normalize_oir_record(legacy)
    assert normalized["physics_reviewed"] is True
    assert normalized["physics_reviewer"] == "Legacy Reviewer"
    assert normalized["physics_status"] == "acceptable"
    assert normalized["physician_reviewed"] is False

    db.close()

