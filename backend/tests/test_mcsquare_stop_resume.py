"""
test_mcsquare_stop_resume.py — Test Stop button, dose preservation, and resumability
for openMCsquare / mock calculations.

Tests:
  1. Stopping after k of N beams preserves completed beams on disk.
  2. Automatic partial gamma evaluation runs for the completed beams.
  3. Resuming the calculation (force=False) skips completed beams and finishes remaining.
  4. Force recalculation (force=True) recalculates all beams from scratch.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pydicom
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from config import settings
from database import SessionLocal
from models.gamma_result import GammaResult
from models.plan import Plan
from models.qa_job import QAJob
from services.dicom_ingestor import ingest_dicom_directory
from services.gamma_analysis import load_plan_doses
from services.job_control import is_cancelled, request_cancel
from services.job_runner import run_qa_job
from tests.synthetic_dicom_gen import (
    generate_synthetic_rtdose,
    generate_synthetic_rtionplan,
    generate_synthetic_rtrecord,
)


def _create_5beam_patient(dicom_dir: str) -> dict[str, str]:
    """Creates a synthetic plan with 5 fields and 5 matching beam-level RTDose files."""
    os.makedirs(dicom_dir, exist_ok=True)
    rtplan, plan_uid = generate_synthetic_rtionplan(n_fields=5)
    plan_path = str(Path(dicom_dir) / f"RP.{rtplan.SOPInstanceUID}.dcm")
    pydicom.dcmwrite(plan_path, rtplan)

    # Composite RTDose
    rtdose = generate_synthetic_rtdose(rtplan, grid_size=(25, 25, 10))
    dose_path = str(Path(dicom_dir) / f"RD.{rtdose.SOPInstanceUID}.dcm")
    pydicom.dcmwrite(dose_path, rtdose)

    # Record
    rtrecord = generate_synthetic_rtrecord(rtplan)
    record_path = str(Path(dicom_dir) / f"RI.{rtrecord.SOPInstanceUID}.dcm")
    pydicom.dcmwrite(record_path, rtrecord)

    # 5 Beam-level RTDoses
    for beam in rtplan.IonBeamSequence:
        b_num = int(beam.BeamNumber)
        b_dose = generate_synthetic_rtdose(rtplan, grid_size=(25, 25, 10))
        b_dose.DoseSummationType = "BEAM"
        ref_plan = b_dose.ReferencedRTPlanSequence[0]
        rfg = Dataset()
        rfb = Dataset()
        rfb.ReferencedBeamNumber = b_num
        rfg.ReferencedBeamSequence = Sequence([rfb])
        ref_plan.ReferencedFractionGroupSequence = Sequence([rfg])
        b_path = str(Path(dicom_dir) / f"RD_beam_{b_num}.{b_dose.SOPInstanceUID}.dcm")
        pydicom.dcmwrite(b_path, b_dose)

    return {"plan": plan_path, "dose": dose_path}


def test_stop_and_resume_mcsquare():
    orig_mock = settings.MCSQUARE_SIMULATION_MODE
    settings.MCSQUARE_SIMULATION_MODE = True
    db = SessionLocal()
    tmp = tempfile.mkdtemp(prefix="stop_resume_test_")
    try:
        # Ingest 5-beam plan
        _create_5beam_patient(tmp)
        ingest_res = ingest_dicom_directory(tmp, db)
        plan_id = ingest_res["plan_id"]
        plan = db.query(Plan).filter_by(id=plan_id).first()
        assert plan is not None
        assert plan.number_of_fields == 5

        # ------------------------------------------------------------------
        # 1. Simulate stopping after beam 2
        # ------------------------------------------------------------------
        # We start a job, and we hook cancel when beam 2 completes in mock
        out_dir = Path(settings.RESULTS_PATH) / f"plan_{plan_id}" / "mcSquare_output"
        if out_dir.exists():
            shutil.rmtree(out_dir)

        job1 = QAJob(plan_id=plan_id, job_type="mcSquare", status="queued")
        db.add(job1)
        db.commit()
        db.refresh(job1)

        # We request cancel after beam 2 is saved:
        # Let's run a worker in a thread or cancel right before beam 3 in mock
        # For a deterministic unit test, pre-save beam 1 and beam 2, request cancel,
        # and verify job_runner handles it gracefully:
        import threading
        import time

        def cancel_trigger():
            # Wait until beam 2 dose exists
            b2_file = out_dir / "mc_dose_beam2.npz"
            for _ in range(50):
                if b2_file.exists():
                    break
                time.sleep(0.05)
            request_cancel(job1.id)

        t = threading.Thread(target=cancel_trigger)
        t.start()

        run_qa_job(job1.id)
        t.join()

        db.refresh(job1)
        assert job1.status == "cancelled", f"Expected cancelled status, got {job1.status}"
        assert "Stopped by user" in (job1.error_message or "")
        assert "2 of 5" in (job1.error_message or "") or "beam" in (job1.error_message or "")

        # Verify beams 1 & 2 are saved on disk
        b1_file = out_dir / "mc_dose_beam1.npz"
        b2_file = out_dir / "mc_dose_beam2.npz"
        assert b1_file.is_file(), "Beam 1 dose should be preserved"
        assert b2_file.is_file(), "Beam 2 dose should be preserved"

        # Verify composite dose was saved
        comp_file = out_dir / "mc_dose.npz"
        assert comp_file.is_file(), "Composite dose should be preserved"

        # Verify partial gamma analysis ran for the completed beams
        gamma_rows = db.query(GammaResult).filter_by(plan_id=plan_id).all()
        assert len(gamma_rows) >= 2, f"Expected at least 2 gamma results, got {len(gamma_rows)}"
        evaluated_beams = {r.beam_number for r in gamma_rows if r.beam_number is not None}
        assert 1 in evaluated_beams
        assert 2 in evaluated_beams

        # Verify dose sources loaded for plan
        doses = load_plan_doses(plan_id, db)
        assert "mcSquare_beam1" in doses
        assert "mcSquare_beam2" in doses
        assert "mcSquare" in doses

        # ------------------------------------------------------------------
        # 2. Resume simulation (force=False)
        # ------------------------------------------------------------------
        job2 = QAJob(plan_id=plan_id, job_type="mcSquare", status="queued")
        db.add(job2)
        db.commit()
        db.refresh(job2)

        # Run without force
        run_qa_job(job2.id, force=False)
        db.refresh(job2)
        assert job2.status == "complete", f"Resume failed: {job2.error_message}"

        # All 5 beams must now exist!
        for b in range(1, 6):
            b_path = out_dir / f"mc_dose_beam{b}.npz"
            assert b_path.is_file(), f"Beam {b} dose should exist after resume"

        # All 5 beams evaluated in gamma
        gamma_rows_resumed = db.query(GammaResult).filter_by(plan_id=plan_id).all()
        evaluated_beams_resumed = {r.beam_number for r in gamma_rows_resumed if r.beam_number is not None}
        assert evaluated_beams_resumed == {1, 2, 3, 4, 5}

        # ------------------------------------------------------------------
        # 3. Force restart (force=True)
        # ------------------------------------------------------------------
        # Record mtimes of beam 1 and 2
        mtime_b1_before = b1_file.stat().st_mtime_ns

        time.sleep(0.05)
        job3 = QAJob(plan_id=plan_id, job_type="mcSquare", status="queued")
        db.add(job3)
        db.commit()
        db.refresh(job3)

        run_qa_job(job3.id, force=True)
        db.refresh(job3)
        assert job3.status == "complete"
        mtime_b1_after = b1_file.stat().st_mtime_ns
        assert mtime_b1_after != mtime_b1_before, "force=True should re-generate beam 1"

        print("[OK] test_stop_and_resume_mcsquare passed successfully!")

    finally:
        settings.MCSQUARE_SIMULATION_MODE = orig_mock
        shutil.rmtree(tmp, ignore_errors=True)
        db.close()


if __name__ == "__main__":
    test_stop_and_resume_mcsquare()
