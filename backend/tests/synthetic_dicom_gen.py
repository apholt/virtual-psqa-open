"""
PHI-free synthetic DICOM generator for testing the ingestion pipeline.

All patient demographics are fake. Spot distributions follow 2D Gaussians.
Generated files can be dropped into the watched folder or uploaded via the UI.
"""
from __future__ import annotations

import os
import random
import string
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileDataset
from pydicom.sequence import Sequence
from pydicom.uid import generate_uid


def _base_dataset(sop_class_uid: str, sop_instance_uid: str) -> FileDataset:
    """Creates a minimal DICOM file dataset with required meta headers."""
    file_meta = Dataset()
    file_meta.MediaStorageSOPClassUID = sop_class_uid
    file_meta.MediaStorageSOPInstanceUID = sop_instance_uid
    file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian

    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.is_implicit_VR = False
    ds.is_little_endian = True
    ds.SOPClassUID = sop_class_uid
    ds.SOPInstanceUID = sop_instance_uid

    now = datetime.now()
    ds.StudyDate = now.strftime("%Y%m%d")
    ds.StudyTime = now.strftime("%H%M%S")
    ds.ContentDate = ds.StudyDate
    ds.ContentTime = ds.StudyTime
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.SpecificCharacterSet = "ISO_IR 192"
    return ds


def _fake_patient_tags(ds: Dataset) -> None:
    """Inserts synthetic (non-PHI) patient demographics."""
    idx = random.randint(1, 999)
    ds.PatientID = f"SYNTHETIC_{idx:03d}"
    ds.PatientName = f"SYNTHETIC^PATIENT^{idx:03d}"
    ds.PatientBirthDate = "19800101"
    ds.PatientSex = random.choice(["M", "F"])


def generate_synthetic_rtionplan(
    n_fields: int = 3,
    n_layers_per_field: int = 20,
    n_spots_per_layer: int = 50,
    energy_range: tuple = (70, 220),
) -> tuple[FileDataset, str]:
    """
    Creates a synthetic RTIonPlan dataset.
    Returns (dataset, plan_uid).
    """
    plan_uid = generate_uid()
    ds = _base_dataset("1.2.840.10008.5.1.4.1.1.481.8", plan_uid)
    _fake_patient_tags(ds)

    ds.Modality = "RTPLAN"
    ds.RTPlanLabel = "SYNTHETIC_PLAN"
    ds.RTPlanName = "Synthetic PBS Plan"
    ds.RTPlanDate = ds.StudyDate
    ds.RTPlanTime = ds.StudyTime
    ds.RTPlanGeometry = "PATIENT"

    # Fraction group
    fg = Dataset()
    fg.FractionGroupNumber = 1
    fg.NumberOfFractionsPlanned = 30
    fg.NumberOfBeams = n_fields
    fg.NumberOfBrachyApplicationSetups = 0

    ref_beams = []
    for field_idx in range(n_fields):
        rb = Dataset()
        rb.ReferencedBeamNumber = field_idx + 1
        rb.BeamMeterset = 100.0
        ref_beams.append(rb)
    fg.ReferencedBeamSequence = Sequence(ref_beams)

    ds.FractionGroupSequence = Sequence([fg])


    # Ion beams
    beams = []
    for field_idx in range(n_fields):
        beam = Dataset()
        beam.BeamNumber = field_idx + 1
        beam.BeamName = f"G{field_idx * 60:03d}"
        beam.BeamType = "STATIC"
        beam.RadiationType = "PROTON"
        beam.TreatmentDeliveryType = "TREATMENT"
        beam.ScanMode = "MODULATED"
        beam.NumberOfRangeShifters = 0
        beam.FinalCumulativeMetersetWeight = 100.0
        beam.TreatmentMachineName = "SYNTHETIC_GANTRY"

        beam.NumberOfControlPoints = n_layers_per_field * 2  # 2 CP per layer

        cp_seq = []
        gantry_angle = field_idx * (360.0 / n_fields)
        energy = float(np.random.uniform(*energy_range))

        for layer_idx in range(n_layers_per_field):
            # Start CP
            cp = Dataset()
            cp.ControlPointIndex = layer_idx * 2
            cp.NominalBeamEnergy = round(energy - layer_idx * 2.0, 1)
            cp.GantryAngle = gantry_angle
            cp.GantryRotationDirection = "NONE"
            cp.BeamLimitingDeviceAngle = 0.0
            cp.PatientSupportAngle = 0.0
            cp.IsocenterPosition = [0.0, 0.0, 0.0]

            # Gaussian spot distribution
            sigma = 30.0
            spots_x = np.random.normal(0, sigma, n_spots_per_layer)
            spots_y = np.random.normal(0, sigma, n_spots_per_layer)
            positions = []
            for x, y in zip(spots_x, spots_y):
                positions.extend([round(float(x), 2), round(float(y), 2)])
            cp.ScanSpotPositionMap = positions
            weights = np.abs(np.random.normal(0.1, 0.02, n_spots_per_layer))
            cp.ScanSpotMetersetWeights = [round(float(w), 4) for w in weights]
            cp.NumberOfScanSpotPositions = n_spots_per_layer
            cp.ScanningSpotSize = [5.0, 5.0]
            cp_seq.append(cp)

            # End CP (minimal)
            cp_end = Dataset()
            cp_end.ControlPointIndex = layer_idx * 2 + 1
            cp_end.CumulativeMetersetWeight = float(np.sum(weights))
            cp_seq.append(cp_end)

        beam.IonControlPointSequence = Sequence(cp_seq)
        beam.FinalCumulativeMetersetWeight = float(
            sum(float(np.sum(np.abs(np.random.normal(0.1, 0.02, n_spots_per_layer))))
                for _ in range(n_layers_per_field))
        )
        beams.append(beam)

    ds.IonBeamSequence = Sequence(beams)
    return ds, plan_uid


