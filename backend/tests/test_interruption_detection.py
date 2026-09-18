import copy
import io
import pytest
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from fastapi.testclient import TestClient

from database import SessionLocal, ensure_schema
from main import app
from models.patient import Patient
from models.plan import Plan
from models.fraction import Fraction
from services.interruption_detector import detect_record_interruption


@pytest.fixture(scope="module", autouse=True)
def init_db():
    ensure_schema()


def _make_dummy_plan():
    plan = Dataset()
    plan.Modality = "RTPLAN"
    plan.SOPClassUID = "1.2.840.10008.5.1.4.1.1.481.8"
    plan.SOPInstanceUID = "1.2.826.0.1.3680043.9.7243.plan.1"
    plan.PatientID = "TEST_INTERRUPT_PT"
    plan.PatientName = "Test^Patient"

    b1 = Dataset()
    b1.BeamNumber = 1
    b1.BeamName = "BEAM_A"
    b1.FinalCumulativeMetersetWeight = 100.0

    b2 = Dataset()
    b2.BeamNumber = 2
    b2.BeamName = "BEAM_B"
    b2.FinalCumulativeMetersetWeight = 150.0

    plan.IonBeamSequence = Sequence([b1, b2])
    return plan


def _make_dummy_record():
    rec = Dataset()
    rec.Modality = "RTRECORD"
    rec.SOPClassUID = "1.2.840.10008.5.1.4.1.1.481.7"
    rec.SOPInstanceUID = "1.2.826.0.1.3680043.9.7243.rec.1"
    rec.PatientID = "TEST_INTERRUPT_PT"
    rec.TreatmentDate = "20260917"
    rec.TreatmentTime = "120000"
    rec.TreatmentTerminationStatus = "NORMAL"
    rec.TreatmentStatusComment = "CURATIVE"

    b1 = Dataset()
    b1.BeamNumber = 1
    b1.BeamName = "BEAM_A:TX"
    b1.BeamTerminationStatus = "NORMAL"
    b1.TreatmentDeliveryType = "TREATMENT"
    b1.SpecifiedPrimaryMeterset = 100.0
    b1.DeliveredPrimaryMeterset = 100.2

    b2 = Dataset()
    b2.BeamNumber = 2
    b2.BeamName = "BEAM_B:TX"
    b2.BeamTerminationStatus = "NORMAL"
    b2.TreatmentDeliveryType = "TREATMENT"
    b2.SpecifiedPrimaryMeterset = 150.0
    b2.DeliveredPrimaryMeterset = 150.1

    rec.TreatmentSessionIonBeamSequence = Sequence([b1, b2])
    return rec


def test_normal_delivery_not_flagged():
    plan = _make_dummy_plan()
    rec = _make_dummy_record()
    res = detect_record_interruption(rec, plan)
    assert not res["is_interrupted"]
    assert res["interruption_reason"] is None


def test_beam_termination_status_interrupted():
    plan = _make_dummy_plan()
    rec = _make_dummy_record()
    rec.TreatmentSessionIonBeamSequence[0].BeamTerminationStatus = "INTERRUPTED"
    res = detect_record_interruption(rec, plan)
    assert res["is_interrupted"]
    assert "INTERRUPTED" in res["interruption_reason"]


def test_treatment_termination_status_operator():
    plan = _make_dummy_plan()
    rec = _make_dummy_record()
    rec.TreatmentTerminationStatus = "OPERATOR"
    res = detect_record_interruption(rec, plan)
    assert res["is_interrupted"]
    assert "OPERATOR" in res["interruption_reason"]


def test_meterset_shortfall_interrupted():
    plan = _make_dummy_plan()
    rec = _make_dummy_record()
    # Delivered only 30 of 100 MU on BEAM_A
    rec.TreatmentSessionIonBeamSequence[0].DeliveredPrimaryMeterset = 30.0
    res = detect_record_interruption(rec, plan)
    assert res["is_interrupted"]
    assert "prescribed MU" in res["interruption_reason"]


