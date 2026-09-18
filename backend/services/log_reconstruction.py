"""
backend/services/log_reconstruction.py -- Proton delivery log dose reconstruction.

Extracts planned and delivered spot delivery parameters from DICOM RT Ion Plans
and RT Ion Records (delivery logs) to reconstruct 2D spot fluence profiles at the
isocenter plane via analytic 2D Gaussian summation. Computes per-spot delivery
metrics (spot positioning deviations, meterset deviations, and spot size comparisons).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pydicom

logger = logging.getLogger(__name__)

# Reconstruction grid (mm). 150 x 150 grid at 1 mm resolution spanning -150 to +149.5 mm
GRID_N = 150
GRID_MIN_MM = -150.0
GRID_MAX_MM = 149.5

# Optional machine aliases mapping
MACHINE_NAMES: dict[str, str] = {}


def _format_machine_name(raw_name: str) -> str:
    """Format raw TreatmentMachineName into a clean, friendly identifier."""
    if not raw_name or raw_name == "unknown":
        return "Proton_Gantry"
    if raw_name in MACHINE_NAMES:
        return MACHINE_NAMES[raw_name]
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in raw_name.strip())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned


def _normalize_beam_name(name: str) -> str:
    """Strip the ':TX'-style delivery suffix so record beams match plan beams.

    'LA:TX' -> 'LA', 'RA:TX' -> 'RA', 'LA' -> 'LA'.
    """
    return str(name).split(":")[0].strip()


@dataclass
class BeamReconstruction:
    beam_name: str                      # normalized (plan) name
    record_beam_name: str               # raw record name (e.g. 'LA:TX')
    prescribed_dose: np.ndarray         # (150,150) MU profile
    delivered_dose: np.ndarray          # (150,150) MU profile
    n_spots_prescribed: int
    n_spots_delivered: int
    total_mu_prescribed: float
    total_mu_delivered: float
    # Per-spot machine metrics (delivered - prescribed), matched by spot index:
    spot_x_offsets_mm: np.ndarray       # delivered_x - prescribed_x
    spot_y_offsets_mm: np.ndarray
    spot_mu_errors: np.ndarray          # delivered_mu - prescribed_mu
    spot_prescribed_mu: np.ndarray
    # Matched per-spot spot sizes [x, y] mm (plan nominal vs record-reported).
    # NOTE: on some machines the record echoes the plan's nominal sizes back
    # per layer; if delivered == prescribed everywhere, the record is an echo
    # and size cannot drift -- the stats layer flags this.
    spot_size_rx_mm: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))
    spot_size_dv_mm: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))


@dataclass
class LogReconstructionResult:
    patient_id: str
    machine_name: str                   # friendly id from MACHINE_NAMES
    raw_machine_name: str
    treatment_date: str
    beams: list[BeamReconstruction] = field(default_factory=list)


# --------------------------------------------------------------------------
# Spot extraction
# --------------------------------------------------------------------------

def _extract_prescribed_spots(plan_ds: pydicom.Dataset, beam_index: int) -> np.ndarray:
    """Prescribed spots for one beam.

    Returns array cols: [Energy, RxMU, SizeX, SizeY, PosX, PosY, Layer].
    Extracts spots from IonControlPointSequence (reading even indices for
    paired start/stop control points).
    """
    beam = plan_ds.IonBeamSequence[beam_index]
    cps = beam.IonControlPointSequence
    n_layers = int(len(cps) / 2)

    total = 0
    for i in range(n_layers):
        w = cps[2 * i].ScanSpotMetersetWeights
        total += len(w) if hasattr(w, "__len__") else 1

    out = np.zeros((total, 7))
    s = 0
    for i in range(n_layers):
        cp = cps[2 * i]
        weights = cp.ScanSpotMetersetWeights
        n = len(weights) if hasattr(weights, "__len__") else 1
        pmap = cp.ScanSpotPositionMap
        px = pmap[::2]
        py = pmap[1::2]
        energy = float(cp.NominalBeamEnergy)
        size = cp.ScanningSpotSize
        sx, sy = float(size[0]), float(size[1])
        for j in range(n):
            out[s, 0] = energy
            out[s, 1] = weights[j] if hasattr(weights, "__len__") else weights
            out[s, 2] = sx
            out[s, 3] = sy
            out[s, 4] = px[j]
            out[s, 5] = py[j]
            out[s, 6] = i
            s += 1
    return out


def _extract_delivered_spots(record_ds: pydicom.Dataset, beam_index: int) -> np.ndarray:
    """Delivered spots for one beam from the RT Ion Record.

    Returns array cols: [SpotIndex, Energy, DeliveredMU, SizeX, SizeY, PosX, PosY, Layer].
    Extracts delivered spots from IonControlPointDeliverySequence, handling both
    compliant (paired start/stop CPs) and single CP per layer formats.
    """
    tsibs = record_ds.TreatmentSessionIonBeamSequence
    beam = tsibs[beam_index]
    cps = beam.IonControlPointDeliverySequence
    n_cp = len(cps)

    # Detect format: compliant has even (start) control points with all-zero
    # delivered metersets and odd (stop) control points with the real data.
    is_compliant = False
    if n_cp >= 2:
        try:
            first = cps[0].ScanSpotMetersetsDelivered
            if first is not None and hasattr(first, "__len__"):
                if all(v == 0 for v in first):
                    is_compliant = True
        except (AttributeError, IndexError, TypeError):
            pass

    if is_compliant:
        n_layers = int(n_cp / 2)
        offset, step = 1, 2
    else:
        n_layers = n_cp
        offset, step = 0, 1

    total = 0
    for i in range(n_layers):
        idx = offset + i * step
        try:
            total += len(cps[idx].ScanSpotMetersetsDelivered)
        except TypeError:
            total += 1

    out = np.zeros((total, 8))
    s = 0
    for i in range(n_layers):
        idx = offset + i * step
        cp = cps[idx]
        delivered = cp.ScanSpotMetersetsDelivered
        n = len(delivered) if hasattr(delivered, "__len__") else 1
        energy = float(cp.NominalBeamEnergy)
        pmap = cp.ScanSpotPositionMap
        px = pmap[::2]
        py = pmap[1::2]
        indices = cp.ScanSpotPrescribedIndices
        size = cp.ScanningSpotSize
        sx, sy = float(size[0]), float(size[1])
        for j in range(n):
            out[s, 0] = indices[j] if hasattr(indices, "__len__") else indices
            out[s, 1] = energy
            out[s, 2] = delivered[j] if hasattr(delivered, "__len__") else delivered
            out[s, 3] = sx
            out[s, 4] = sy
            out[s, 5] = px[j]
            out[s, 6] = py[j]
            out[s, 7] = i
            s += 1
    return out


# --------------------------------------------------------------------------
# Dose reconstruction (2D Gaussian spot sum)
# --------------------------------------------------------------------------

def _reconstruct_profile(
    xo: np.ndarray, yo: np.ndarray,
    sigma_x: np.ndarray, sigma_y: np.ndarray, amplitude: np.ndarray,
) -> np.ndarray:
    """Sum per-spot 2D Gaussians onto the reconstruction grid.

    Each spot's Gaussian is normalized to unit sum then scaled to its meterset.
    """
    xmm = np.linspace(GRID_MIN_MM, GRID_MAX_MM, GRID_N)
    ymm = np.linspace(GRID_MIN_MM, GRID_MAX_MM, GRID_N)
    xi, yi = np.meshgrid(xmm, ymm)

    dose = np.zeros((GRID_N, GRID_N))
    for i in range(len(amplitude)):
        sx = sigma_x[i]
        sy = sigma_y[i]
        if sx <= 0 or sy <= 0 or amplitude[i] == 0:
            continue
        a = 1.0 / (2 * sx * sx)
        c = 1.0 / (2 * sy * sy)
        g = np.exp(-(a * (xi - xo[i]) ** 2 + c * (yi - yo[i]) ** 2))
        total = np.sum(g)
        if total <= 0:
            continue
        dose += (g / total) * amplitude[i]
    return dose


# --------------------------------------------------------------------------
# Per-spot machine metrics
# --------------------------------------------------------------------------

def _spot_metrics(prescribed: np.ndarray, delivered: np.ndarray) -> tuple:
    """Delivered - prescribed per-spot position, MU, and size metrics.

    Matched by ordinal spot order after dropping test pulses (SpotIndex == 0)
    from the delivered set.
    Prescribed cols: [Energy,RxMU,SizeX,SizeY,PosX,PosY,Layer]
    Delivered cols:  [SpotIndex,Energy,DeliveredMU,SizeX,SizeY,PosX,PosY,Layer]

    Returns (x_off, y_off, mu_err, rx_mu, rx_sizes, dv_sizes) where the size
    arrays are matched (n, 2) [x, y] mm.
    """
    # Drop test pulses (delivered SpotIndex == 0) then drop the index column.
    dmask = delivered[:, 0] != 0
    d = delivered[dmask][:, 1:]  # -> [Energy,MU,SizeX,SizeY,PosX,PosY,Layer]

    n = min(len(prescribed), len(d))
    if n == 0:
        empty = np.zeros(0)
        empty2 = np.zeros((0, 2))
        return empty, empty, empty, empty, empty2, empty2

    rx = prescribed[:n]
    dv = d[:n]
    x_off = dv[:, 4] - rx[:, 4]      # delivered PosX - prescribed PosX
    y_off = dv[:, 5] - rx[:, 5]
    mu_err = dv[:, 1] - rx[:, 1]     # delivered MU - prescribed MU
    rx_mu = rx[:, 1]
    rx_sizes = rx[:, 2:4].copy()     # plan nominal spot size [x, y]
    dv_sizes = dv[:, 2:4].copy()     # record-reported spot size [x, y]
    return x_off, y_off, mu_err, rx_mu, rx_sizes, dv_sizes


# --------------------------------------------------------------------------
# Top-level entry point
# --------------------------------------------------------------------------

def reconstruct_from_record(plan_path: str, record_path: str) -> LogReconstructionResult:
    """Reconstruct delivered vs prescribed dose for every matched beam.

    Pairs record beams to plan beams by normalized name (stripping ':TX').
    """
    plan_ds = pydicom.dcmread(plan_path, force=True)
    record_ds = pydicom.dcmread(record_path, force=True)

    raw_machine = "unknown"
    try:
        raw_machine = str(record_ds.TreatmentMachineSequence[0].TreatmentMachineName)
    except Exception:
        pass
    machine = _format_machine_name(raw_machine)

    treatment_date = str(getattr(record_ds, "TreatmentDate", ""))
    patient_id = str(getattr(record_ds, "PatientID", ""))

    # Build normalized-name -> (index, canonical_plan_name) maps for both sides.
    plan_beams = {
        _normalize_beam_name(b.BeamName).upper(): (idx, str(b.BeamName))
        for idx, b in enumerate(plan_ds.IonBeamSequence)
    }
    record_beams = list(record_ds.TreatmentSessionIonBeamSequence)

    result = LogReconstructionResult(
        patient_id=patient_id,
        machine_name=machine,
        raw_machine_name=raw_machine,
        treatment_date=treatment_date,
    )

    for r_idx, r_beam in enumerate(record_beams):
        raw_name = str(r_beam.BeamName)
        norm_key = _normalize_beam_name(raw_name).upper()
        if norm_key not in plan_beams:
            logger.warning(
                f"Record beam '{raw_name}' (normalized '{norm_key}') has no matching "
                f"plan beam; skipping. Plan beams: {list(plan_beams)}"
            )
            continue
        p_idx, plan_beam_name = plan_beams[norm_key]

        rx = _extract_prescribed_spots(plan_ds, p_idx)
        dv = _extract_delivered_spots(record_ds, r_idx)

        # Prescribed profile
        rx_dose = _reconstruct_profile(
            rx[:, 4], rx[:, 5],
            rx[:, 2] / 2.355, rx[:, 3] / 2.355, rx[:, 1],
        )

        # Delivered profile -- drop test pulses (SpotIndex == 0) and zero-MU spots
        dmask = dv[:, 0] != 0
        d = dv[dmask]
        d_dose = _reconstruct_profile(
            d[:, 5], d[:, 6],
            d[:, 3] / 2.355, d[:, 4] / 2.355, d[:, 2],
        )

        x_off, y_off, mu_err, rx_mu, rx_sizes, dv_sizes = _spot_metrics(rx, dv)

        result.beams.append(BeamReconstruction(
            beam_name=plan_beam_name,
            record_beam_name=raw_name,
            prescribed_dose=rx_dose,
            delivered_dose=d_dose,
            n_spots_prescribed=len(rx),
            n_spots_delivered=int(np.sum(dmask)),
            total_mu_prescribed=float(np.sum(rx[:, 1])),
            total_mu_delivered=float(np.sum(d[:, 2])),
            spot_x_offsets_mm=x_off,
            spot_y_offsets_mm=y_off,
            spot_mu_errors=mu_err,
            spot_prescribed_mu=rx_mu,
            spot_size_rx_mm=rx_sizes,
            spot_size_dv_mm=dv_sizes,
        ))
        logger.info(
            f"Reconstructed beam '{plan_beam_name}': "
            f"{len(rx)} prescribed / {int(np.sum(dmask))} delivered spots, "
            f"MU {np.sum(rx[:, 1]):.1f} -> {np.sum(d[:, 2]):.1f}"
        )

    return result