def generate_synthetic_rtdose(
    rtplan: FileDataset,
    grid_size: tuple = (100, 100, 50),
) -> FileDataset:
    """
    Creates a synthetic RTDose with a realistic Gaussian dose distribution.
    """
    dose_uid = generate_uid()
    ds = _base_dataset("1.2.840.10008.5.1.4.1.1.481.2", dose_uid)
    _fake_patient_tags(ds)
    ds.PatientID = rtplan.PatientID
    ds.PatientName = rtplan.PatientName

    ds.Modality = "RTDOSE"
    ds.DoseUnits = "GY"
    ds.DoseType = "PHYSICAL"
    ds.DoseSummationType = "PLAN"
    ds.DoseGridScaling = 0.0001

    # Reference the plan
    ref_plan = Dataset()
    ref_plan.ReferencedSOPClassUID = rtplan.SOPClassUID
    ref_plan.ReferencedSOPInstanceUID = rtplan.SOPInstanceUID
    ds.ReferencedRTPlanSequence = Sequence([ref_plan])

    n_z, n_y, n_x = grid_size
    ds.Rows = n_y
    ds.Columns = n_x
    ds.NumberOfFrames = n_z
    ds.PixelSpacing = [2.0, 2.0]
    ds.SliceThickness = 2.0
    ds.ImagePositionPatient = [-100.0, -100.0, -50.0]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.FrameIncrementPointer = pydicom.tag.Tag(0x3004, 0x000C)
    ds.GridFrameOffsetVector = [float(i * 2) for i in range(n_z)]

    # Synthetic dose: sum of 3D Gaussians (one per field)
    x = np.linspace(-100, 100, n_x)
    y = np.linspace(-100, 100, n_y)
    z = np.linspace(-50, 50, n_z)
    xx, yy, zz = np.meshgrid(x, y, z, indexing="ij")
    dose = np.zeros((n_x, n_y, n_z))

    n_fields = len(getattr(rtplan, "IonBeamSequence", []) or [])
    for _ in range(max(n_fields, 1)):
        cx, cy, cz = random.uniform(-20, 20), random.uniform(-20, 20), random.uniform(-10, 10)
        dose += np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2 + (zz - cz) ** 2) / (2 * 30**2))

    dose_3d = np.transpose(dose, (2, 1, 0))  # (n_z, n_y, n_x)
    max_dose = dose_3d.max()
    pixel_array = (dose_3d / max_dose / float(ds.DoseGridScaling)).astype(np.uint32)

    ds.BitsAllocated = 32
    ds.BitsStored = 32
    ds.HighBit = 31
    ds.PixelRepresentation = 0
    ds.PixelData = pixel_array.tobytes()
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"

    return ds


