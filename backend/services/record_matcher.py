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
    seq = (
        getattr(record_dcm, "TreatmentSessionIonBeamSequence", None)
        or getattr(record_dcm, "TreatmentSessionBeamSequence", None)
        or []
    )
    return {_normalize_beam_name(b.BeamName) for b in seq if hasattr(b, "BeamName")}


def _plan_beam_names(plan_dcm: pydicom.Dataset) -> set[str]:
    seq = (
        getattr(plan_dcm, "IonBeamSequence", None)
        or getattr(plan_dcm, "BeamSequence", None)
        or []
    )
    return {_normalize_beam_name(b.BeamName) for b in seq if hasattr(b, "BeamName")}


def _find_plan_dicom(store_path: str, plan_uid: Optional[str] = None) -> Optional[pydicom.Dataset]:
    """Read the RTPLAN dataset (header only) from a plan's store dir."""
    from pathlib import Path
    for p in Path(store_path).glob("*.dcm"):
        try:
            dcm = pydicom.dcmread(str(p), stop_before_pixels=True, force=True)
        except Exception:
            continue
        if str(getattr(dcm, "Modality", "")).upper() in ("RTPLAN", "RTIBTR"):
            if plan_uid is None or str(getattr(dcm, "SOPInstanceUID", "")) == plan_uid:
                return dcm
    return None


def identify_plan_from_rtrecord(
    dcm: pydicom.Dataset,
    db: Session,
    target_plan_id: Optional[int] = None,
) -> int:
    """Return the plan_id an RT Record belongs to.

    Tries target_plan_id first, then referenced-UID match, then falls back to
    PatientID + beam-name matching. If beam names cannot disambiguate among a
    patient's multiple plans, gracefully defaults to the most recent plan.
    """
    if target_plan_id is not None:
        target = db.query(Plan).filter_by(id=target_plan_id).first()
        if target:
            logger.info(f"Record explicitly directed to target plan {target.id} ({target.plan_label}).")
            return target.id

    # --- Strategy 1: referenced plan UID ---
    ref_uids: list[str] = []
    ref_seq = getattr(dcm, "ReferencedRTPlanSequence", None)
    if ref_seq:
        for item in ref_seq:
            try:
                u = str(item.ReferencedSOPInstanceUID)
                if u:
                    ref_uids.append(u)
            except (IndexError, AttributeError):
                pass

    for ref_uid in ref_uids:
        plan = db.query(Plan).filter_by(rtplan_uid=ref_uid).first()
        if plan:
            logger.info(f"Record matched to plan {plan.id} via referenced UID {ref_uid}.")
            return plan.id

    # Check store directory DICOMs for matching SOPInstanceUID
    patient_id_str = str(getattr(dcm, "PatientID", "") or "")
    if patient_id_str:
        patient = db.query(Patient).filter_by(patient_id=patient_id_str).first()
        if patient:
            candidate_plans = (
                db.query(Plan)
                .filter_by(patient_id=patient.id)
                .order_by(Plan.created_at.desc())
                .all()
            )
            for cand in candidate_plans:
                cand_dcm = _find_plan_dicom(cand.dicom_store_path, plan_uid=cand.rtplan_uid)
                if cand_dcm:
                    cand_sop = str(getattr(cand_dcm, "SOPInstanceUID", ""))
                    if cand_sop in ref_uids:
                        logger.info(f"Record matched to plan {cand.id} via store SOPInstanceUID.")
                        return cand.id

    # --- Strategy 2: PatientID + beam-name match ---
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

    # Prefer plan whose beam names match the record's (exact or subset)
    scored_plans: list[tuple[float, Plan]] = []
    best_no_beam_info = None

    for plan in plans:
        plan_dcm = _find_plan_dicom(plan.dicom_store_path, plan_uid=plan.rtplan_uid)
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
        if record_beams and plan_beams:
            overlap = len(record_beams & plan_beams)
            if overlap > 0:
                scored_plans.append((overlap / len(record_beams), plan))

    # Highest beam overlap
    if scored_plans:
        scored_plans.sort(key=lambda x: x[0], reverse=True)
        best_score, best_plan = scored_plans[0]
        logger.info(
            f"Record matched to plan {best_plan.id} via beam overlap score {best_score:.2f}."
        )
        return best_plan.id

    # If only 1 plan exists for this patient, use it
    if len(plans) == 1:
        logger.info(
            f"Record matched to single plan {plans[0].id} for patient {patient_id_str}."
        )
        return plans[0].id

    if best_no_beam_info is not None:
        logger.warning(
            f"Record matched to plan {best_no_beam_info} for patient "
            f"{patient_id_str} without beam confirmation (plan beams unreadable)."
        )
        return best_no_beam_info

    # Graceful fallback: default to the most recent plan for the patient
    logger.warning(
        f"Record for patient {patient_id_str} has {len(plans)} plans but none strictly match "
        f"record beams {sorted(record_beams)}; defaulting to most recent plan {plans[0].id} ({plans[0].plan_label})."
    )
    return plans[0].id


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