"""
Core DICOM ingestion service.

Scans a directory of .dcm files, classifies by modality, extracts patient
and plan information, persists to the database, and archives files to the
organised dicom_store.
"""
from __future__ import annotations

import logging
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pydicom
from sqlalchemy.orm import Session

from config import settings
from models.fraction import Fraction
from models.patient import Patient
from models.plan import Plan
from services.record_matcher import (
    _find_plan_dicom,
    identify_plan_from_rtrecord,
    record_fraction_number,
    record_delivery_type,
)
from services.interruption_detector import detect_record_interruption

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_dicom_files(directory: str) -> dict[str, list[str]]:
    """
    Returns dict keyed by Modality string, values are lists of file paths.
    Handles RTPLAN, RTDOSE, RTSTRUCT, RTIBTR/RTRECORD (RT Treatment Records).
    """
    result: dict[str, list[str]] = {}
    candidates = set(Path(directory).rglob("*.dcm")) | set(Path(directory).rglob("*.DCM"))
    for p in Path(directory).rglob("*"):
        if p.is_file() and p not in candidates and not p.name.startswith("."):
            candidates.add(p)

    for path in sorted(candidates):
        try:
            dcm = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
            modality = str(dcm.get("Modality", "UNKNOWN")).upper()
            sop_class = str(dcm.get("SOPClassUID", ""))
            # 1.2.840.10008.5.1.4.1.1.481.4 = RT Beams Treatment Record
            # 1.2.840.10008.5.1.4.1.1.481.7 = RT Ion Beams Treatment Record
            if modality in ("RTIBTR", "RTRECORD") or sop_class in (
                "1.2.840.10008.5.1.4.1.1.481.4",
                "1.2.840.10008.5.1.4.1.1.481.7",
            ):
                modality = "RTRECORD"
            result.setdefault(modality, []).append(str(path))
        except Exception:
            pass
    return result


# ---------------------------------------------------------------------------
# Patient info
# ---------------------------------------------------------------------------

def extract_patient_info(dcm: pydicom.Dataset) -> dict:
    """
    Safely extracts patient demographics. Never raises on missing tags.
    """
    raw_dob = str(dcm.get("PatientBirthDate", "") or "")
    dob: Optional[date] = None
    if len(raw_dob) == 8:
        try:
            dob = date(int(raw_dob[:4]), int(raw_dob[4:6]), int(raw_dob[6:8]))
        except ValueError:
            pass

    return {
        "patient_id": str(dcm.get("PatientID", "UNKNOWN") or "UNKNOWN"),
        "patient_name": str(dcm.get("PatientName", "") or ""),
        "date_of_birth": dob,
        "sex": str(dcm.get("PatientSex", "") or "")[:1] or None,
    }


# ---------------------------------------------------------------------------
# Plan / field parsing
# ---------------------------------------------------------------------------

def get_beam_sequence(dcm: pydicom.Dataset):
    """Returns (sequence, beam_type) where beam_type is 'ion' or 'conventional'."""
    if hasattr(dcm, "IonBeamSequence"):
        return dcm.IonBeamSequence, "ion"
    elif hasattr(dcm, "BeamSequence"):
        return dcm.BeamSequence, "conventional"
    raise ValueError("No beam sequence found in RTPLAN")


