import sys
import tempfile
from pathlib import Path

# Ensure backend root is on sys.path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

import numpy as np
import pydicom
import pytest
from database import SessionLocal
from models.plan import Plan
from models.synthetic_ct import SyntheticCT
from services.synthetic_ct_service import ingest_cbct_series
from tests.test_synthetic_ct import create_test_ct_slice


def test_multi_series_disambiguation_with_reg():
    """Verify that when multiple CBCT series are uploaded, the one matching REG is selected."""
    db = SessionLocal()
    # Find or use an existing plan
    plan = db.query(Plan).first()
    if not plan:
        pytest.skip("No test plan found in database.")

    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        study_uid = pydicom.uid.generate_uid()
        series1_uid = pydicom.uid.generate_uid()
        series2_uid = pydicom.uid.generate_uid()
        for1 = "1.2.840.10008.1.101"
        for2 = "1.2.840.10008.1.102"

        files = []
        # Create 5 slices for series 1 (setup scan)
        for i in range(5):
            f1 = tmp_dir / f"s1_{i}.dcm"
            create_test_ct_slice(
                f1, series1_uid, i + 1, z_pos=i * 2.0, patient_id="TEST", study_uid=study_uid, for_uid=for1
            )
            files.append(f1)

        # Create 5 slices for series 2 (verification scan)
        for i in range(5):
            f2 = tmp_dir / f"s2_{i}.dcm"
            create_test_ct_slice(
                f2, series2_uid, i + 1, z_pos=i * 2.0, patient_id="TEST", study_uid=study_uid, for_uid=for2
            )
            files.append(f2)

        # Create a mock REG file pointing to series 2
        reg_file = tmp_dir / "reg.dcm"
        reg_sop = pydicom.uid.generate_uid()
        reg_meta = pydicom.dataset.Dataset()
        reg_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.66.1"
        reg_meta.MediaStorageSOPInstanceUID = reg_sop
        reg_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian
        reg_ds = pydicom.dataset.FileDataset(str(reg_file), {}, file_meta=reg_meta, preamble=b"\0" * 128)
        reg_ds.Modality = "REG"
        reg_ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.66.1"
        reg_ds.SOPInstanceUID = reg_sop
        reg_ds.SeriesInstanceUID = pydicom.uid.generate_uid()
        reg_ds.StudyInstanceUID = study_uid
        reg_ds.PatientID = "TEST"
        reg_ds.PatientName = "SYNTHETIC^PATIENT"

        reg_seq_item = pydicom.dataset.Dataset()
        reg_seq_item.FrameOfReferenceUID = for2
        mat_reg_seq_item = pydicom.dataset.Dataset()
        mat_seq_item = pydicom.dataset.Dataset()
        mat_seq_item.FrameOfReferenceTransformationMatrix = list(np.eye(4).flatten())
        mat_reg_seq_item.MatrixSequence = [mat_seq_item]
        reg_seq_item.MatrixRegistrationSequence = [mat_reg_seq_item]
        reg_ds.RegistrationSequence = [reg_seq_item]
        reg_ds.save_as(str(reg_file))
        files.append(reg_file)

        # Ingest files
        sct, dest = ingest_cbct_series(plan.id, 999, files, db)
        assert sct.cbct_num_slices == 5
        assert sct.cbct_series_instance_uid == series2_uid
        raw_files = list(dest.glob("*.dcm"))
        assert len(raw_files) == 5

        # Clean up test fraction 999
        db.delete(sct)
        db.commit()