def generate_synthetic_rtrecord(
    rtplan: FileDataset,
    position_noise_mm: float = 0.1,
    mu_noise_percent: float = 0.5,
) -> FileDataset:
    """
    Creates an RT Ion Beam Treatment Record from the RTIonPlan with small
    random noise on spot positions and MU to simulate real delivery.
    """
    record_uid = generate_uid()
    ds = _base_dataset("1.2.840.10008.5.1.4.1.1.481.9", record_uid)
    _fake_patient_tags(ds)
    ds.PatientID = rtplan.PatientID
    ds.PatientName = rtplan.PatientName

    ds.Modality = "RTRECORD"
    ds.TreatmentDate = ds.StudyDate
    ds.TreatmentTime = ds.StudyTime

    beam_records = []
    for beam in getattr(rtplan, "IonBeamSequence", []):
        beam_rec = Dataset()
        beam_rec.BeamName = beam.BeamName
        beam_rec.TreatmentDeliveryType = "TREATMENT"

        cp_deliveries = []
        for cp in getattr(beam, "IonControlPointSequence", []):
            cp_del = Dataset()
            cp_del.ControlPointIndex = cp.ControlPointIndex

            planned_positions = list(getattr(cp, "ScanSpotPositionMap", []))
            if planned_positions:
                noise = np.random.normal(0, position_noise_mm, len(planned_positions))
                cp_del.ScanSpotPositionMap = [
                    round(p + float(n), 3) for p, n in zip(planned_positions, noise)
                ]
            else:
                cp_del.ScanSpotPositionMap = []

            planned_mu = list(getattr(cp, "ScanSpotMetersetWeights", []))
            if planned_mu:
                mu_noise = np.random.normal(1.0, mu_noise_percent / 100, len(planned_mu))
                cp_del.ScanSpotMetersetWeights = [
                    round(float(m) * float(n), 4) for m, n in zip(planned_mu, mu_noise)
                ]
            else:
                cp_del.ScanSpotMetersetWeights = []

            if hasattr(cp, "NominalBeamEnergy"):
                cp_del.NominalBeamEnergy = cp.NominalBeamEnergy

            cp_deliveries.append(cp_del)

        beam_rec.IonControlPointDeliverySequence = Sequence(cp_deliveries)
        beam_records.append(beam_rec)

    ds.TreatmentSessionIonBeamSequence = Sequence(beam_records)
    return ds


