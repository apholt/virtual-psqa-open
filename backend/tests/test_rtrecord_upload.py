import copy
import io
import uuid
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
from config import settings
from services.auth_service import create_session_token


@pytest.fixture(scope="module", autouse=True)
def init_db():
    ensure_schema()


def _make_dummy_plan(patient_id="PT_UPLOAD_TEST", plan_uid=None):
    plan = Dataset()
    plan.Modality = "RTPLAN"
    plan.SOPClassUID = "1.2.840.10008.5.1.4.1.1.481.8"
    plan.SOPInstanceUID = plan_uid or f"1.2.826.0.1.3680043.9.7243.{uuid.uuid4().hex[:8]}.plan"
    plan.PatientID = patient_id
    plan.PatientName = "Upload^Test"
    plan.RTPlanLabel = "PLAN_UPLOAD_TEST"
    plan.RTPlanName = "PLAN_UPLOAD_TEST"

    b1 = Dataset()
    b1.BeamNumber = 1
    b1.BeamName = "BEAM_1"
    b1.FinalCumulativeMetersetWeight = 100.0

    plan.IonBeamSequence = Sequence([b1])

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = plan.SOPClassUID
    file_meta.MediaStorageSOPInstanceUID = plan.SOPInstanceUID
    file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian
    plan.file_meta = file_meta
    plan.is_little_endian = True
    plan.is_implicit_VR = False
    return plan


def _make_dummy_record(patient_id="PT_UPLOAD_TEST", plan_uid=None, fraction_number=1, is_verification=False):
    rec = Dataset()
    rec.Modality = "RTRECORD"
    rec.SOPClassUID = "1.2.840.10008.5.1.4.1.1.481.7"
    rec.SOPInstanceUID = f"1.2.826.0.1.3680043.9.7243.{uuid.uuid4().hex[:8]}.rec"
    rec.PatientID = patient_id
    rec.TreatmentDate = "20260921"
    rec.TreatmentTime = "100000"
    rec.TreatmentTerminationStatus = "NORMAL"
    rec.TreatmentStatusComment = "VERIFICATION" if is_verification else "CURATIVE"

    if plan_uid:
        ref_plan = Dataset()
        ref_plan.ReferencedSOPClassUID = "1.2.840.10008.5.1.4.1.1.481.8"
        ref_plan.ReferencedSOPInstanceUID = plan_uid
        rec.ReferencedRTPlanSequence = Sequence([ref_plan])

    b1 = Dataset()
    b1.BeamNumber = 1
    b1.BeamName = "BEAM_1:TX"
    b1.BeamTerminationStatus = "NORMAL"
    b1.TreatmentDeliveryType = "VERIFICATION" if is_verification else "TREATMENT"
    b1.CurrentFractionNumber = 0 if is_verification else fraction_number
    b1.SpecifiedPrimaryMeterset = 100.0
    b1.DeliveredPrimaryMeterset = 100.0

    rec.TreatmentSessionIonBeamSequence = Sequence([b1])

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = rec.SOPClassUID
    file_meta.MediaStorageSOPInstanceUID = rec.SOPInstanceUID
    file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian
    rec.file_meta = file_meta
    rec.is_little_endian = True
    rec.is_implicit_VR = False
    return rec


def _dataset_to_bytes(ds):
    buf = io.BytesIO()
    ds.save_as(buf)
    buf.seek(0)
    return buf.read()


