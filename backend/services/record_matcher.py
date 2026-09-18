"""
Match an arriving RT Ion Record to a stored plan.

Real-world DICOM lineage is unreliable: RayStation re-exports plans with fresh
SOPInstanceUIDs, while the delivery system's record references the ORIGINAL
plan UID assigned at treatment time. So ReferencedRTPlanSequence often points
at a UID that no stored plan carries (observed: record references
1.2.840.113854... but the stored plan's UID is 1.2.752.243...).

Matching strategy (in order):
  1. Referenced UID: ReferencedRTPlanSequence -> ReferencedSOPInstanceUID
     == Plan.rtplan_uid.  (Works only when UIDs happen to align.)
  2. PatientID + beam-name match: find the patient by the record's PatientID,
     then among that patient's plans keep those whose beam names match the
     record's (normalizing 'LA:TX' -> 'LA'), and pick the MOST RECENT.
  3. Raise ValueError if nothing matches.
"""
from __future__ import annotations

import logging
from typing import Optional

import pydicom
from sqlalchemy.orm import Session

from models.patient import Patient
from models.plan import Plan

logger = logging.getLogger(__name__)


def _normalize_beam_name(name: str) -> str:
    """'LA:TX' -> 'LA'; strips the delivery suffix so record and plan match."""
    return str(name).split(":")[0].strip().upper()


def _record_beam_names(record_dcm: pydicom.Dataset) -> set[str]:
    seq = getattr(record_dcm, "TreatmentSessionIonBeamSequence", None) or []
    return {_normalize_beam_name(b.BeamName) for b in seq if hasattr(b, "BeamName")}


def _plan_beam_names(plan_dcm: pydicom.Dataset) -> set[str]:
    seq = getattr(plan_dcm, "IonBeamSequence", None) or []
    return {_normalize_beam_name(b.BeamName) for b in seq if hasattr(b, "BeamName")}


def _find_plan_dicom(store_path: str) -> Optional[pydicom.Dataset]:
    """Read the RTPLAN dataset (header only) from a plan's store dir."""
    from pathlib import Path
    for p in Path(store_path).glob("*.dcm"):
        try:
            dcm = pydicom.dcmread(str(p), stop_before_pixels=True, force=True)
        except Exception:
            continue
        if str(getattr(dcm, "Modality", "")).upper() == "RTPLAN":
            return dcm
    return None


def identify_plan_from_rtrecord(dcm: pydicom.Dataset, db: Session) -> int:
    """Return the plan_id an RT Ion Record belongs to.

    Tries referenced-UID match first, then falls back to PatientID + beam-name
    match (most recent plan). Raises ValueError if no plan can be matched.
    """
    # --- Strategy 1: referenced plan UID ---
    ref_uid = None
    ref_seq = getattr(dcm, "ReferencedRTPlanSequence", None)
    if ref_seq:
        try:
            ref_uid = str(ref_seq[0].ReferencedSOPInstanceUID)
        except (IndexError, AttributeError):
            ref_uid = None
    if ref_uid:
        plan = db.query(Plan).filter_by(rtplan_uid=ref_uid).first()
        if plan:
            logger.info(f"Record matched to plan {plan.id} via referenced UID.")
            return plan.id
        logger.info(
            f"Referenced plan UID {ref_uid} not found among stored plans; "
            f"falling back to PatientID + beam match."
        )

    # --- Strategy 2: PatientID + beam-name match, most recent ---
    patient_id_str = str(getattr(dcm, "PatientID", "") or "")
    if not patient_id_str:
        raise ValueError("RT Record has no PatientID; cannot match to a plan.")

    patient = db.query(Patient).filter_by(patient_id=patient_id_str).first()
    if patient is None:
        raise ValueError(
            f"No patient with PatientID={patient_id_str} found for this record."
        )

    plans = (
        db.query(Plan)
        .filter_by(patient_id=patient.id)
        .order_by(Plan.created_at.desc())
        .all()
    )
    if not plans:
        raise ValueError(
            f"Patient {patient_id_str} has no stored plans to match the record."
        )

    record_beams = _record_beam_names(dcm)

    # Prefer the most recent plan whose beam names match the record's.
    best_no_beam_info = None
    for plan in plans:  # already newest-first
        plan_dcm = _find_plan_dicom(plan.dicom_store_path)
        if plan_dcm is None:
            continue
        plan_beams = _plan_beam_names(plan_dcm)
        if not plan_beams:
            if best_no_beam_info is None:
                best_no_beam_info = plan.id
            continue
        if record_beams and record_beams.issubset(plan_beams):
            logger.info(
                f"Record matched to plan {plan.id} via PatientID + beam match "
                f"(patient {patient_id_str}, beams {sorted(record_beams)})."
            )
            return plan.id

    # If no beam-name match but the patient has exactly one plan, use it.
    if len(plans) == 1:
        logger.info(
            f"Record matched to plan {plans[0].id} via PatientID (single plan; "
            f"beam names did not overlap — verify beam naming)."
        )
        return plans[0].id

    if best_no_beam_info is not None:
        logger.warning(
            f"Record matched to plan {best_no_beam_info} for patient "
            f"{patient_id_str} without beam confirmation (plan beams unreadable)."
        )
        return best_no_beam_info

    raise ValueError(
        f"Patient {patient_id_str} has {len(plans)} plans but none match the "
        f"record's beams {sorted(record_beams)}. Cannot disambiguate."
    )


