"""
Unit and integration tests for Orthanc PACS/VNA integration service and router.
"""
import io
import zipfile
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from database import SessionLocal
from models.patient import Patient
from models.plan import Plan


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def create_dummy_zip_with_dicom(modality="RTPLAN", sop_uid="1.2.3.4.5"):
    """Helper to generate an in-memory zip archive with a dummy DICOM file."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{modality}_{sop_uid}.dcm", b"FAKE_DICOM_CONTENT")
    buf.seek(0)
    return buf.getvalue()


class MockOrthancClient:
    def __init__(self, get_handler=None, post_handler=None):
        self.get_handler = get_handler or (lambda url: MagicMock(status_code=404))
        self.post_handler = post_handler or (lambda url, json=None: MagicMock(status_code=404))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, *args, **kwargs):
        return self.get_handler(url)

    def post(self, url, *args, **kwargs):
        return self.post_handler(url, json=kwargs.get("json"))


# ---------------------------------------------------------------------------
# Test Connection
# ---------------------------------------------------------------------------

def test_orthanc_status_online(client):
    mock_system_resp = MagicMock()
    mock_system_resp.status_code = 200
    mock_system_resp.json.return_value = {
        "Version": "1.12.3",
        "Name": "ClinicalOrthanc",
        "DicomAet": "ORTHANC_TEST",
        "DicomPort": 4242,
        "HttpPort": 8042,
    }

    mock_client = MockOrthancClient(get_handler=lambda url: mock_system_resp)

    with patch("services.orthanc_service.get_orthanc_client", return_value=mock_client):
        res = client.get("/api/orthanc/status")
        assert res.status_code == 200
        data = res.json()
        assert data["online"] is True
        assert data["version"] == "1.12.3"
        assert data["name"] == "ClinicalOrthanc"
        assert data["dicom_aet"] == "ORTHANC_TEST"


def test_orthanc_status_offline(client):
    with patch("services.orthanc_service.get_orthanc_client", side_effect=Exception("Connection refused")):
        res = client.get("/api/orthanc/status")
        assert res.status_code == 200
        data = res.json()
        assert data["online"] is False
        assert "Connection refused" in data["error"]


# ---------------------------------------------------------------------------
# Test Patient Search
# ---------------------------------------------------------------------------

def test_search_orthanc_patients(client):
    mock_find_resp = MagicMock()
    mock_find_resp.status_code = 200
    mock_find_resp.json.return_value = ["orthanc-pat-1"]

    mock_pat_resp = MagicMock()
    mock_pat_resp.status_code = 200
    mock_pat_resp.json.return_value = {
        "ID": "orthanc-pat-1",
        "MainDicomTags": {
            "PatientID": "TEST_PAT_001",
            "PatientName": "DOE^JANE",
            "PatientBirthDate": "19850512",
            "PatientSex": "F",
        },
        "Studies": ["study-1"],
    }

    def mock_get(url):
        if "/patients/orthanc-pat-1" in url:
            return mock_pat_resp
        return MagicMock(status_code=404)

    def mock_post(url, json=None):
        if "/tools/find" in url:
            return mock_find_resp
        return MagicMock(status_code=404)

    mock_client = MockOrthancClient(get_handler=mock_get, post_handler=mock_post)

    with patch("services.orthanc_service.get_orthanc_client", return_value=mock_client):
        res = client.get("/api/orthanc/patients?query=DOE")
        assert res.status_code == 200
        patients = res.json()
        assert len(patients) == 1
        p = patients[0]
        assert p["patient_id"] == "TEST_PAT_001"
        assert p["patient_name"] == "DOE JANE"
        assert p["date_of_birth"] == "19850512"
        assert p["studies_count"] == 1


# ---------------------------------------------------------------------------
# Test Patient Details & Categorization
# ---------------------------------------------------------------------------

def test_get_patient_details_categorization(client):
    mock_pat_resp = MagicMock()
    mock_pat_resp.status_code = 200
    mock_pat_resp.json.return_value = {
        "ID": "pat-123",
        "MainDicomTags": {
            "PatientID": "PID_001",
            "PatientName": "SMITH^JOHN",
        },
        "Studies": ["study-1"],
    }

    mock_study_resp = MagicMock()
    mock_study_resp.status_code = 200
    mock_study_resp.json.return_value = {
        "MainDicomTags": {
            "StudyDescription": "Proton Cranial Plan",
            "StudyDate": "20260901",
            "StudyInstanceUID": "1.2.840.1000",
        },
        "Series": ["ser-plan", "ser-dose", "ser-struct", "ser-ct", "ser-rec", "ser-cbct", "ser-reg"],
    }

    series_map = {
        "ser-plan": {
            "MainDicomTags": {"Modality": "RTPLAN", "SeriesDescription": "PROG_01", "SeriesDate": "20260901", "SeriesInstanceUID": "uid.plan"},
            "Instances": ["inst-plan"],
        },
        "ser-dose": {
            "MainDicomTags": {"Modality": "RTDOSE", "SeriesDescription": "TPS Dose", "SeriesDate": "20260901", "SeriesInstanceUID": "uid.dose"},
            "Instances": ["inst-dose"],
        },
        "ser-struct": {
            "MainDicomTags": {"Modality": "RTSTRUCT", "SeriesDescription": "Contours", "SeriesDate": "20260901", "SeriesInstanceUID": "uid.struct"},
            "Instances": ["inst-struct"],
        },
        "ser-ct": {
            "MainDicomTags": {"Modality": "CT", "SeriesDescription": "Planning CT 1mm", "SeriesDate": "20260901", "SeriesInstanceUID": "uid.ct"},
            "Instances": ["inst-ct1", "inst-ct2", "inst-ct3"],
        },
        "ser-rec": {
            "MainDicomTags": {"Modality": "RTRECORD", "SeriesDescription": "Treatment Fraction 1", "SeriesDate": "20260905", "SeriesInstanceUID": "uid.rec"},
            "Instances": ["inst-rec"],
        },
        "ser-cbct": {
            "MainDicomTags": {"Modality": "CT", "SeriesDescription": "Daily CBCT Fx 1", "SeriesDate": "20260905", "SeriesInstanceUID": "uid.cbct"},
            "Instances": ["inst-cbct1", "inst-cbct2"],
        },
        "ser-reg": {
            "MainDicomTags": {"Modality": "REG", "SeriesDescription": "Treated Match Fx 1", "SeriesDate": "20260905", "SeriesInstanceUID": "uid.reg"},
            "Instances": ["inst-reg"],
        },
    }

    inst_tags = {
        "inst-plan": {"RTPlanLabel": "PROG_01", "SOPInstanceUID": "sop.plan", "SOPClassUID": "1.2.840.10008.5.1.4.1.1.481.8"},
        "inst-dose": {"SOPInstanceUID": "sop.dose", "SOPClassUID": "1.2.840.10008.5.1.4.1.1.481.2"},
        "inst-struct": {"SOPInstanceUID": "sop.struct", "SOPClassUID": "1.2.840.10008.5.1.4.1.1.481.3"},
        "inst-ct1": {"SliceThickness": 1.0, "SOPClassUID": "1.2.840.10008.5.1.4.1.1.2"},
        "inst-rec": {"TreatmentDate": "20260905", "TreatmentDeliveryType": "TREATMENT", "FractionGroupSequence": [{"ReferencedFractionNumber": 1}], "SOPInstanceUID": "sop.rec", "SOPClassUID": "1.2.840.10008.5.1.4.1.1.481.7"},
        "inst-cbct1": {"SliceThickness": 2.0, "SOPClassUID": "1.2.840.10008.5.1.4.1.1.2"},
        "inst-reg": {"ContentDescription": "Treated Match", "SOPInstanceUID": "sop.reg", "SOPClassUID": "1.2.840.10008.5.1.4.1.1.66.1"},
    }

    def mock_get_details(url):
        if url.endswith("/patients/pat-123"):
            return mock_pat_resp
        if url.endswith("/studies/study-1"):
            return mock_study_resp
        for ser_k, ser_v in series_map.items():
            if url.endswith(f"/series/{ser_k}"):
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = ser_v
                return resp
        for inst_k, inst_v in inst_tags.items():
            if url.endswith(f"/instances/{inst_k}/simplified-tags"):
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = inst_v
                return resp
        return MagicMock(status_code=404)

    mock_client = MockOrthancClient(get_handler=mock_get_details)

    with patch("services.orthanc_service.get_orthanc_client", return_value=mock_client):
        res = client.get("/api/orthanc/patients/pat-123")
        assert res.status_code == 200
        data = res.json()
        assert data["patient"]["patient_id"] == "PID_001"
        assert len(data["plans"]) == 1
        plan = data["plans"][0]
        assert plan["plan_label"] == "PROG_01"
        assert plan["dose_series_id"] == "ser-dose"
        assert plan["struct_series_id"] == "ser-struct"
        assert plan["planning_ct_series_id"] == "ser-ct"

        assert len(data["rt_records"]) == 1
        rec = data["rt_records"][0]
        assert rec["fraction_number"] == 1
        assert rec["delivery_type"] == "curative"

        assert len(data["offline_images"]) >= 2
        cbcts = [img for img in data["offline_images"] if img["kind"] == "cbct"]
        regs = [img for img in data["offline_images"] if img["kind"] == "reg"]
        assert len(cbcts) == 1
        assert len(regs) == 1


# ---------------------------------------------------------------------------
# Test Plan Ingestion from Orthanc
# ---------------------------------------------------------------------------

def test_import_plan_endpoint(client):
    zip_bytes = create_dummy_zip_with_dicom("RTPLAN", "1.2.3")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = zip_bytes

    mock_ingest_result = {
        "plan_id": 999,
        "patient_id": "PT999",
        "patient_name": "TEST PATIENT",
        "plan_label": "TEST_PLAN",
        "plan_name": "TEST_PLAN",
        "number_of_fields": 2,
        "number_of_fractions": 30,
        "fields": [],
        "warnings": [],
        "dicom_files_found": {"RTPLAN": 1},
    }

    mock_client = MockOrthancClient(get_handler=lambda url: mock_resp)

    with patch("services.orthanc_service.get_orthanc_client", return_value=mock_client), \
         patch("services.orthanc_service.ingest_dicom_directory", return_value=mock_ingest_result):
        payload = {
            "plan_series_id": "ser-plan-1",
            "dose_series_id": "ser-dose-1",
            "struct_series_id": "ser-struct-1",
            "ct_series_id": "ser-ct-1",
        }
        res = client.post("/api/orthanc/import/plan", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["plan_id"] == 999
        assert data["plan_label"] == "TEST_PLAN"


# ---------------------------------------------------------------------------
# Test Settings Update for Orthanc
# ---------------------------------------------------------------------------

def test_update_settings_orthanc(client):
    payload = {
        "orthanc": {
            "orthanc_url": "http://hospital-pacs:8042",
            "orthanc_username": "physicist",
            "orthanc_password": "secretpassword",
            "orthanc_timeout_seconds": 90,
        }
    }
    res = client.post("/api/settings", json=payload)
    assert res.status_code == 200

    settings_res = client.get("/api/settings")
    assert settings_res.status_code == 200
    s_data = settings_res.json()
    assert s_data["orthanc"]["orthanc_url"] == "http://hospital-pacs:8042"
    assert s_data["orthanc"]["orthanc_username"] == "physicist"
    assert s_data["orthanc"]["has_password"] is True
