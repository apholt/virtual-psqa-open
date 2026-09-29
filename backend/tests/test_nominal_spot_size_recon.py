"""
Unit test for log reconstruction nominal spot size mode (LOG_RECON_USE_NOMINAL_SPOT_SIZE).

Verifies that:
1. When use_nominal_spot_size=True, delivered dose is convolved using the plan's
   nominal spot size (isolating spot steering and meterset delivery accuracy).
2. When use_nominal_spot_size=False, delivered dose is convolved using the machine-reported
   spot size from the delivery record.
3. Machine-reported spot sizes and deltas (size_max_abs_diff_mm) are preserved and reported
   in both modes for machine QA tracking.
"""
import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileDataset
from pydicom.sequence import Sequence

from config import settings
from services.log_reconstruction import reconstruct_from_record
from services.log_reconstructor import _beam_spot_stats
from services.log_gamma import gamma_log_vs_rx


def _build_test_plan_and_record(tmp_path, nominal_size=8.0, delivered_size=9.2):
    plan_path = tmp_path / "RP.test.dcm"
    record_path = tmp_path / "RI.test.dcm"

    # Create synthetic RT Plan
    file_meta = Dataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.481.8"
    file_meta.MediaStorageSOPInstanceUID = "1.2.3.4.1"
    file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian

    plan_ds = FileDataset(str(plan_path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    plan_ds.SOPClassUID = file_meta.MediaStorageSOPClassUID
    plan_ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    plan_ds.Modality = "RTPLAN"
    plan_ds.PatientID = "TEST_PATIENT"

    beam = Dataset()
    beam.BeamName = "BEAM_1"
    beam.BeamNumber = 1

    cp0 = Dataset()
    cp0.NominalBeamEnergy = 150.0
    cp0.ScanningSpotSize = [nominal_size, nominal_size]
    # 4 spots in a 2x2 grid
    cp0.ScanSpotPositionMap = [-10.0, -10.0, 10.0, -10.0, -10.0, 10.0, 10.0, 10.0]
    cp0.ScanSpotMetersetWeights = [1.0, 1.0, 1.0, 1.0]

    cp1 = Dataset()  # odd control point (stop)
    cp1.NominalBeamEnergy = 150.0
    cp1.ScanningSpotSize = [nominal_size, nominal_size]
    cp1.ScanSpotPositionMap = []
    cp1.ScanSpotMetersetWeights = []

    beam.IonControlPointSequence = Sequence([cp0, cp1])
    plan_ds.IonBeamSequence = Sequence([beam])
    plan_ds.save_as(str(plan_path), write_like_original=False)

    # Create synthetic RT Ion Record
    rec_meta = Dataset()
    rec_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.481.9"
    rec_meta.MediaStorageSOPInstanceUID = "1.2.3.4.2"
    rec_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian

    rec_ds = FileDataset(str(record_path), {}, file_meta=rec_meta, preamble=b"\0" * 128)
    rec_ds.SOPClassUID = rec_meta.MediaStorageSOPClassUID
    rec_ds.SOPInstanceUID = rec_meta.MediaStorageSOPInstanceUID
    rec_ds.Modality = "RTRECORD"
    rec_ds.PatientID = "TEST_PATIENT"
    rec_ds.TreatmentDate = "20260929"

    m_item = Dataset()
    m_item.TreatmentMachineName = "TR1_Franklin_GR1"
    rec_ds.TreatmentMachineSequence = Sequence([m_item])

    r_beam = Dataset()
    r_beam.BeamName = "BEAM_1:TX"
    r_beam.TreatmentDeliveryType = "TREATMENT"

    # Delivered CP: exact positions, exact MU, but machine-measured spot size broadened
    rcp0 = Dataset()
    rcp0.NominalBeamEnergy = 150.0
    rcp0.ScanningSpotSize = [delivered_size, delivered_size]
    rcp0.ScanSpotPositionMap = [-10.0, -10.0, 10.0, -10.0, -10.0, 10.0, 10.0, 10.0]
    rcp0.ScanSpotMetersetsDelivered = [1.0, 1.0, 1.0, 1.0]
    rcp0.ScanSpotPrescribedIndices = [1, 2, 3, 4]

    r_beam.IonControlPointDeliverySequence = Sequence([rcp0])
    rec_ds.TreatmentSessionIonBeamSequence = Sequence([r_beam])
    rec_ds.save_as(str(record_path), write_like_original=False)

    return str(plan_path), str(record_path)


def test_nominal_vs_machine_spot_size_mode(tmp_path):
    plan_path, record_path = _build_test_plan_and_record(
        tmp_path, nominal_size=8.0, delivered_size=9.5
    )

    # 1. Reconstruct with nominal spot size (default)
    res_nom = reconstruct_from_record(plan_path, record_path, use_nominal_spot_size=True)
    beam_nom = res_nom.beams[0]

    # Prescribed and delivered profiles should be identical because positions and MU are identical
    diff_nom = np.max(np.abs(beam_nom.delivered_dose - beam_nom.prescribed_dose))
    assert diff_nom < 1e-5, f"Expected identical profiles with nominal spot sizes, got diff {diff_nom}"

    _, pass_rate_nom, fails_nom, _ = gamma_log_vs_rx(
        beam_nom.delivered_dose, beam_nom.prescribed_dose,
        dd_percent=2.0, dta_mm=2.0, resolution_mm=0.5,
    )
    assert pass_rate_nom >= 99.9, f"Expected 100% gamma pass rate in nominal mode, got {pass_rate_nom}%"

    # Check that spot metrics still record the real machine spot size (9.5 mm vs 8.0 mm)
    stats_nom = _beam_spot_stats(beam_nom)
    assert stats_nom["size_x_min_mm"] == 9.5
    assert stats_nom["size_x_max_mm"] == 9.5
    assert stats_nom["size_max_abs_diff_mm"] == 1.5
    assert stats_nom["size_is_plan_echo"] is False

    # 2. Reconstruct with machine spot size (use_nominal_spot_size=False)
    res_mach = reconstruct_from_record(plan_path, record_path, use_nominal_spot_size=False)
    beam_mach = res_mach.beams[0]

    # Delivered dose should differ due to spot broadening (9.5mm vs 8.0mm)
    rel_diff_pct = 100.0 * np.max(np.abs(beam_mach.delivered_dose - beam_mach.prescribed_dose)) / np.max(beam_mach.prescribed_dose)
    assert rel_diff_pct > 10.0, f"Expected relative dose difference from spot broadening, got {rel_diff_pct}%"

    _, pass_rate_mach, fails_mach, _ = gamma_log_vs_rx(
        beam_mach.delivered_dose, beam_mach.prescribed_dose,
        dd_percent=2.0, dta_mm=2.0, resolution_mm=0.5,
    )
    # The broadened spot should result in lower gamma pass rate
    assert pass_rate_mach < 85.0, f"Expected lower gamma due to 1.5mm broadening, got {pass_rate_mach}%"
