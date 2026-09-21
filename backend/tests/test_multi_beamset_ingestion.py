"""
Tests for multi-beamset/multi-plan ingestion, dose segregation, and patient worklist display.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest
import pydicom
from pydicom.dataset import Dataset, FileDataset
from pydicom.sequence import Sequence
from pydicom.uid import generate_uid
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import settings
from database import Base
from models.patient import Patient
from models.plan import Plan
from services.dicom_ingestor import ingest_dicom_directory
from dicom.rtdose_parser import find_rtdose_file, find_beam_rtdose_files
from tests.synthetic_dicom_gen import (
    _base_dataset,
    generate_synthetic_rtionplan,
    generate_synthetic_rtdose,
)
from _diagnostics.repair_multi_plan_store import inspect_store, repair_directory


@pytest.fixture
def test_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_multi_beamset_ingestion_and_dose_segregation(test_db, tmp_path, monkeypatch):
    # Route store to tmp_path
    monkeypatch.setattr(settings, "DICOM_STORE_PATH", str(tmp_path / "dicom_store"))

    upload_dir = tmp_path / "upload_multi"
    upload_dir.mkdir()

    patient_id = "PATIENT_BREAST_001"
    patient_name = "BREAST^PATIENT"

    # 1. Create two beamset RTPLANs
    plan1_dcm, plan1_uid = generate_synthetic_rtionplan(n_fields=2)
    plan1_dcm.PatientID = patient_id
    plan1_dcm.PatientName = patient_name
    plan1_dcm.RTPlanLabel = "Lt_breast_B1"
    plan1_dcm.RTPlanName = "Lt_breast_B1"
    plan1_path = upload_dir / "RP.plan1.dcm"
    plan1_dcm.save_as(str(plan1_path))

    plan2_dcm, plan2_uid = generate_synthetic_rtionplan(n_fields=2)
    plan2_dcm.PatientID = patient_id
    plan2_dcm.PatientName = patient_name
    plan2_dcm.RTPlanLabel = "Lt_breast_B2"
    plan2_dcm.RTPlanName = "Lt_breast_B2"
    plan2_path = upload_dir / "RP.plan2.dcm"
    plan2_dcm.save_as(str(plan2_path))

    # 2. Create individual plan RTDOSE files
    dose1_dcm = generate_synthetic_rtdose(plan1_dcm, grid_size=(10, 10, 10))
    dose1_uid = generate_uid()
    dose1_dcm.SOPInstanceUID = dose1_uid
    dose1_dcm.DoseSummationType = "PLAN"
    dose1_path = upload_dir / "RD.dose1.dcm"
    dose1_dcm.save_as(str(dose1_path))

    dose2_dcm = generate_synthetic_rtdose(plan2_dcm, grid_size=(10, 10, 10))
    dose2_uid = generate_uid()
    dose2_dcm.SOPInstanceUID = dose2_uid
    dose2_dcm.DoseSummationType = "PLAN"
    dose2_path = upload_dir / "RD.dose2.dcm"
    dose2_dcm.save_as(str(dose2_path))

    # 3. Create a MULTI_PLAN total plan dose (composite dose)
    comp_dose_dcm = generate_synthetic_rtdose(plan1_dcm, grid_size=(10, 10, 10))
    comp_uid = generate_uid()
    comp_dose_dcm.SOPInstanceUID = comp_uid
    comp_dose_dcm.DoseSummationType = "MULTI_PLAN"
    # Referenced both plans
    ref1 = Dataset()
    ref1.ReferencedSOPClassUID = plan1_dcm.SOPClassUID
    ref1.ReferencedSOPInstanceUID = plan1_uid
    ref2 = Dataset()
    ref2.ReferencedSOPClassUID = plan2_dcm.SOPClassUID
    ref2.ReferencedSOPInstanceUID = plan2_uid
    comp_dose_dcm.ReferencedRTPlanSequence = Sequence([ref1, ref2])
    comp_path = upload_dir / "RD.total_plan_dose.dcm"
    comp_dose_dcm.save_as(str(comp_path))

    # 4. Ingest directory
    result = ingest_dicom_directory(str(upload_dir), test_db)

    # Verification: Both plans were ingested
    assert "plan_ids" in result
    assert len(result["plan_ids"]) == 2
    assert len(result["plans"]) == 2

    # Query DB
    plans_in_db = test_db.query(Plan).filter_by(patient_id=1).all()
    assert len(plans_in_db) == 2
    p1 = test_db.query(Plan).filter_by(rtplan_uid=plan1_uid).first()
    p2 = test_db.query(Plan).filter_by(rtplan_uid=plan2_uid).first()
    assert p1 is not None
    assert p2 is not None

    # Check store directories
    store1 = Path(p1.dicom_store_path)
    store2 = Path(p2.dicom_store_path)
    assert store1 != store2
    assert store1.exists()
    assert store2.exists()

    # Verify find_rtdose_file returns the CORRECT matching dose for each beamset
    matched_dose1 = find_rtdose_file(store1, plan_uid=plan1_uid)
    matched_dose2 = find_rtdose_file(store2, plan_uid=plan2_uid)

    assert matched_dose1 is not None
    assert matched_dose2 is not None

    d1_ds = pydicom.dcmread(matched_dose1, stop_before_pixels=True)
    d2_ds = pydicom.dcmread(matched_dose2, stop_before_pixels=True)

    # dose 1 must match plan 1 and NOT plan 2
    assert d1_ds.SOPInstanceUID == dose1_uid
    assert str(d1_ds.ReferencedRTPlanSequence[0].ReferencedSOPInstanceUID) == plan1_uid

    # dose 2 must match plan 2 and NOT plan 1
    assert d2_ds.SOPInstanceUID == dose2_uid
    assert str(d2_ds.ReferencedRTPlanSequence[0].ReferencedSOPInstanceUID) == plan2_uid

    # Neither dose should be the MULTI_PLAN composite dose
    assert d1_ds.SOPInstanceUID != comp_uid
    assert d2_ds.SOPInstanceUID != comp_uid

    # Verify patient worklist API returns plan_count == 2 and both plans
    import asyncio
    from routers.patients import list_patients

    patients_resp = asyncio.run(list_patients(db=test_db))
    assert len(patients_resp) == 1
    pat_item = patients_resp[0]
    assert pat_item.plan_count == 2
    assert pat_item.plans is not None
    assert len(pat_item.plans) == 2
    labels = {p.plan_label for p in pat_item.plans}
    assert "Lt_breast_B1" in labels
    assert "Lt_breast_B2" in labels


def test_repair_diagnostic_utility(test_db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DICOM_STORE_PATH", str(tmp_path / "dicom_store"))

    # Create a corrupted/legacy combined store directory with 2 plans together
    combined_dir = tmp_path / "legacy_combined_store"
    combined_dir.mkdir(parents=True)

    patient_id = "PATIENT_LEGACY_002"
    patient_name = "LEGACY^PATIENT"

    plan1_dcm, plan1_uid = generate_synthetic_rtionplan(n_fields=2)
    plan1_dcm.PatientID = patient_id
    plan1_dcm.PatientName = patient_name
    plan1_dcm.RTPlanLabel = "Beamset_A"
    plan1_dcm.save_as(str(combined_dir / "RP1.dcm"))

    plan2_dcm, plan2_uid = generate_synthetic_rtionplan(n_fields=2)
    plan2_dcm.PatientID = patient_id
    plan2_dcm.PatientName = patient_name
    plan2_dcm.RTPlanLabel = "Beamset_B"
    plan2_dcm.save_as(str(combined_dir / "RP2.dcm"))

    dose1_dcm = generate_synthetic_rtdose(plan1_dcm, grid_size=(10, 10, 10))
    dose1_dcm.save_as(str(combined_dir / "RD1.dcm"))

    dose2_dcm = generate_synthetic_rtdose(plan2_dcm, grid_size=(10, 10, 10))
    dose2_dcm.save_as(str(combined_dir / "RD2.dcm"))

    # Inspect store detects multi-plan directory
    found = inspect_store(tmp_path)
    assert len(found) == 1
    assert len(found[0]["plans"]) == 2

    # Run repair with apply=True
    repaired_ids = repair_directory(found[0], test_db, apply=True, run_pipeline=False)
    assert len(repaired_ids) == 2

    # Verify both plans exist in DB and have isolated folders
    p_a = test_db.query(Plan).filter_by(rtplan_uid=plan1_uid).first()
    p_b = test_db.query(Plan).filter_by(rtplan_uid=plan2_uid).first()
    assert p_a is not None
    assert p_b is not None
    assert p_a.dicom_store_path != p_b.dicom_store_path
    assert Path(p_a.dicom_store_path).exists()
    assert Path(p_b.dicom_store_path).exists()