def parse_rtplan_fields(dcm: pydicom.Dataset) -> list[dict]:
    """
    Iterates IonBeamSequence (RTIonPlan) or BeamSequence (RTPlan).
    Per field: beam name, control points, energy range, spot count, total MU.
    """
    try:
        beam_seq, beam_type = get_beam_sequence(dcm)
    except ValueError:
        return []

    fields = []
    for beam in beam_seq:
        beam_name = str(beam.get("BeamName", "") or beam.get("IonBeamName", "") or "Unknown")
        gantry_angle = 0.0
        energy_min = 0.0
        energy_max = 0.0
        total_spots = 0
        total_mu = 0.0
        n_layers = 0

        cp_seq_attr = "IonControlPointSequence" if beam_type == "ion" else "ControlPointSequence"
        cp_seq = getattr(beam, cp_seq_attr, [])
        energies = []

        for cp in cp_seq:
            # Gantry angle from first control point
            if hasattr(cp, "GantryAngle") and gantry_angle == 0.0:
                try:
                    gantry_angle = float(cp.GantryAngle)
                except (TypeError, ValueError):
                    pass

            # Energy
            energy_attr = "NominalBeamEnergy"
            if hasattr(cp, energy_attr):
                try:
                    energies.append(float(getattr(cp, energy_attr)))
                except (TypeError, ValueError):
                    pass

            # Spot positions → count spots
            spot_map = getattr(cp, "ScanSpotPositionMap", None)
            if spot_map is not None:
                n_layers += 1
                # ScanSpotPositionMap is interleaved x,y pairs
                n_spots = len(spot_map) // 2
                total_spots += n_spots

            # MU per spot
            spot_mu = getattr(cp, "ScanSpotMetersetWeights", None)
            if spot_mu is not None:
                try:
                    total_mu += float(sum(spot_mu)) if hasattr(spot_mu, "__iter__") else float(spot_mu)
                except (TypeError, ValueError):
                    pass

        if energies:
            energy_min = min(energies)
            energy_max = max(energies)

        fields.append({
            "beam_name": beam_name,
            "gantry_angle": gantry_angle,
            "energy_min_mev": energy_min,
            "energy_max_mev": energy_max,
            "number_of_layers": n_layers,
            "total_spots": total_spots,
            "total_mu": round(total_mu, 4),
        })

    return fields


