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


def _make_dummy_record(
    patient_id="PT_UPLOAD_TEST",
    plan_uid=None,
    fraction_number=1,
    is_verification=False,
    modality="RTRECORD",
    sop_class_uid="1.2.840.10008.5.1.4.1.1.481.7",
    use_fraction_group_seq=False,
):
    rec = Dataset()
    rec.Modality = modality
    rec.SOPClassUID = sop_class_uid
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
    b1.SpecifiedPrimaryMeterset = 100.0
    b1.DeliveredPrimaryMeterset = 100.0

    if not is_verification:
        if use_fraction_group_seq:
            fg = Dataset()
            fg.ReferencedFractionNumber = fraction_number
            rec.FractionGroupSequence = Sequence([fg])
        else:
            b1.CurrentFractionNumber = fraction_number
    else:
        b1.CurrentFractionNumber = 0

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


def test_upload_proton_rtionrecord_sop_class_481_9_and_modality_rtibtr(tmp_path):
    """Test uploading standard proton RT Ion Beams Treatment Record (SOPClassUID 481.9, Modality RTIBTR)."""
    client = TestClient(app)
    client.cookies[settings.AUTH_SESSION_COOKIE] = create_session_token("test_physicist")
    db = SessionLocal()

    try:
        rand = uuid.uuid4().hex[:8]
        pid = f"PT_PROTON_{rand}"
        plan_uid = f"1.2.826.0.1.3680043.9.7243.{rand}.proton_plan"

        plan_dcm = _make_dummy_plan(patient_id=pid, plan_uid=plan_uid)
        plan_bytes = _dataset_to_bytes(plan_dcm)

        # First upload the plan
        resp = client.post(
            "/api/plans/upload",
            files=[("files", ("Plan.dcm", plan_bytes, "application/dicom"))],
        )
        assert resp.status_code == 200, resp.text
        plan_id = resp.json()["plan_id"]

        # Now upload proton RT Ion Record with SOPClassUID 1.2.840.10008.5.1.4.1.1.481.9 and Modality RTIBTR
        rec_dcm = _make_dummy_record(
            patient_id=pid,
            plan_uid=plan_uid,
            fraction_number=2,
            modality="RTIBTR",
            sop_class_uid="1.2.840.10008.5.1.4.1.1.481.9",
        )
        rec_bytes = _dataset_to_bytes(rec_dcm)

        # Upload directly to the plan's records endpoint
        resp2 = client.post(
            f"/api/plans/{plan_id}/upload-records",
            files=[("files", ("IonRecord_fx2.dcm", rec_bytes, "application/dicom"))],
        )
        assert resp2.status_code == 200, resp2.text

        frac2 = db.query(Fraction).filter_by(plan_id=plan_id, fraction_number=2).first()
        assert frac2 is not None
        assert frac2.rtrecord_uid == rec_dcm.SOPInstanceUID
        assert frac2.delivery_type == "curative"
    finally:
        db.close()


def test_upload_extensionless_and_raw_uid_rtrecord(tmp_path):
    """Test uploading RTRecord files that lack .dcm extensions (common in PACS exports)."""
    client = TestClient(app)
    client.cookies[settings.AUTH_SESSION_COOKIE] = create_session_token("test_physicist")
    db = SessionLocal()

    try:
        rand = uuid.uuid4().hex[:8]
        pid = f"PT_RAW_{rand}"
        plan_uid = f"1.2.826.0.1.3680043.9.7243.{rand}.raw_plan"

        plan_dcm = _make_dummy_plan(patient_id=pid, plan_uid=plan_uid)
        plan_bytes = _dataset_to_bytes(plan_dcm)

        resp = client.post(
            "/api/plans/upload",
            files=[("files", ("Plan.dcm", plan_bytes, "application/dicom"))],
        )
        assert resp.status_code == 200, resp.text
        plan_id = resp.json()["plan_id"]

        rec_dcm = _make_dummy_record(
            patient_id=pid,
            plan_uid=plan_uid,
            fraction_number=3,
            modality="RTRECORD",
            sop_class_uid="1.2.840.10008.5.1.4.1.1.481.9",
        )
        rec_bytes = _dataset_to_bytes(rec_dcm)

        # Extensionless filename: raw SOPInstanceUID
        raw_filename = f"RI.{rec_dcm.SOPInstanceUID}"
        resp2 = client.post(
            f"/api/plans/{plan_id}/upload-records",
            files=[("files", (raw_filename, rec_bytes, "application/octet-stream"))],
        )
        assert resp2.status_code == 200, resp2.text

        frac3 = db.query(Fraction).filter_by(plan_id=plan_id, fraction_number=3).first()
        assert frac3 is not None
        assert frac3.rtrecord_uid == rec_dcm.SOPInstanceUID
    finally:
        db.close()


