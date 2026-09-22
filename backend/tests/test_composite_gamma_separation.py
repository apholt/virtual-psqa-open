import tempfile
from pathlib import Path
import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import generate_uid

import uuid
from database import SessionLocal, ensure_schema
from models.patient import Patient
from models.plan import Plan
from models.gamma_result import GammaResult
from services.dose_grid import DoseGrid
from services.gamma_analysis import (
    run_gamma_analysis,
    ensure_composite_gamma,
    clear_dose_caches,
)
from services.report_generator import build_secondary_dose_report_html
from config import settings


def _create_dummy_plan(db, store_dir: Path, n_fields: int = 2) -> Plan:
    plan_uid = generate_uid()
    patient = Patient(
        patient_id=f"PT_{uuid.uuid4().hex[:8]}",
        patient_name="Gamma^CompositeTest",
    )
    db.add(patient)
    db.commit()

    plan = Plan(
        patient_id=patient.id,
        plan_label="TEST_COMPOSITE_PLAN",
        plan_name="Composite Plan Test",
        number_of_fields=n_fields,
        number_of_fractions=30,
        rtplan_uid=plan_uid,
        dicom_store_path=str(store_dir),
    )
    db.add(plan)
    db.commit()

    # Create RTPLAN DICOM in store_dir with IonBeamSequence
    plan_file = store_dir / "rtplan.dcm"
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.481.8"
    file_meta.MediaStorageSOPInstanceUID = plan_uid
    file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian

    ds = pydicom.dataset.FileDataset(str(plan_file), {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.Modality = "RTPLAN"
    ds.SOPClassUID = file_meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = plan_uid

    beam_seq = []
    for b_num in range(1, n_fields + 1):
        b = Dataset()
        b.BeamNumber = b_num
        b.BeamName = f"Field_{b_num}"
        beam_seq.append(b)
    ds.IonBeamSequence = Sequence(beam_seq)
    ds.save_as(str(plan_file))

    return plan


def _create_beam_rtdose(store_dir: Path, plan_uid: str, beam_num: int, dose_val: float) -> Path:
    sop_uid = generate_uid()
    file_path = store_dir / f"dose_beam{beam_num}.dcm"
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.481.2"
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian

    ds = pydicom.dataset.FileDataset(str(file_path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.Modality = "RTDOSE"
    ds.SOPClassUID = file_meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = sop_uid
    ds.DoseSummationType = "BEAM"

    ref_plan = Dataset()
    ref_plan.ReferencedSOPClassUID = "1.2.840.10008.5.1.4.1.1.481.8"
    ref_plan.ReferencedSOPInstanceUID = plan_uid
    ds.ReferencedRTPlanSequence = Sequence([ref_plan])

    ref_beam = Dataset()
    ref_beam.ReferencedBeamNumber = beam_num
    ds.ReferencedBeamSequence = Sequence([ref_beam])

    # 10x10x10 voxel array
    arr = np.zeros((10, 10, 10), dtype=np.uint16)
    arr[3:7, 3:7, 3:7] = int(dose_val * 1000)
    ds.PixelData = arr.tobytes()
    ds.Rows = 10
    ds.Columns = 10
    ds.NumberOfFrames = 10
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelSpacing = [2.0, 2.0]
    ds.SliceThickness = 2.0
    ds.GridFrameOffsetVector = [float(z * 2.0) for z in range(10)]
    ds.ImagePositionPatient = [0.0, 0.0, 0.0]
    ds.DoseGridScaling = 0.001

    ds.save_as(str(file_path))
    return file_path


def _create_mc_doses(plan_id: int, n_fields: int, beam1_dose: float, beam2_dose: float):
    mc_dir = Path(settings.RESULTS_PATH) / f"plan_{plan_id}" / "mcSquare_output"
    mc_dir.mkdir(parents=True, exist_ok=True)

    arr1 = np.zeros((10, 10, 10), dtype=np.float32)
    # Slight shift on beam 1 so gamma is e.g. ~95%
    arr1[3:7, 3:7, 3:7] = beam1_dose * 1.02
    grid1 = DoseGrid(array=arr1, spacing=(2.0, 2.0, 2.0), origin=(0.0, 0.0, 0.0))
    grid1.save(str(mc_dir / "mc_dose_beam1.npz"))

    arr2 = np.zeros((10, 10, 10), dtype=np.float32)
    # Larger shift on beam 2 so gamma is e.g. ~88%
    arr2[3:7, 3:7, 3:7] = beam2_dose * 1.08
    grid2 = DoseGrid(array=arr2, spacing=(2.0, 2.0, 2.0), origin=(0.0, 0.0, 0.0))
    grid2.save(str(mc_dir / "mc_dose_beam2.npz"))

    # Summed MC dose
    arr_sum = arr1 + arr2
    grid_sum = DoseGrid(array=arr_sum, spacing=(2.0, 2.0, 2.0), origin=(0.0, 0.0, 0.0))
    grid_sum.save(str(mc_dir / "mc_dose.npz"))


def test_run_gamma_analysis_creates_composite_row_for_multibeam():
    ensure_schema()
    clear_dose_caches()
    db = SessionLocal()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            store_dir = Path(tmpdir)
            plan = _create_dummy_plan(db, store_dir, n_fields=2)
            _create_beam_rtdose(store_dir, plan.rtplan_uid, 1, 2.0)
            _create_beam_rtdose(store_dir, plan.rtplan_uid, 2, 2.0)
            _create_mc_doses(plan.id, 2, 2.0, 2.0)

            run_gamma_analysis(plan.id, db)

            results = (
                db.query(GammaResult)
                .filter_by(plan_id=plan.id, comparison_type="mcSquare_vs_TPS")
                .all()
            )

            # Must have 3 rows: Beam 1, Beam 2, and Composite
            assert len(results) == 3

            beam1_row = next((r for r in results if r.beam_number == 1), None)
            beam2_row = next((r for r in results if r.beam_number == 2), None)
            comp_row = next((r for r in results if r.field_name == "Composite" and r.beam_number is None), None)

            assert beam1_row is not None
            assert beam2_row is not None
            assert comp_row is not None

            # Verify that the composite gamma is its own calculated 3D score
            assert isinstance(comp_row.passing_rate, float)
            assert comp_row.gamma_map_path is not None
            assert Path(comp_row.gamma_map_path).exists()
    finally:
        db.close()


def test_ensure_composite_gamma_backfills_missing_composite():
    ensure_schema()
    clear_dose_caches()
    db = SessionLocal()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            store_dir = Path(tmpdir)
            plan = _create_dummy_plan(db, store_dir, n_fields=2)
            _create_beam_rtdose(store_dir, plan.rtplan_uid, 1, 2.0)
            _create_beam_rtdose(store_dir, plan.rtplan_uid, 2, 2.0)
            _create_mc_doses(plan.id, 2, 2.0, 2.0)

            # Simulate an existing database state where only per-beam results exist
            row1 = GammaResult(
                plan_id=plan.id,
                beam_number=1,
                field_name="Field_1",
                comparison_type="mcSquare_vs_TPS",
                dd_percent=3.0,
                dta_mm=3.0,
                passing_rate=98.5,
                threshold=90.0,
                passed=True,
            )
            row2 = GammaResult(
                plan_id=plan.id,
                beam_number=2,
                field_name="Field_2",
                comparison_type="mcSquare_vs_TPS",
                dd_percent=3.0,
                dta_mm=3.0,
                passing_rate=92.0,
                threshold=90.0,
                passed=True,
            )
            db.add_all([row1, row2])
            db.commit()

            # Ensure composite gamma runs and auto-backfills the Composite row
            comp = ensure_composite_gamma(plan.id, db)
            assert comp is not None
            assert comp.field_name == "Composite"
            assert comp.beam_number is None
            assert isinstance(comp.passing_rate, float)

            # Idempotent: second call returns the existing row without adding another
            comp2 = ensure_composite_gamma(plan.id, db)
            assert comp2.id == comp.id

            count = (
                db.query(GammaResult)
                .filter_by(plan_id=plan.id, comparison_type="mcSquare_vs_TPS")
                .count()
            )
            assert count == 3
    finally:
        db.close()


def test_secondary_dose_report_displays_composite_gamma():
    ensure_schema()
    clear_dose_caches()
    db = SessionLocal()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            store_dir = Path(tmpdir)
            plan = _create_dummy_plan(db, store_dir, n_fields=2)
            _create_beam_rtdose(store_dir, plan.rtplan_uid, 1, 2.0)
            _create_beam_rtdose(store_dir, plan.rtplan_uid, 2, 2.0)
            _create_mc_doses(plan.id, 2, 2.0, 2.0)

            # Insert beam results + distinct composite row
            row1 = GammaResult(
                plan_id=plan.id,
                beam_number=1,
                field_name="Field_1",
                comparison_type="mcSquare_vs_TPS",
                dd_percent=3.0,
                dta_mm=3.0,
                passing_rate=99.1,
                threshold=90.0,
                passed=True,
            )
            row2 = GammaResult(
                plan_id=plan.id,
                beam_number=2,
                field_name="Field_2",
                comparison_type="mcSquare_vs_TPS",
                dd_percent=3.0,
                dta_mm=3.0,
                passing_rate=91.4,
                threshold=90.0,
                passed=True,
            )
            comp_row = GammaResult(
                plan_id=plan.id,
                beam_number=None,
                field_name="Composite",
                comparison_type="mcSquare_vs_TPS",
                dd_percent=3.0,
                dta_mm=3.0,
                passing_rate=96.7,
                threshold=90.0,
                passed=True,
            )
            db.add_all([row1, row2, comp_row])
            db.commit()

            html = build_secondary_dose_report_html(plan.id, db)
            # The report KPI score must display 96.7% (the composite score), NOT 99.1% (Beam 1) and NOT 95.25% (mean)
            assert "96.7%" in html
            assert "Field_1" in html
            assert "Field_2" in html
    finally:
        db.close()