def test_upload_standalone_rtrecord_to_plan_endpoint(tmp_path):
    """Test targeted upload to /api/plans/{plan_id}/upload-records."""
    client = TestClient(app)
    client.cookies[settings.AUTH_SESSION_COOKIE] = create_session_token("test_physicist")
    db = SessionLocal()

    try:
        rand = uuid.uuid4().hex[:8]
        pid = f"PT_DIRECT_{rand}"
        pat = Patient(patient_id=pid, patient_name="Direct^Test")
        db.add(pat)
        db.flush()

        store_dir = tmp_path / f"store_{rand}"
        store_dir.mkdir(parents=True, exist_ok=True)

        plan_dcm = _make_dummy_plan(patient_id=pid)
        plan_path = store_dir / "RTPlan.dcm"
        plan_dcm.save_as(str(plan_path))

        plan = Plan(
            patient_id=pat.id,
            plan_label=f"PLAN_DIRECT_{rand}",
            plan_name=f"PLAN_DIRECT_{rand}",
            rtplan_uid=plan_dcm.SOPInstanceUID,
            dicom_store_path=str(store_dir),
            number_of_fields=2,
            number_of_fractions=5,
            qa_status="pending",
        )
        db.add(plan)
        db.commit()

        # Generate RTRecord for fraction 2
        rec_dcm = _make_dummy_record(patient_id=pid, plan_uid=plan.rtplan_uid, fraction_number=2)
        rec_bytes = _dataset_to_bytes(rec_dcm)

        response = client.post(
            f"/api/plans/{plan.id}/upload-records",
            files=[("files", ("record_fx2.dcm", rec_bytes, "application/dicom"))],
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["plan_id"] == plan.id

        # Verify fraction exists in DB
        frac = db.query(Fraction).filter_by(plan_id=plan.id, fraction_number=2).first()
        assert frac is not None
        assert frac.rtrecord_uid == rec_dcm.SOPInstanceUID
        assert frac.delivery_type == "curative"
        assert not frac.is_interrupted
    finally:
        db.close()


def test_upload_standalone_rtrecord_general_upload(tmp_path):
    """Test uploading standalone RTRecord to /api/plans/upload matches existing plan."""
    client = TestClient(app)
    client.cookies[settings.AUTH_SESSION_COOKIE] = create_session_token("test_physicist")
    db = SessionLocal()

    try:
        rand = uuid.uuid4().hex[:8]
        pid = f"PT_GENERAL_{rand}"
        pat = Patient(patient_id=pid, patient_name="General^Test")
        db.add(pat)
        db.flush()

        store_dir = tmp_path / f"store_gen_{rand}"
        store_dir.mkdir(parents=True, exist_ok=True)

        plan_dcm = _make_dummy_plan(patient_id=pid)
        plan_path = store_dir / "RTPlan.dcm"
        plan_dcm.save_as(str(plan_path))

        plan = Plan(
            patient_id=pat.id,
            plan_label=f"PLAN_GEN_{rand}",
            plan_name=f"PLAN_GEN_{rand}",
            rtplan_uid=plan_dcm.SOPInstanceUID,
            dicom_store_path=str(store_dir),
            number_of_fields=2,
            number_of_fractions=10,
            qa_status="pending",
        )
        db.add(plan)
        db.commit()

        # Upload record for fraction 3
        rec_dcm = _make_dummy_record(patient_id=pid, plan_uid=plan.rtplan_uid, fraction_number=3)
        rec_bytes = _dataset_to_bytes(rec_dcm)

        response = client.post(
            "/api/plans/upload",
            files=[("files", ("record_fx3.dcm", rec_bytes, "application/dicom"))],
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["plan_id"] == plan.id

        frac = db.query(Fraction).filter_by(plan_id=plan.id, fraction_number=3).first()
        assert frac is not None
        assert frac.rtrecord_uid == rec_dcm.SOPInstanceUID
    finally:
        db.close()


def test_upload_rtrecord_without_plan_creates_provisional_plan(tmp_path):
    """Test uploading an RTRecord when NO plan exists yet for the patient."""
    client = TestClient(app)
    client.cookies[settings.AUTH_SESSION_COOKIE] = create_session_token("test_physicist")
    db = SessionLocal()

    try:
        rand = uuid.uuid4().hex[:8]
        pid = f"PT_NOPLAN_{rand}"
        planned_plan_uid = f"1.2.826.0.1.3680043.9.7243.{rand}.plan"

        # Record referencing a plan that does not exist in the database yet
        rec_dcm = _make_dummy_record(patient_id=pid, plan_uid=planned_plan_uid, fraction_number=1)
        rec_bytes = _dataset_to_bytes(rec_dcm)

        response = client.post(
            "/api/plans/upload",
            files=[("files", ("record_fx1.dcm", rec_bytes, "application/dicom"))],
        )
        assert response.status_code == 200, response.text
        data = response.json()
        prov_plan_id = data["plan_id"]

        # Check provisional plan in DB
        prov_plan = db.query(Plan).filter_by(id=prov_plan_id).first()
        assert prov_plan is not None
        assert prov_plan.qa_status == "pending_plan"
        assert prov_plan.rtplan_uid == planned_plan_uid

        frac = db.query(Fraction).filter_by(plan_id=prov_plan_id, fraction_number=1).first()
        assert frac is not None

        # Now, subsequently upload the RTPlan for this patient!
        plan_dcm = _make_dummy_plan(patient_id=pid, plan_uid=planned_plan_uid)
        plan_bytes = _dataset_to_bytes(plan_dcm)

        response2 = client.post(
            "/api/plans/upload",
            files=[("files", ("RTPlan.dcm", plan_bytes, "application/dicom"))],
        )
        assert response2.status_code == 200, response2.text
        data2 = response2.json()

        # The provisional plan should have been upgraded!
        db.refresh(prov_plan)
        assert prov_plan.qa_status != "pending_plan"
        assert prov_plan.number_of_fields == 1

        # And the fraction previously uploaded is still attached!
        frac2 = db.query(Fraction).filter_by(plan_id=prov_plan.id, fraction_number=1).first()
        assert frac2 is not None
        assert frac2.rtrecord_uid == rec_dcm.SOPInstanceUID
    finally:
        db.close()


def test_upload_accompanied_rtplan_and_rtrecord(tmp_path):
    """Test uploading RTPlan and RTRecord together in a single batch."""
    client = TestClient(app)
    client.cookies[settings.AUTH_SESSION_COOKIE] = create_session_token("test_physicist")
    db = SessionLocal()

    try:
        rand = uuid.uuid4().hex[:8]
        pid = f"PT_COMBO_{rand}"
        plan_uid = f"1.2.826.0.1.3680043.9.7243.{rand}.combo_plan"

        plan_dcm = _make_dummy_plan(patient_id=pid, plan_uid=plan_uid)
        rec_dcm = _make_dummy_record(patient_id=pid, plan_uid=plan_uid, fraction_number=1)

        plan_bytes = _dataset_to_bytes(plan_dcm)
        rec_bytes = _dataset_to_bytes(rec_dcm)

        response = client.post(
            "/api/plans/upload",
            files=[
                ("files", ("RP.dcm", plan_bytes, "application/dicom")),
                ("files", ("REC.dcm", rec_bytes, "application/dicom")),
            ],
        )
        assert response.status_code == 200, response.text
        data = response.json()
        plan_id = data["plan_id"]

        plan = db.query(Plan).filter_by(id=plan_id).first()
        assert plan is not None
        assert plan.rtplan_uid == plan_uid

        frac = db.query(Fraction).filter_by(plan_id=plan.id, fraction_number=1).first()
        assert frac is not None
        assert frac.rtrecord_uid == rec_dcm.SOPInstanceUID
    finally:
        db.close()
