"""
Complexity feature extraction for proton PBS plans.

Computes the plan-complexity evidence layer entirely from the RTIonPlan DICOM —
no simulation required. These features feed the ML prediction engine and are
also displayed on the PlanDetail "complexity" evidence card.

Key metrics:
  MCS   — Modulation Complexity Score (Li et al. 2013, adapted for PBS).
  SAS   — Small Aperture Score: fraction of low-weight spots.
  MU/Gy — total monitor units per prescribed Gy (delivery efficiency / modulation).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

import numpy as np
import pydicom
from sqlalchemy.orm import Session

from config import settings
from models.plan import Plan

logger = logging.getLogger(__name__)


def get_beam_sequence(dcm: pydicom.Dataset):
    """Return (sequence, kind) for ion or conventional plans."""
    if hasattr(dcm, "IonBeamSequence"):
        return dcm.IonBeamSequence, "ion"
    if hasattr(dcm, "BeamSequence"):
        return dcm.BeamSequence, "conventional"
    raise ValueError("No beam sequence found in RTPLAN")


def _control_point_sequence(beam):
    """Return the control point sequence for an ion or conventional beam."""
    if hasattr(beam, "IonControlPointSequence"):
        return beam.IonControlPointSequence
    if hasattr(beam, "ControlPointSequence"):
        return beam.ControlPointSequence
    return []


def _spot_weights(cp) -> List[float]:
    """Return per-spot meterset weights for a control point (empty if none)."""
    weights = getattr(cp, "ScanSpotMetersetWeights", None)
    if weights is None:
        return []
    if hasattr(weights, "__iter__"):
        out = []
        for w in weights:
            try:
                out.append(float(w))
            except (TypeError, ValueError):
                continue
        return out
    try:
        return [float(weights)]
    except (TypeError, ValueError):
        return []


def _spot_positions(cp) -> Optional[np.ndarray]:
    """Return an (n,2) array of spot x,y positions (mm), or None."""
    raw = getattr(cp, "ScanSpotPositionMap", None)
    if raw is None:
        return None
    try:
        arr = np.asarray([float(v) for v in raw], dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if arr.size < 2:
        return None
    return arr.reshape(-1, 2)


def compute_mcs(beam_sequence) -> float:
    """
    Modulation Complexity Score (Li et al. 2013, adapted for PBS).

    Per layer: (1 - max_weight / sum_weights). The plan MCS is the mean over
    layers weighted by each layer's total MU. Lower MCS -> a few spots dominate
    delivery (highly modulated) -> higher QA failure risk.
    """
    layer_scores: List[float] = []
    layer_weights: List[float] = []
    for beam in beam_sequence:
        for cp in _control_point_sequence(beam):
            w = _spot_weights(cp)
            total = sum(w)
            if total <= 0 or len(w) == 0:
                continue
            score = 1.0 - (max(w) / total)
            layer_scores.append(score)
            layer_weights.append(total)
    if not layer_scores:
        return 0.5
    return float(np.average(layer_scores, weights=layer_weights))


def compute_sas(beam_sequence, threshold_mu: float = 0.005) -> float:
    """
    Small Aperture Score: fraction of all spots with meterset weight below
    threshold. High SAS -> many low-weight spots -> delivery accuracy risk.
    """
    n_total = 0
    n_small = 0
    for beam in beam_sequence:
        for cp in _control_point_sequence(beam):
            for w in _spot_weights(cp):
                n_total += 1
                if w < threshold_mu:
                    n_small += 1
    if n_total == 0:
        return 0.0
    return float(n_small / n_total)


def _prescribed_dose_gy(dcm: pydicom.Dataset) -> Optional[float]:
    """Best-effort prescribed (target) dose in Gy."""
    dose_ref = getattr(dcm, "DoseReferenceSequence", None)
    if dose_ref:
        for ref in dose_ref:
            val = getattr(ref, "TargetPrescriptionDose", None)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue
    return None


def _load_rtplan(plan: Plan) -> pydicom.Dataset:
    """Load the RTIonPlan dataset from the plan's dicom_store folder."""
    store = Path(plan.dicom_store_path)
    for f in store.glob("*.dcm"):
        try:
            dcm = pydicom.dcmread(str(f), stop_before_pixels=True)
        except Exception:
            continue
        if dcm.get("Modality", "") in ("RTPLAN", "RTIBTR") and (
            hasattr(dcm, "IonBeamSequence") or hasattr(dcm, "BeamSequence")
        ):
            return dcm
    raise FileNotFoundError(f"No RTPlan DICOM found in {store}")


