"""
Tests for duplicate file detection and ignoring on upload, particularly duplicate RTDOSE files.
"""
import io
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config import settings
from database import SessionLocal
from tests.synthetic_dicom_gen import write_synthetic_dicom_set
from main import app
from models.patient import Patient
from models.plan import Plan
from services.auth_service import create_session_token
from services.dicom_ingestor import (
    classify_dicom_files,
    file_sha256,
    get_rtdose_fingerprint,
    ingest_dicom_directory,
    ingest_standalone_rtdose_files,
)
from dicom.rtdose_parser import clean_store_duplicates


@pytest.fixture(scope="module")
def client():
    token = create_session_token("admin")
    return TestClient(app, cookies={settings.AUTH_SESSION_COOKIE: token})


def test_classify_dicom_files_ignores_duplicates():
    tmp = Path(tempfile.mkdtemp(prefix="test_classify_dup_"))
    try:
        write_synthetic_dicom_set(str(tmp), n_fields=2)
        # Duplicate all files into a subfolder
        sub = tmp / "dups"
        sub.mkdir()
        for f in tmp.glob("*.dcm"):
            shutil.copy2(f, sub / f"dup_{f.name}")

        classified = classify_dicom_files(str(tmp))
        # There should only be 1 plan, 1 struct, 1 CT series, etc.
        assert len(classified.get("RTPLAN", [])) == 1
        assert len(classified.get("RTSTRUCT", [])) == 1
        assert len(classified.get("RTDOSE", [])) == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_clean_store_duplicates():
    tmp = Path(tempfile.mkdtemp(prefix="test_clean_dup_"))
    try:
        write_synthetic_dicom_set(str(tmp), n_fields=2)
        rtdose_files = list(tmp.glob("RD*.dcm"))
        assert len(rtdose_files) >= 1
        rd = rtdose_files[0]

        # Create exact duplicate file
        dup1 = tmp / f"dup_0_{rd.name}"
        shutil.copy2(rd, dup1)

        # Create another duplicate with identical content
        dup2 = tmp / f"dup_1_{rd.name}"
        shutil.copy2(rd, dup2)

        assert dup1.exists() and dup2.exists()
        removed = clean_store_duplicates(str(tmp))
        assert len(removed) == 2
        # Exactly one copy remains
        remaining = [p for p in tmp.glob("*.dcm") if p.name.endswith(rd.name)]
        assert len(remaining) == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ingest_dicom_directory_ignores_duplicate_upload():
    db = SessionLocal()
    tmp = tempfile.mkdtemp(prefix="test_ingest_dup_")
    try:
        write_synthetic_dicom_set(tmp, n_fields=2)
        # First ingestion
        res1 = ingest_dicom_directory(tmp, db)
        assert res1.get("is_all_duplicates") is False
        assert res1.get("new_files_count", 0) > 0
        plan_id = res1["plan_id"]
        plan = db.query(Plan).filter_by(id=plan_id).first()
        store_path = Path(plan.dicom_store_path)

        # Count dose files in store
        initial_dose_count = len(list(store_path.glob("RD*.dcm")))

        # Re-ingest the exact same folder
        res2 = ingest_dicom_directory(tmp, db)
        assert res2.get("is_all_duplicates") is True
        assert res2.get("new_files_count") == 0
        assert any("duplicates" in w.lower() for w in res2.get("warnings", []))

        # Verify no extra dose files were dumped into the store
        after_dose_count = len(list(store_path.glob("RD*.dcm")))
        assert after_dose_count == initial_dose_count
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        db.close()


def test_upload_api_ignores_duplicate_rtdose(client):
    tmp = tempfile.mkdtemp(prefix="test_api_dup_")
    try:
        write_synthetic_dicom_set(tmp, n_fields=2)
        files = []
        for p in Path(tmp).glob("*.dcm"):
            files.append(("files", (p.name, p.read_bytes(), "application/dicom")))

        # First upload
        res1 = client.post("/api/plans/upload", files=files)
        assert res1.status_code == 200
        data1 = res1.json()
        assert data1.get("is_all_duplicates") is False

        # Second upload of the same files
        files_dup = []
        for p in Path(tmp).glob("*.dcm"):
            files_dup.append(("files", (f"re_{p.name}", p.read_bytes(), "application/dicom")))

        res2 = client.post("/api/plans/upload", files=files_dup)
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2.get("is_all_duplicates") is True
        assert any("duplicates" in w.lower() for w in data2.get("warnings", []))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