def generate_synthetic_rtstruct(
    rtplan: FileDataset,
    rtdose: FileDataset,
) -> FileDataset:
    """
    Creates a synthetic RTSTRUCT dataset containing PTV_High, SpinalCord, and External contours.
    """
    struct_uid = generate_uid()
    ds = _base_dataset("1.2.840.10008.5.1.4.1.1.481.3", struct_uid)
    _fake_patient_tags(ds)
    ds.PatientID = rtplan.PatientID
    ds.PatientName = rtplan.PatientName

    ds.Modality = "RTSTRUCT"
    ds.StructureSetLabel = "SYNTHETIC_STRUCTS"
    ds.StructureSetName = "Planning Contours"
    ds.StructureSetDate = ds.StudyDate
    ds.StructureSetTime = ds.StudyTime

    # Define ROIs
    rois = [
        {"num": 1, "name": "PTV_High", "type": "PTV", "color": [220, 40, 40], "radius": 28.0, "offset": (0.0, 0.0)},
        {"num": 2, "name": "SpinalCord", "type": "ORGAN", "color": [40, 140, 240], "radius": 8.0, "offset": (0.0, -35.0)},
        {"num": 3, "name": "External", "type": "EXTERNAL", "color": [50, 180, 50], "radius": 75.0, "offset": (0.0, 0.0)},
    ]

    roi_seq = []
    obs_seq = []
    contour_seq = []

    # Get slice Z levels from RTDose
    n_planes = getattr(rtdose, "NumberOfFrames", 20)
    grid_offsets = list(getattr(rtdose, "GridFrameOffsetVector", range(n_planes)))
    ipp = [float(v) for v in getattr(rtdose, "ImagePositionPatient", [0.0, 0.0, 0.0])]

    for r in rois:
        # StructureSetROISequence item
        s_item = Dataset()
        s_item.ROINumber = r["num"]
        s_item.ReferencedFrameOfReferenceUID = generate_uid()
        s_item.ROIName = r["name"]
        s_item.ROIGenerationAlgorithm = "AUTOMATIC"
        roi_seq.append(s_item)

        # RTROIObservationsSequence item
        o_item = Dataset()
        o_item.ObservationNumber = r["num"]
        o_item.ReferencedROINumber = r["num"]
        o_item.RTROIInterpretedType = r["type"]
        o_item.ROIInterpreter = "VirtualPSQA"
        obs_seq.append(o_item)

        # ROIContourSequence item
        c_item = Dataset()
        c_item.ReferencedROINumber = r["num"]
        c_item.ROIDisplayColor = r["color"]

        slices = []
        # Draw contour on slices around center
        mid = n_planes // 2
        z_start = max(0, mid - 5)
        z_end = min(n_planes, mid + 6)

        theta = np.linspace(0, 2 * np.pi, 16, endpoint=False)
        rad = r["radius"]
        ox, oy = r["offset"]

        for z_idx in range(z_start, z_end):
            z_coord = ipp[2] + float(grid_offsets[z_idx])
            pts = []
            for t in theta:
                px = ipp[0] + ox + rad * np.cos(t)
                py = ipp[1] + oy + rad * np.sin(t)
                pts.extend([round(float(px), 2), round(float(py), 2), round(float(z_coord), 2)])

            sl = Dataset()
            sl.ContourGeometricType = "CLOSED_PLANAR"
            sl.NumberOfContourPoints = len(theta)
            sl.ContourData = pts
            slices.append(sl)

        c_item.ContourSequence = Sequence(slices)
        contour_seq.append(c_item)

    ds.StructureSetROISequence = Sequence(roi_seq)
    ds.RTROIObservationsSequence = Sequence(obs_seq)
    ds.ROIContourSequence = Sequence(contour_seq)
    return ds


def write_synthetic_dicom_set(output_dir: str, n_fields: int = 3, include_rtstruct: bool = True) -> dict[str, str]:
    """
    Generates a complete set of synthetic DICOM files and writes them to output_dir.
    Returns dict mapping modality → file path.
    """
    os.makedirs(output_dir, exist_ok=True)
    rtplan, plan_uid = generate_synthetic_rtionplan(n_fields=n_fields)
    rtdose = generate_synthetic_rtdose(rtplan)
    rtrecord = generate_synthetic_rtrecord(rtplan)

    items = [(rtplan, "RP"), (rtdose, "RD"), (rtrecord, "RI")]
    if include_rtstruct:
        rtstruct = generate_synthetic_rtstruct(rtplan, rtdose)
        items.append((rtstruct, "RS"))

    paths = {}
    for ds, name in items:
        path = str(Path(output_dir) / f"{name}.{ds.SOPInstanceUID}.dcm")
        pydicom.dcmwrite(path, ds)
        paths[name] = path

    return paths


if __name__ == "__main__":
    out = tempfile.mkdtemp(prefix="synthetic_psqa_")
    paths = write_synthetic_dicom_set(out, n_fields=2)
    print(f"Synthetic DICOM set written to: {out}")
    for k, v in paths.items():
        print(f"  {k}: {v}")