def extract_features(plan_id: int, db: Session) -> dict:
    """
    Extract the complexity / plan-level portion of the canonical feature vector
    from the ingested RTIonPlan. Returns a feature dict (subset of the schema).
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan is None:
        raise ValueError(f"Plan {plan_id} not found")

    dcm = _load_rtplan(plan)
    beam_seq, _kind = get_beam_sequence(dcm)

    n_fields = len(beam_seq)
    n_fractions = float(plan.number_of_fractions or 0)

    total_spots = 0
    total_layers = 0
    total_mu = 0.0
    spots_per_layer: List[int] = []
    field_energy_ranges: List[float] = []
    field_sizes_cm2: List[float] = []

    # Beam meterset (MU) lookup from FractionGroupSequence if present.
    beam_mu = _beam_meterset_map(dcm)

    for idx, beam in enumerate(beam_seq):
        energies: List[float] = []
        xs: List[float] = []
        ys: List[float] = []
        beam_number = getattr(beam, "BeamNumber", None)
        cum_weight = 0.0
        final_cum = _final_cumulative_weight(beam)

        for cp in _control_point_sequence(beam):
            energy = getattr(cp, "NominalBeamEnergy", None)
            if energy is not None:
                try:
                    energies.append(float(energy))
                except (TypeError, ValueError):
                    pass
            pos = _spot_positions(cp)
            if pos is not None and pos.shape[0] > 0:
                total_layers += 1
                spots_per_layer.append(pos.shape[0])
                total_spots += pos.shape[0]
                xs.extend(pos[:, 0].tolist())
                ys.extend(pos[:, 1].tolist())
            cum_weight += sum(_spot_weights(cp))

        # MU for this beam: prefer FractionGroup meterset, else weight sum.
        mu = beam_mu.get(beam_number)
        if mu is None:
            mu = cum_weight
        total_mu += float(mu)

        if energies:
            field_energy_ranges.append(max(energies) - min(energies))
        if xs and ys:
            size_mm2 = (max(xs) - min(xs)) * (max(ys) - min(ys))
            field_sizes_cm2.append(size_mm2 / 100.0)

    mcs = compute_mcs(beam_seq)
    sas = compute_sas(beam_seq, threshold_mu=settings.COMPLEXITY_SAS_THRESHOLD)

    presc = _prescribed_dose_gy(dcm)
    if presc and presc > 0:
        mu_gy = total_mu / presc
    elif n_fractions > 0:
        mu_gy = total_mu / n_fractions  # proxy: MU per fraction
    else:
        mu_gy = 0.0

    mean_spots = float(np.mean(spots_per_layer)) if spots_per_layer else 0.0
    max_spots = float(np.max(spots_per_layer)) if spots_per_layer else 0.0
    mean_energy_range = float(np.mean(field_energy_ranges)) if field_energy_ranges else 0.0
    mean_mu_per_spot = float(total_mu / total_spots) if total_spots else 0.0
    mean_field_size = float(np.mean(field_sizes_cm2)) if field_sizes_cm2 else 0.0

    return {
        "n_fields": float(n_fields),
        "n_fractions": n_fractions,
        "total_spots": float(total_spots),
        "total_layers": float(total_layers),
        "total_mu": round(total_mu, 4),
        "mcs": round(mcs, 5),
        "sas": round(sas, 5),
        "mu_gy": round(mu_gy, 4),
        "mean_spots_per_layer": round(mean_spots, 3),
        "max_spots_per_layer": max_spots,
        "mean_energy_range_mev": round(mean_energy_range, 3),
        "mean_mu_per_spot": round(mean_mu_per_spot, 5),
        "mean_field_size_cm2": round(mean_field_size, 3),
    }


def _beam_meterset_map(dcm: pydicom.Dataset) -> dict:
    """Map beam number -> total beam meterset (MU) from FractionGroupSequence."""
    out: dict = {}
    fg_seq = getattr(dcm, "FractionGroupSequence", None)
    if not fg_seq:
        return out
    try:
        ref_beams = getattr(fg_seq[0], "ReferencedBeamSequence", [])
    except (IndexError, AttributeError):
        return out
    for rb in ref_beams:
        num = getattr(rb, "ReferencedBeamNumber", None)
        mu = getattr(rb, "BeamMeterset", None)
        if num is not None and mu is not None:
            try:
                out[int(num)] = float(mu)
            except (TypeError, ValueError):
                continue
    return out


def _final_cumulative_weight(beam) -> Optional[float]:
    val = getattr(beam, "FinalCumulativeMetersetWeight", None)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