def test_missing_planned_beam_partial_delivery():
    plan = _make_dummy_plan()
    rec = _make_dummy_record()
    # Record only has BEAM_A, missing BEAM_B
    rec.TreatmentSessionIonBeamSequence = Sequence([rec.TreatmentSessionIonBeamSequence[0]])
    res = detect_record_interruption(rec, plan)
    assert res["is_interrupted"]
    assert "missing planned beam" in res["interruption_reason"].lower()


def test_treatment_status_comment_flagged():
    plan = _make_dummy_plan()
    rec = _make_dummy_record()
    rec.TreatmentStatusComment = "PARTIAL DELIVERY - MACHINE ABORT"
    res = detect_record_interruption(rec, plan)
    assert res["is_interrupted"]
    assert "comment indicates interrupted" in res["interruption_reason"].lower()


def test_reupload_fraction_record_endpoint(tmp_path):
    import uuid
    from config import settings
    from services.auth_service import create_session_token
    token = create_session_token("admin")
    client = TestClient(app, cookies={settings.AUTH_SESSION_COOKIE: token})
    db = SessionLocal()
    try:
        # Create test patient and plan
        uid_rand = uuid.uuid4().hex[:8]
        pid = f"PT_REUPLOAD_{uid_rand}"
        pat = Patient(patient_id=pid, patient_name="Reupload^Test")
        db.add(pat)
        db.flush()

        store_dir = tmp_path / f"plan_reupload_store_{uid_rand}"
        store_dir.mkdir(parents=True, exist_ok=True)

        plan_dcm = _make_dummy_plan()
        plan_dcm.PatientID = pat.patient_id
        plan_dcm.SOPInstanceUID = f"1.2.826.0.1.3680043.9.7243.{uid_rand}.plan"
        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = plan_dcm.SOPClassUID
        file_meta.MediaStorageSOPInstanceUID = plan_dcm.SOPInstanceUID
        file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian
        plan_file_ds = copy.deepcopy(plan_dcm)
        plan_file_ds.file_meta = file_meta
        plan_file_ds.is_little_endian = True
        plan_file_ds.is_implicit_VR = False
        plan_path = store_dir / "RTPlan.dcm"
        plan_file_ds.save_as(str(plan_path))

        plan = Plan(
            patient_id=pat.id,
            plan_label="TEST_PLAN_REUPLOAD",
            plan_name="TEST_PLAN_REUPLOAD",
            rtplan_uid=plan_dcm.SOPInstanceUID,
            dicom_store_path=str(store_dir),
            number_of_fields=2,
            number_of_fractions=10,
            qa_status="pending",
        )
        db.add(plan)
        db.flush()

        frac = Fraction(
            plan_id=plan.id,
            fraction_number=1,
            qa_status="interrupted",
            is_interrupted=True,
            interruption_reason="Initial partial delivery (Beam A interrupted)",
        )
        db.add(frac)
        db.commit()

        # Now re-upload a fixed / complete record for Fraction 1
        rec = _make_dummy_record()
        rec.PatientID = pat.patient_id
        rec.SOPInstanceUID = f"1.2.826.0.1.3680043.9.7243.{uid_rand}.rec"
        rec_meta = FileMetaDataset()
        rec_meta.MediaStorageSOPClassUID = rec.SOPClassUID
        rec_meta.MediaStorageSOPInstanceUID = rec.SOPInstanceUID
        rec_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian
        rec_file_ds = copy.deepcopy(rec)
        rec_file_ds.file_meta = rec_meta
        rec_file_ds.is_little_endian = True
        rec_file_ds.is_implicit_VR = False

        buf = io.BytesIO()
        rec_file_ds.save_as(buf)
        buf.seek(0)

        response = client.post(
            f"/api/plans/{plan.id}/fractions/1/reupload-record",
            files={"file": ("merged_record.dcm", buf, "application/dicom")},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] == "success"
        assert not data["is_interrupted"]
        assert data["interruption_reason"] is None

        # Check DB
        db.refresh(frac)
        assert not frac.is_interrupted
        assert frac.interruption_reason is None
    finally:
        db.close()
