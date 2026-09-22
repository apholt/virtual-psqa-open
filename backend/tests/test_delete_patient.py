"""
Test suite for deleting patient data with full cascade cleanup and safeguards.
"""
from __future__ import annotations

import uuid
import pytest
from pydicom.uid import generate_uid
from fastapi.testclient import TestClient
from sqlalchemy import text

from main import app
from database import SessionLocal
from config import settings
from models.patient import Patient
from models.plan import Plan
from models.qa_job import QAJob
from models.gamma_result import GammaResult
from models.fraction import Fraction
from models.audit_log import AuditLog
from services.auth_service import create_session_token


@pytest.fixture
def client():
    token = create_session_token("admin")
    return TestClient(app, cookies={settings.AUTH_SESSION_COOKIE: token})


def test_delete_patient_success(client):
    db = SessionLocal()
    rand = uuid.uuid4().hex[:8]
    pid = f"PT_DEL_{rand}"
    pat = Patient(patient_id=pid, patient_name=f"DeleteTest_{rand}")
    db.add(pat)
    db.commit()
    db.refresh(pat)

    # Create associated plan
    plan = Plan(
        patient_id=pat.id,
        plan_label=f"Plan_{rand}",
        plan_name=f"Plan_{rand}",
        number_of_fields=2,
        dicom_store_path=f"./data/dicom_store/{pid}/dummy_uid",
        rtplan_uid=generate_uid(),
        qa_status="pass",
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)

    # Create associated job, gamma result, fraction
    job = QAJob(plan_id=plan.id, job_type="mcsquare_simulation", status="completed")
    db.add(job)
    gamma = GammaResult(
        plan_id=plan.id,
        field_name="Field1",
        comparison_type="mc_vs_tps",
        dd_percent=3.0,
        dta_mm=3.0,
        passing_rate=98.5,
        threshold=90.0,
        passed=True,
    )
    db.add(gamma)
    frac = Fraction(plan_id=plan.id, fraction_number=1, qa_status="verified")
    db.add(frac)

    # Create a beam delivery to test machine-history retention
    db.execute(
        text(
            "INSERT INTO beam_deliveries (plan_id, fraction_number, beam_name, created_at) "
            "VALUES (:pid, 1, 'Beam1', datetime('now'))"
        ),
        {"pid": plan.id},
    )
    db.commit()

    saved_pat_id = pat.id
    saved_plan_id = plan.id
    db.close()

    # Now delete via API
    res = client.delete(f"/api/patients/{saved_pat_id}")
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["patient_id"] == pid
    assert data["plans_deleted"] == 1

    # Verify DB state
    db2 = SessionLocal()
    try:
        assert db2.query(Patient).filter_by(id=saved_pat_id).first() is None
        assert db2.query(Plan).filter_by(id=saved_plan_id).first() is None
        assert db2.query(QAJob).filter_by(plan_id=saved_plan_id).first() is None
        assert db2.query(GammaResult).filter_by(plan_id=saved_plan_id).first() is None
        assert db2.query(Fraction).filter_by(plan_id=saved_plan_id).first() is None

        # Verify beam delivery was retained with negated plan_id
        row = db2.execute(
            text("SELECT plan_id, retained_at FROM beam_deliveries WHERE plan_id = :neg_id"),
            {"neg_id": -saved_plan_id},
        ).fetchone()
        assert row is not None
        assert row[0] == -saved_plan_id

        # Verify HIPAA audit log
        audit = (
            db2.query(AuditLog)
            .filter(AuditLog.target_type == "patient", AuditLog.target_id == pid)
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert audit is not None
        assert "PATIENT" in audit.action
    finally:
        db2.close()


def test_delete_patient_not_found(client):
    res = client.delete("/api/patients/99999999")
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()


def test_delete_patient_active_job_conflict(client):
    db = SessionLocal()
    rand = uuid.uuid4().hex[:8]
    pid = f"PT_ACTIVE_{rand}"
    pat = Patient(patient_id=pid, patient_name=f"ActiveJobTest_{rand}")
    db.add(pat)
    db.commit()
    db.refresh(pat)

    plan = Plan(
        patient_id=pat.id,
        plan_label=f"Plan_Active_{rand}",
        plan_name=f"Plan_Active_{rand}",
        number_of_fields=1,
        dicom_store_path=f"./data/dicom_store/{pid}/uid",
        rtplan_uid=generate_uid(),
        qa_status="running",
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)

    active_job = QAJob(plan_id=plan.id, job_type="mcsquare_simulation", status="running")
    db.add(active_job)
    db.commit()

    saved_pat_id = pat.id
    saved_plan_id = plan.id
    saved_job_id = active_job.id
    db.close()

    try:
        res = client.delete(f"/api/patients/{saved_pat_id}")
        assert res.status_code == 409
        assert "running/queued" in res.json()["detail"]

        # Verify patient was NOT deleted
        db2 = SessionLocal()
        try:
            assert db2.query(Patient).filter_by(id=saved_pat_id).first() is not None
        finally:
            db2.close()
    finally:
        # Cleanup
        db3 = SessionLocal()
        try:
            db3.query(QAJob).filter_by(id=saved_job_id).delete()
            db3.query(Plan).filter_by(id=saved_plan_id).delete()
            db3.query(Patient).filter_by(id=saved_pat_id).delete()
            db3.commit()
        finally:
            db3.close()


def test_delete_patient_by_string_id(client):
    db = SessionLocal()
    rand = uuid.uuid4().hex[:8]
    pid = f"STR_ID_{rand}"
    pat = Patient(patient_id=pid, patient_name=f"StringIdTest_{rand}")
    db.add(pat)
    db.commit()
    db.close()

    res = client.delete(f"/api/patients/{pid}")
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["patient_id"] == pid

    db2 = SessionLocal()
    try:
        assert db2.query(Patient).filter_by(patient_id=pid).first() is None
    finally:
        db2.close()