def record_delivery_type(dcm: pydicom.Dataset) -> str:
    """Classifies an RT Ion Record as 'verification' or 'curative'.

    Verification records are dry runs / machine QA deliveries executed WITHOUT
    the patient on the table, analyzed to check machine delivery logs and
    identify plan delivery problems prior to treatment.
    Curative records represent actual clinical fractions delivered to the patient.
    """
    # 1. Primary: TreatmentStatusComment (3008, 0202)
    comment = str(dcm.get((0x3008, 0x0202), "") or "").strip().upper()
    if "VERIF" in comment:
        return "verification"
    if "CURAT" in comment or "TREAT" in comment:
        return "curative"

    # 2. Secondary: TreatmentDeliveryType (3008, 0024)
    deliv_type = str(dcm.get((0x3008, 0x0024), "") or "").strip().upper()
    if "VERIF" in deliv_type:
        return "verification"
    if "TREAT" in deliv_type or "CURAT" in deliv_type:
        return "curative"

    # 3. Beam-level TreatmentDeliveryType in TreatmentSessionBeamSequence
    seq = (getattr(dcm, "TreatmentSessionIonBeamSequence", None)
           or getattr(dcm, "TreatmentSessionBeamSequence", None) or [])
    for item in seq:
        b_deliv = str(getattr(item, "TreatmentDeliveryType", "") or "").strip().upper()
        if "VERIF" in b_deliv:
            return "verification"

    # 4. Description checks (SeriesDescription, StudyDescription)
    desc = (str(getattr(dcm, "SeriesDescription", "") or "") + " " +
            str(getattr(dcm, "StudyDescription", "") or "")).upper()
    if "VERIF" in desc or "DRY RUN" in desc or "DRY_RUN" in desc or "QA" in desc:
        return "verification"

    return "curative"


def _coerce_fraction_int(val, allow_zero: bool = False) -> Optional[int]:
    """Coerce a DICOM IS/str fraction value to an int, else None.

    If allow_zero is False, values < 1 are rejected so 0 placeholders
    don't masquerade as clinical fractions.
    """
    if val is None:
        return None
    try:
        n = int(val)
    except (TypeError, ValueError):
        return None
    min_val = 0 if allow_zero else 1
    return n if n >= min_val else None


def record_fraction_number(dcm: pydicom.Dataset) -> Optional[int]:
    """Delivered fraction number from an RT Ion Record.

    Verification runs (no patient on table) return 0 to cleanly distinguish
    them from curative treatment fractions 1..N.
    """
    if record_delivery_type(dcm) == "verification":
        return 0

    # Preferred: nested per-beam CurrentFractionNumber (3008,0022).
    seq = (getattr(dcm, "TreatmentSessionIonBeamSequence", None)
           or getattr(dcm, "TreatmentSessionBeamSequence", None) or [])
    for item in seq:
        n = _coerce_fraction_int(getattr(item, "CurrentFractionNumber", None))
        if n is not None:
            return n

    # Secondary: top-level attrs (some vendors put it here).
    for attr in ("CurrentFractionNumber", "FractionNumber"):
        n = _coerce_fraction_int(getattr(dcm, attr, None))
        if n is not None:
            return n

    return None