def parse_rtdose_metadata(dcm: pydicom.Dataset) -> dict:
    """
    Extracts RTDose metadata without loading the full pixel array.
    """
    ref_plan_uid = None
    ref_seq = getattr(dcm, "ReferencedRTPlanSequence", None)
    if ref_seq:
        try:
            ref_plan_uid = str(ref_seq[0].ReferencedSOPInstanceUID)
        except (IndexError, AttributeError):
            pass

    rows = int(dcm.get("Rows", 0) or 0)
    cols = int(dcm.get("Columns", 0) or 0)
    n_frames = int(dcm.get("NumberOfFrames", 0) or 0)
    pixel_spacing = list(getattr(dcm, "PixelSpacing", [0, 0]))
    slice_thickness = float(dcm.get("SliceThickness", 0) or 0)

    return {
        "grid_dimensions": (n_frames, rows, cols),
        "voxel_spacing": pixel_spacing + [slice_thickness],
        "dose_units": str(dcm.get("DoseUnits", "") or ""),
        "dose_type": str(dcm.get("DoseType", "") or ""),
        "referenced_plan_uid": ref_plan_uid,
        "dose_summation_type": str(dcm.get("DoseSummationType", "") or ""),
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_dicom_set(classified: dict, rtplan_dcm: Optional[pydicom.Dataset] = None) -> list[str]:
    """
    Returns list of validation warnings. Empty list = all OK.
    """
    warnings_list: list[str] = []

    has_plan = bool(classified.get("RTPLAN") or classified.get("RTIBTR"))
    if not has_plan:
        warnings_list.append("No RTPlan or RTIonPlan file found.")

    has_dose = bool(classified.get("RTDOSE"))
    if not has_dose:
        warnings_list.append("No RTDose file found.")

    if has_plan and has_dose and rtplan_dcm is not None:
        plan_uid = str(rtplan_dcm.get("SOPInstanceUID", ""))
        dose_paths = classified.get("RTDOSE", [])
        matched = False
        for dp in dose_paths:
            try:
                ddcm = pydicom.dcmread(dp, stop_before_pixels=True)
                meta = parse_rtdose_metadata(ddcm)
                if meta["referenced_plan_uid"] == plan_uid:
                    matched = True
                    break
            except Exception:
                pass
        if not matched:
            warnings_list.append("RTDose does not appear to reference the found RTPlan UID.")

    if not classified.get("RTSTRUCT"):
        warnings_list.append("No RTStruct file found (non-critical — ROI context unavailable).")

    return warnings_list


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def archive_ingested_files(source_folder: str, patient_id: str, plan_uid: str) -> str:
    """
    Moves DICOM files from the watch/upload folder into the organised dicom_store.
    Returns the destination path.
    """
    dest = Path(settings.DICOM_STORE_PATH) / patient_id / plan_uid
    dest.mkdir(parents=True, exist_ok=True)
    for f in Path(source_folder).rglob("*"):
        if f.is_file():
            target = dest / f.name
            if f.resolve() != target.resolve():
                shutil.move(str(f), target)
    return str(dest)


def ingest_rtrecord_files(record_paths: list[str], db: Session) -> dict:
    """
    Ingests standalone RTRecord file(s), associates them with the correct patient
    and plan, and records/tracks them in the fractions table.
    """
    if not record_paths:
        raise ValueError("No RTRecord files provided for ingestion.")

    matched_plans: dict[int, Plan] = {}
    linked_fractions: list[str] = []
    last_patient_info: dict = {}

    for rec_path in record_paths:
        dcm = pydicom.dcmread(rec_path, stop_before_pixels=True, force=True)
        pinfo = extract_patient_info(dcm)
        last_patient_info = pinfo

        patient_id_str = pinfo["patient_id"]
        patient = db.query(Patient).filter_by(patient_id=patient_id_str).first()
        if patient is None:
            patient = Patient(
                patient_id=pinfo["patient_id"],
                patient_name=pinfo["patient_name"],
                date_of_birth=pinfo["date_of_birth"],
                sex=pinfo["sex"],
            )
            db.add(patient)
            db.flush()

        # Match to plan
        try:
            plan_id = identify_plan_from_rtrecord(dcm, db)
        except ValueError as exc:
            patient_plans = (
                db.query(Plan)
                .filter_by(patient_id=patient.id)
                .order_by(Plan.created_at.desc())
                .all()
            )
            if len(patient_plans) == 1:
                plan_id = patient_plans[0].id
            elif not patient_plans:
                raise ValueError(
                    f"Patient {patient_id_str} ({pinfo['patient_name']}) was registered, "
                    f"but has no RTPlan uploaded yet. Please upload the RTPlan first."
                )
            else:
                raise exc

        plan = db.query(Plan).filter_by(id=plan_id).first()
        if not plan:
            raise ValueError(f"Plan {plan_id} not found in database.")
        matched_plans[plan.id] = plan

        deliv_type = record_delivery_type(dcm)
        fx_num = 0 if deliv_type == "verification" else (record_fraction_number(dcm) or 1)
        sop_uid = str(dcm.get("SOPInstanceUID", "") or "")
        uid_suffix = sop_uid.replace(".", "_")[-12:] if sop_uid else "rec"
        if deliv_type == "verification":
            dest_filename = f"RTRecord_verification_{uid_suffix}.dcm"
        else:
            dest_filename = f"RTRecord_fx{fx_num}_{uid_suffix}.dcm"
        dest = Path(plan.dicom_store_path) / dest_filename
        if Path(rec_path).resolve() != dest.resolve():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rec_path, dest)

        frac = (
            db.query(Fraction)
            .filter_by(plan_id=plan.id, fraction_number=fx_num)
            .order_by(Fraction.id.desc())
            .first()
        )
        if frac is None:
            frac = Fraction(plan_id=plan.id, fraction_number=fx_num)
            db.add(frac)

        raw_date = str(dcm.get("TreatmentDate", "") or dcm.get("SeriesDate", "") or dcm.get("InstanceCreationDate", "") or "")
        parsed_date = None
        if len(raw_date) == 8 and raw_date.isdigit():
            try:
                parsed_date = datetime.strptime(raw_date, "%Y%m%d").date()
            except Exception:
                pass

        frac.delivery_date = parsed_date
        frac.delivery_type = deliv_type
        frac.rtrecord_uid = sop_uid
        frac.rtrecord_path = str(dest)

        # Check for interrupted / partial delivery
        plan_dcm = _find_plan_dicom(plan.dicom_store_path)
        interruption_info = detect_record_interruption(dcm, plan_dcm)
        frac.is_interrupted = interruption_info["is_interrupted"]
        frac.interruption_reason = interruption_info["interruption_reason"]
        if interruption_info["is_interrupted"]:
            frac.qa_status = "interrupted"
            logger.warning(
                f"RT Record for plan {plan.id} fx {fx_num} flagged as INTERRUPTED: {interruption_info['interruption_reason']}"
            )
        else:
            frac.qa_status = "pending"
        db.commit()

        type_label = "Verification Run (Dry Run)" if deliv_type == "verification" else f"Fraction {fx_num} (Curative)"
        if frac.is_interrupted:
            linked_fractions.append(f"{type_label} [INTERRUPTED: {frac.interruption_reason}]")
        else:
            linked_fractions.append(f"{type_label} (UID: {frac.rtrecord_uid})")

    primary_plan = list(matched_plans.values())[0]
    plan_dcm = _find_plan_dicom(primary_plan.dicom_store_path)
    fields = parse_rtplan_fields(plan_dcm) if plan_dcm else []
    patient = db.query(Patient).filter_by(id=primary_plan.patient_id).first()

    return {
        "plan_id": primary_plan.id,
        "patient_id": patient.patient_id if patient else last_patient_info.get("patient_id", "UNKNOWN"),
        "patient_name": patient.patient_name if patient else last_patient_info.get("patient_name", ""),
        "plan_label": primary_plan.plan_label,
        "plan_name": primary_plan.plan_name,
        "number_of_fields": primary_plan.number_of_fields,
        "number_of_fractions": primary_plan.number_of_fractions,
        "fields": fields,
        "warnings": [
            f"Ingested and tracked RTRecord for {', '.join(linked_fractions)} under Plan '{primary_plan.plan_label}'"
        ],
        "dicom_files_found": {"RTRECORD": len(record_paths)},
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def ingest_dicom_directory(upload_path: str, db: Session) -> dict:
    """
    Main entry point. Scans directory, classifies files, upserts Patient + Plan
    DB records, archives files, returns ingestion summary dict.
    Supports RTPlan/RTIonPlan datasets as well as standalone or companion RTRecords.
    """
    classified = classify_dicom_files(upload_path)
    if not classified:
        raise ValueError(f"No readable DICOM files found in: {upload_path}")

    # Pick the first RTPlan/RTIonPlan file
    plan_paths = classified.get("RTPLAN", []) or classified.get("RTIBTR", [])
    record_paths = classified.get("RTRECORD", [])

    if not plan_paths:
        if record_paths:
            return ingest_rtrecord_files(record_paths, db)
        raise ValueError("No RTPlan or RTIonPlan file found — cannot ingest.")

    rtplan_dcm = pydicom.dcmread(plan_paths[0], stop_before_pixels=True)
    patient_info = extract_patient_info(rtplan_dcm)
    fields = parse_rtplan_fields(rtplan_dcm)
    warnings_list = validate_dicom_set(classified, rtplan_dcm)

    plan_label = str(rtplan_dcm.get("RTPlanLabel", "") or "")
    plan_name = str(rtplan_dcm.get("RTPlanName", "") or plan_label)
    plan_uid = str(rtplan_dcm.get("SOPInstanceUID", ""))
    n_fractions = None
    frac_seq = getattr(rtplan_dcm, "FractionGroupSequence", None)
    if frac_seq:
        try:
            n_fractions = int(frac_seq[0].NumberOfFractionsPlanned)
        except (IndexError, AttributeError, TypeError, ValueError):
            pass

    # RTDose UID
    rtdose_uid = None
    dose_paths = classified.get("RTDOSE", [])
    if dose_paths:
        try:
            ddcm = pydicom.dcmread(dose_paths[0], stop_before_pixels=True)
            rtdose_uid = str(ddcm.get("SOPInstanceUID", "") or "")
        except Exception:
            pass

    # RTStruct UID
    rtstruct_uid = None
    struct_paths = classified.get("RTSTRUCT", [])
    if struct_paths:
        try:
            sdcm = pydicom.dcmread(struct_paths[0], stop_before_pixels=True)
            rtstruct_uid = str(sdcm.get("SOPInstanceUID", "") or "")
        except Exception:
            pass

    # Archive files BEFORE saving to DB so we have a stable path
    dest_path = archive_ingested_files(upload_path, patient_info["patient_id"], plan_uid)

    # Upsert Patient
    patient = db.query(Patient).filter_by(patient_id=patient_info["patient_id"]).first()
    if patient is None:
        patient = Patient(
            patient_id=patient_info["patient_id"],
            patient_name=patient_info["patient_name"],
            date_of_birth=patient_info["date_of_birth"],
            sex=patient_info["sex"],
        )
        db.add(patient)
        db.flush()  # get patient.id without committing

    # Check if plan already exists (re-ingestion)
    existing_plan = db.query(Plan).filter_by(rtplan_uid=plan_uid).first()
    if existing_plan:
        warnings_list.append(f"Plan UID {plan_uid} already exists — updating record.")
        existing_plan.plan_label = plan_label
        existing_plan.plan_name = plan_name
        existing_plan.number_of_fields = len(fields)
        existing_plan.number_of_fractions = n_fractions
        existing_plan.dicom_store_path = dest_path
        existing_plan.rtdose_uid = rtdose_uid
        existing_plan.rtstruct_uid = rtstruct_uid
        db.commit()
        plan = existing_plan
    else:
        plan = Plan(
            patient_id=patient.id,
            plan_label=plan_label,
            plan_name=plan_name,
            number_of_fields=len(fields),
            number_of_fractions=n_fractions,
            dicom_store_path=dest_path,
            rtplan_uid=plan_uid,
            rtdose_uid=rtdose_uid,
            rtstruct_uid=rtstruct_uid,
            qa_status="pending",
        )
        db.add(plan)
        db.commit()

    db.refresh(plan)

    # If RTRECORD files were also in this upload, link and track them
    if record_paths:
        for rec_path in record_paths:
            try:
                rec_dcm = pydicom.dcmread(rec_path, stop_before_pixels=True, force=True)
                deliv_type = record_delivery_type(rec_dcm)
                fx_num = 0 if deliv_type == "verification" else (record_fraction_number(rec_dcm) or 1)
                sop_uid = str(rec_dcm.get("SOPInstanceUID", "") or "")
                uid_suffix = sop_uid.replace(".", "_")[-12:] if sop_uid else "rec"
                if deliv_type == "verification":
                    dest_filename = f"RTRecord_verification_{uid_suffix}.dcm"
                else:
                    dest_filename = f"RTRecord_fx{fx_num}_{uid_suffix}.dcm"
                dest = Path(dest_path) / dest_filename
                if Path(rec_path).resolve() != dest.resolve():
                    shutil.copy2(rec_path, dest)

                frac = (
                    db.query(Fraction)
                    .filter_by(plan_id=plan.id, fraction_number=fx_num)
                    .order_by(Fraction.id.desc())
                    .first()
                )
                if frac is None:
                    frac = Fraction(plan_id=plan.id, fraction_number=fx_num)
                    db.add(frac)

                raw_date = str(rec_dcm.get("TreatmentDate", "") or rec_dcm.get("SeriesDate", "") or rec_dcm.get("InstanceCreationDate", "") or "")
                parsed_date = None
                if len(raw_date) == 8 and raw_date.isdigit():
                    try:
                        parsed_date = datetime.strptime(raw_date, "%Y%m%d").date()
                    except Exception:
                        pass

                frac.delivery_date = parsed_date
                frac.delivery_type = deliv_type
                frac.rtrecord_uid = sop_uid
                frac.rtrecord_path = str(dest)

                # Check for interrupted / partial delivery
                plan_dcm = _find_plan_dicom(dest_path)
                interruption_info = detect_record_interruption(rec_dcm, plan_dcm)
                frac.is_interrupted = interruption_info["is_interrupted"]
                frac.interruption_reason = interruption_info["interruption_reason"]
                if interruption_info["is_interrupted"]:
                    frac.qa_status = "interrupted"
                    warnings_list.append(
                        f"INTERRUPTED RECORD: Fraction {fx_num} flagged as partial delivery ({interruption_info['interruption_reason']})"
                    )
                else:
                    frac.qa_status = "pending"
                db.commit()

                if deliv_type == "verification":
                    warnings_list.append(f"Linked Verification Run (Dry Run, UID: {frac.rtrecord_uid})")
                else:
                    warnings_list.append(f"Linked RTRecord for Fraction {fx_num} (Curative, UID: {frac.rtrecord_uid})")
            except Exception as exc:
                logger.warning(f"Could not link RTRecord {rec_path}: {exc}")

    dicom_files_found = {k: len(v) for k, v in classified.items()}

    return {
        "plan_id": plan.id,
        "patient_id": patient_info["patient_id"],
        "patient_name": patient_info["patient_name"],
        "plan_label": plan_label,
        "plan_name": plan_name,
        "number_of_fields": len(fields),
        "number_of_fractions": n_fractions,
        "fields": fields,
        "warnings": warnings_list,
        "dicom_files_found": dicom_files_found,
    }