def test_upload_rtrecord_fraction_number_from_fraction_group_sequence(tmp_path):
    """Test extracting fraction number when specified in FractionGroupSequence.ReferencedFractionNumber."""
    client = TestClient(app)
    client.cookies[settings.AUTH_SESSION_COOKIE] = create_session_token("test_physicist")
    db = SessionLocal()

    try:
        rand = uuid.uuid4().hex[:8]
        pid = f"PT_FXSEQ_{rand}"
        plan_uid = f"1.2.826.0.1.3680043.9.7243.{rand}.seq_plan"

        plan_dcm = _make_dummy_plan(patient_id=pid, plan_uid=plan_uid)
        plan_bytes = _dataset_to_bytes(plan_dcm)

        resp = client.post(
            "/api/plans/upload",
            files=[("files", ("Plan.dcm", plan_bytes, "application/dicom"))],
        )
        assert resp.status_code == 200, resp.text
        plan_id = resp.json()["plan_id"]

        # Generate fraction 7 with fraction number strictly in FractionGroupSequence
        rec_dcm7 = _make_dummy_record(
            patient_id=pid,
            plan_uid=plan_uid,
            fraction_number=7,
            modality="RTIBTR",
            sop_class_uid="1.2.840.10008.5.1.4.1.1.481.9",
            use_fraction_group_seq=True,
        )
        # Generate fraction 9 with fraction number strictly in FractionGroupSequence
        rec_dcm9 = _make_dummy_record(
            patient_id=pid,
            plan_uid=plan_uid,
            fraction_number=9,
            modality="RTIBTR",
            sop_class_uid="1.2.840.10008.5.1.4.1.1.481.9",
            use_fraction_group_seq=True,
        )

        resp_fx7 = client.post(
            f"/api/plans/{plan_id}/upload-records",
            files=[("files", ("rec_fx7.dcm", _dataset_to_bytes(rec_dcm7), "application/dicom"))],
        )
        assert resp_fx7.status_code == 200, resp_fx7.text

        resp_fx9 = client.post(
            f"/api/plans/{plan_id}/upload-records",
            files=[("files", ("rec_fx9.dcm", _dataset_to_bytes(rec_dcm9), "application/dicom"))],
        )
        assert resp_fx9.status_code == 200, resp_fx9.text

        frac7 = db.query(Fraction).filter_by(plan_id=plan_id, fraction_number=7).first()
        frac9 = db.query(Fraction).filter_by(plan_id=plan_id, fraction_number=9).first()
        assert frac7 is not None, "Fraction 7 was not identified from FractionGroupSequence"
        assert frac9 is not None, "Fraction 9 was not identified from FractionGroupSequence"
        assert frac7.rtrecord_uid == rec_dcm7.SOPInstanceUID
        assert frac9.rtrecord_uid == rec_dcm9.SOPInstanceUID
    finally:
        db.close()


def test_upload_zip_with_multiple_records(tmp_path):
    """Test uploading a zip archive containing multiple RTRecords."""
    import zipfile
    client = TestClient(app)
    client.cookies[settings.AUTH_SESSION_COOKIE] = create_session_token("test_physicist")
    db = SessionLocal()

    try:
        rand = uuid.uuid4().hex[:8]
        pid = f"PT_ZIP_{rand}"
        plan_uid = f"1.2.826.0.1.3680043.9.7243.{rand}.zip_plan"

        plan_dcm = _make_dummy_plan(patient_id=pid, plan_uid=plan_uid)
        rec2 = _make_dummy_record(patient_id=pid, plan_uid=plan_uid, fraction_number=2, sop_class_uid="1.2.840.10008.5.1.4.1.1.481.9")
        rec3 = _make_dummy_record(patient_id=pid, plan_uid=plan_uid, fraction_number=3, sop_class_uid="1.2.840.10008.5.1.4.1.1.481.9")

        # Create zip containing plan and both records
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w") as zf:
            zf.writestr("plan.dcm", _dataset_to_bytes(plan_dcm))
            zf.writestr("record_fx2.dcm", _dataset_to_bytes(rec2))
            zf.writestr("record_fx3.dcm", _dataset_to_bytes(rec3))
        zip_buf.seek(0)

        resp = client.post(
            "/api/plans/upload",
            files=[("files", ("treatment_package.zip", zip_buf.read(), "application/zip"))],
        )
        assert resp.status_code == 200, resp.text
        plan_id = resp.json()["plan_id"]

        frac2 = db.query(Fraction).filter_by(plan_id=plan_id, fraction_number=2).first()
        frac3 = db.query(Fraction).filter_by(plan_id=plan_id, fraction_number=3).first()
        assert frac2 is not None
        assert frac3 is not None
        assert frac2.rtrecord_uid == rec2.SOPInstanceUID
        assert frac3.rtrecord_uid == rec3.SOPInstanceUID
    finally:
        db.close()

