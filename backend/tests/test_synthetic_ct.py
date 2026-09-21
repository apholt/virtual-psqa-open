import os
import shutil
import sys
import tempfile
from pathlib import Path

# Ensure backend root is on sys.path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

import numpy as np
import pydicom
import pytest
from fastapi.testclient import TestClient
from config import settings
from database import SessionLocal
from main import app
from models.plan import Plan
from models.synthetic_ct import SyntheticCT
from services.dicom_ingestor import ingest_dicom_directory
from services.imaging import (
    VolumeGeometry,
    cbct_external_mask,
    build_virtual_ct,
    export_ct_series,
    resample_to_reference,
)
from services.synthetic_ct_service import (
    calculate_synthetic_ct_dose,
    calculate_synthetic_ct_dvh,
    generate_synthetic_ct,
    get_cbct_image_plane,
    get_sct_dose_plane,
    get_sct_gamma_plane,
    get_sct_image_plane,
    ingest_cbct_series,
    ingest_synthetic_ct_files,
)
from services.report_generator import build_synthetic_ct_report_html
from tests.synthetic_dicom_gen import write_synthetic_dicom_set


def create_test_ct_slice(
    output_path: Path,
    series_uid: str,
    slice_idx: int,
    z_pos: float,
    patient_id: str,
    study_uid: str,
    for_uid: str = "1.2.840.10008.1.1",
    hu_value: int = 0,
    modality: str = "CT",
):
    sop_uid = pydicom.uid.generate_uid()
    file_meta = pydicom.dataset.Dataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian

    ds = pydicom.dataset.FileDataset(str(output_path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.is_implicit_VR = False
    ds.is_little_endian = True
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.SOPInstanceUID = sop_uid
    ds.Modality = modality
    ds.PatientID = patient_id
    ds.PatientName = "SYNTHETIC^PATIENT"
    ds.PatientBirthDate = "19800101"
    ds.PatientSex = "M"
    ds.StudyID = "1"
    ds.StudyDate = "20260901"
    ds.StudyTime = "120000"
    ds.SeriesInstanceUID = series_uid
    ds.StudyInstanceUID = study_uid
    ds.FrameOfReferenceUID = for_uid
    ds.SeriesDescription = f"Test {modality} Scan"
    ds.InstanceNumber = slice_idx
    ds.Rows = 32
    ds.Columns = 32
    ds.PixelSpacing = [2.0, 2.0]
    ds.SliceThickness = 2.5
    ds.ImagePositionPatient = [-32.0, -32.0, z_pos]
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    ds.RescaleSlope = 1.0
    ds.RescaleIntercept = -1024.0
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    
    # Store cylinder/block in center
    arr = np.full((32, 32), -1000, dtype=np.int16)  # Air
    y, x = np.ogrid[:32, :32]
    mask = (x - 16) ** 2 + (y - 16) ** 2 <= 10 ** 2
    arr[mask] = hu_value  # Tissue HU
    
    stored = (arr.astype(np.int32) + 1024).astype(np.uint16)
    ds.PixelData = stored.tobytes()
    pydicom.dcmwrite(str(output_path), ds)
    return output_path


def test_imaging_core_unit():
    """Unit test VolumeGeometry, resampling, and body masking."""
    geom = VolumeGeometry(
        origin_lps=(0.0, 0.0, 0.0),
        spacing_mm=(2.0, 2.0, 2.0),
        size_voxels=(32, 32, 10),
    )
    # voxel (0,0,0) -> (0,0,0)
    assert np.allclose(geom.voxel_to_physical((0, 0, 0)), [0.0, 0.0, 0.0])
    # voxel (1,2,3) -> (2.0, 4.0, 6.0)
    assert np.allclose(geom.voxel_to_physical((1, 2, 3)), [2.0, 4.0, 6.0])
    assert np.allclose(geom.physical_to_voxel((2.0, 4.0, 6.0)), [1.0, 2.0, 3.0])

    # Test resample_to_reference
    arr = np.zeros((10, 32, 32), dtype=np.int16)
    arr[2:8, 8:24, 8:24] = 50  # Soft tissue HU
    resampled = resample_to_reference(arr, geom, geom)
    assert resampled.shape == arr.shape
    assert np.allclose(resampled[4, 16, 16], 50)

    # Test cbct_external_mask
    mask = cbct_external_mask(arr, geom.spacing_mm, threshold_hu=-300.0, open_mm=2.0, close_mm=2.0)
    assert mask.shape == arr.shape
    assert mask[4, 16, 16] is True or mask[4, 16, 16] == 1


def test_synthetic_ct_full_pipeline():
    settings.MCSQUARE_SIMULATION_MODE = True
    from services.auth_service import create_session_token
    token = create_session_token("admin")
    client = TestClient(app, cookies={settings.AUTH_SESSION_COOKIE: token})
    db = SessionLocal()
    try:
        # 1. Ingest a synthetic plan (includes planning RTDose)
        plan_tmp = tempfile.mkdtemp(prefix="sct_test_plan_")
        write_synthetic_dicom_set(plan_tmp, n_fields=2)
        
        # Extract patient_id and study_uid from the generated RTPlan
        rp_files = list(Path(plan_tmp).glob("RP.*.dcm"))
        assert rp_files, "No RTPlan generated"
        ds_rp = pydicom.dcmread(str(rp_files[0]), stop_before_pixels=True)
        patient_id = str(ds_rp.PatientID)
        pct_study_uid = str(ds_rp.StudyInstanceUID)

        # Add 6 planning CT slices into the plan upload directory so find_planning_ct_series succeeds
        pct_series_uid = pydicom.uid.generate_uid()
        for i in range(6):
            p = Path(plan_tmp) / f"CT_plan_{i:04d}.dcm"
            create_test_ct_slice(
                p, pct_series_uid, i + 1, i * 2.5, patient_id, pct_study_uid, hu_value=40
            )

        ingest_res = ingest_dicom_directory(plan_tmp, db)
        plan_id = ingest_res["plan_id"]
        assert plan_id is not None
        plan = db.query(Plan).filter_by(id=plan_id).first()

        # 2. Create 6 raw CBCT slices for Fraction 1 with slight offset
        cbct_tmp = Path(tempfile.mkdtemp(prefix="cbct_test_slices_"))
        cbct_series_uid = pydicom.uid.generate_uid()
        cbct_files = []
        for i in range(6):
            p = cbct_tmp / f"CBCT_{i:04d}.dcm"
            create_test_ct_slice(
                p, cbct_series_uid, i + 1, i * 2.5, patient_id, pydicom.uid.generate_uid(), hu_value=35
            )
            cbct_files.append(p)

        # 3. Ingest raw CBCT series
        sct, cbct_dir = ingest_cbct_series(plan_id, fraction_number=1, source_paths=cbct_files, db=db)
        assert sct.id is not None
        assert sct.fraction_number == 1
        assert sct.cbct_num_slices == 6
        assert sct.status == "cbct_uploaded"
        assert os.path.isdir(sct.cbct_dir)

        # 4. Generate synthetic CT from CBCT and TPCT using DIR
        sct = generate_synthetic_ct(plan_id, fraction_number=1, db=db, dir_method="demons", auto_calculate=False)
        assert sct.status == "contour_check"
        assert sct.num_slices == 6
        assert sct.dicom_dir is not None
        assert os.path.isdir(sct.dicom_dir)
        assert sct.dir_method == "demons"
        assert sct.mae_hu_after is not None

        # Verify external mask was saved
        mask_npz = Path(sct.dicom_dir).parent / "external_mask.npz"
        assert mask_npz.is_file()

        # 5. Calculate dose on generated synthetic CT (runs in mock mode)
        mc_path = calculate_synthetic_ct_dose(plan_id, fraction_number=1, db=db)
        assert os.path.exists(mc_path)

        # Verify DB record updated
        db.refresh(sct)
        assert sct.status == "complete"
        assert sct.gamma_passing_rate is not None
        assert sct.gamma_2mm_passing_rate is not None
        assert sct.gamma_passed is not None

        # 6. Verify plane extraction (sCT, CBCT, Dose, Gamma)
        dose_plane, max_d = get_sct_dose_plane(plan_id, fraction_number=1, z=0, db=db)
        assert dose_plane is not None
        assert max_d > 0

        ct_plane = get_sct_image_plane(plan_id, fraction_number=1, z=0, db=db)
        assert ct_plane is not None
        assert ct_plane.shape == (32, 32)

        cbct_plane = get_cbct_image_plane(plan_id, fraction_number=1, z=0, db=db)
        assert cbct_plane is not None
        assert cbct_plane.shape == (32, 32)

        gamma_plane, pass_r = get_sct_gamma_plane(plan_id, fraction_number=1, z=0, db=db)
        assert gamma_plane is not None
        assert pass_r >= 0

        # 7. Verify HTML report generation (including Deformed Target Coverage & DVH)
        html = build_synthetic_ct_report_html(plan_id, fraction_number=1, db=db)
        assert "SyntheticQACT Adaptive Setup &amp; Dose QA Report" in html
        assert "Adaptive Dose Verification Summary" in html
        assert "Fraction 1" in html
        assert "Deformed Target Coverage" in html
        assert "Adaptive DVH" in html

        # 8. Verify direct DVH computation on deformed targets
        dvh_res = calculate_synthetic_ct_dvh(plan_id, fraction_number=1, db=db)
        assert dvh_res["plan_id"] == plan_id
        assert dvh_res["fraction_number"] == 1
        assert dvh_res["overall_target_coverage"] in ("PASS", "WARNING", "FAIL")
        assert "overall_note" in dvh_res
        assert len(dvh_res["targets"]) + len(dvh_res["oars"]) > 0
        if dvh_res["targets"]:
            t0 = dvh_res["targets"][0]
            assert "d95" in t0["planned_metrics"]
            assert "d95" in t0["deformed_metrics"]
            assert "dvh" in t0
            assert len(t0["dvh"]["dose_bins_gy"]) > 0
            assert len(t0["dvh"]["sct_volume_pct"]) == len(t0["dvh"]["dose_bins_gy"])

        # 9. Test REST API endpoints
        res_list = client.get(f"/api/plans/{plan_id}/synthetic-ct")
        assert res_list.status_code == 200
        summaries = res_list.json()
        assert len(summaries) >= 1
        assert summaries[0]["fraction_number"] == 1
        assert summaries[0]["status"] == "complete"
        assert summaries[0]["has_cbct"] is True
        assert summaries[0]["has_dvh"] is True

        res_detail = client.get(f"/api/plans/{plan_id}/synthetic-ct/1")
        assert res_detail.status_code == 200
        detail = res_detail.json()
        assert detail["fraction_number"] == 1
        assert detail["has_dose"] is True
        assert detail["cbct_num_slices"] == 6
        assert detail["has_dvh"] is True

        # Test DVH endpoint
        res_dvh = client.get(f"/api/plans/{plan_id}/synthetic-ct/1/dvh")
        assert res_dvh.status_code == 200
        dvh_api_data = res_dvh.json()
        assert dvh_api_data["fraction_number"] == 1
        assert dvh_api_data["overall_target_coverage"] in ("PASS", "WARNING", "FAIL")

        # Test DVH recompute endpoint
        res_dvh_recompute = client.get(f"/api/plans/{plan_id}/synthetic-ct/1/dvh?recompute=true")
        assert res_dvh_recompute.status_code == 200
        assert res_dvh_recompute.json()["fraction_number"] == 1

        res_report = client.get(f"/api/reports/{plan_id}/synthetic-ct?fraction_number=1")
        assert res_report.status_code == 200
        assert "SyntheticQACT" in res_report.text
        assert "Deformed Target Coverage" in res_report.text

        res_dose_plane = client.get(f"/api/plans/{plan_id}/synthetic-ct/1/dose/plane/0")
        assert res_dose_plane.status_code == 200
        assert "X-Max-Dose" in res_dose_plane.headers

        res_ct_plane = client.get(f"/api/plans/{plan_id}/synthetic-ct/1/ct/plane/0")
        assert res_ct_plane.status_code == 200

        res_cbct_plane = client.get(f"/api/plans/{plan_id}/synthetic-ct/1/cbct/plane/0")
        assert res_cbct_plane.status_code == 200

        res_gamma_plane = client.get(f"/api/plans/{plan_id}/synthetic-ct/1/gamma/plane/0")
        assert res_gamma_plane.status_code == 200
        assert "X-Passing-Rate" in res_gamma_plane.headers

        # 10. Test Multi-Reference Comparison (TPS vs Baseline MCsquare)
        # Create baseline MC dose in mcSquare_output
        plan_mc_dir = Path(settings.RESULTS_PATH) / f"plan_{plan_id}" / "mcSquare_output"
        plan_mc_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(mc_path, plan_mc_dir / "mc_dose.npz")

        # Test detail endpoint with comparisons
        res_detail_2 = client.get(f"/api/plans/{plan_id}/synthetic-ct/1")
        assert res_detail_2.status_code == 200
        detail_2 = res_detail_2.json()
        assert "available_references" in detail_2
        assert any(r["id"] == "tps" for r in detail_2["available_references"])
        assert any(r["id"] == "mcsquare" for r in detail_2["available_references"])
        assert "comparisons" in detail_2
        assert "tps" in detail_2["comparisons"]
        assert "mcsquare" in detail_2["comparisons"]

        # Test Reference Dose Plane (TPS and MCsquare)
        res_ref_tps = client.get(f"/api/plans/{plan_id}/synthetic-ct/1/ref-dose/plane/0?reference=tps")
        assert res_ref_tps.status_code == 200
        assert "X-Max-Dose" in res_ref_tps.headers

        res_ref_mc = client.get(f"/api/plans/{plan_id}/synthetic-ct/1/ref-dose/plane/0?reference=mcsquare")
        assert res_ref_mc.status_code == 200
        assert "X-Max-Dose" in res_ref_mc.headers

        # Test Dose Difference Plane
        res_diff_tps = client.get(f"/api/plans/{plan_id}/synthetic-ct/1/dose-diff/plane/0?reference=tps")
        assert res_diff_tps.status_code == 200
        assert "X-Max-Diff" in res_diff_tps.headers

        res_diff_mc = client.get(f"/api/plans/{plan_id}/synthetic-ct/1/dose-diff/plane/0?reference=mcsquare")
        assert res_diff_mc.status_code == 200
        assert "X-Max-Diff" in res_diff_mc.headers

        # Test Gamma against MCsquare
        res_gamma_mc = client.get(f"/api/plans/{plan_id}/synthetic-ct/1/gamma/plane/0?reference=mcsquare")
        assert res_gamma_mc.status_code == 200
        assert "X-Passing-Rate" in res_gamma_mc.headers
        assert float(res_gamma_mc.headers["X-Passing-Rate"]) >= 95.0

        # Test DVH includes mcsquare comparison
        res_dvh_mc = client.get(f"/api/plans/{plan_id}/synthetic-ct/1/dvh?recompute=true")
        assert res_dvh_mc.status_code == 200
        dvh_data_mc = res_dvh_mc.json()
        assert "available_references" in dvh_data_mc
        if dvh_data_mc.get("targets"):
            t0 = dvh_data_mc["targets"][0]
            assert "mcsquare_volume_pct" in t0["dvh"]
            assert "mcsquare_metrics" in t0

        print("[SUCCESS] All SyntheticQACT, CBCT, Deformed DVH, and Multi-Reference Comparison tests passed!")
    finally:
        db.close()


if __name__ == "__main__":
    test_imaging_core_unit()
    test_synthetic_ct_full_pipeline()
