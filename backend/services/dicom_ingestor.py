"""
Core DICOM ingestion service.

Scans a directory of .dcm files, classifies by modality, extracts patient
and plan information, persists to the database, and archives files to the
organised dicom_store.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pydicom
from sqlalchemy.orm import Session

from config import settings
from dicom.rtdose_parser import clean_store_duplicates
from models.fraction import Fraction
from models.patient import Patient
from models.plan import Plan
from services.record_matcher import (
    _find_plan_dicom,
    _record_beam_names,
    identify_plan_from_rtrecord,
    record_fraction_number,
    record_delivery_type,
)
from services.interruption_detector import detect_record_interruption

logger = logging.getLogger(__name__)

RTRECORD_SOP_CLASSES = {
    "1.2.840.10008.5.1.4.1.1.481.4",  # RT Beams Treatment Record Storage
    "1.2.840.10008.5.1.4.1.1.481.6",  # RT Brachy Treatment Record Storage
    "1.2.840.10008.5.1.4.1.1.481.7",  # RT Treatment Summary Record Storage
    "1.2.840.10008.5.1.4.1.1.481.9",  # RT Ion Beams Treatment Record Storage (Proton/Ion)
    "1.2.840.10008.5.1.4.1.1.481.10", # RT Ion Radiation Record Storage
    "1.2.840.10008.5.1.4.1.1.481.11", # RT Ion Radiation Summary Record Storage
}
RTRECORD_MODALITIES = {"RTRECORD", "RTIBTR", "IONRECORD", "RECORD", "RTR"}


def file_sha256(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def get_rtdose_fingerprint(dcm: pydicom.Dataset, file_bytes: Optional[bytes] = None) -> tuple:
    """
    Returns a fingerprint tuple uniquely identifying RTDose content:
    (dose_summation_type, ref_plan_uids, ref_beam, grid_shape, scaling, pixel_hash)
    """
    sum_type = str(dcm.get("DoseSummationType", "") or "").upper()
    ref_uids = []
    ref_seq = getattr(dcm, "ReferencedRTPlanSequence", None)
    ref_beam = None
    if ref_seq:
        for item in ref_seq:
            uid = getattr(item, "ReferencedSOPInstanceUID", None)
            if uid:
                ref_uids.append(str(uid))
        try:
            rfg = ref_seq[0].ReferencedFractionGroupSequence[0]
            rfb = rfg.ReferencedBeamSequence[0]
            ref_beam = int(rfb.ReferencedBeamNumber)
        except Exception:
            pass

    rows = int(dcm.get("Rows", 0) or 0)
    cols = int(dcm.get("Columns", 0) or 0)
    frames = int(dcm.get("NumberOfFrames", 1) or 1)
    scaling = float(dcm.get("DoseGridScaling", 1.0) or 1.0)

    pixel_hash = ""
    pixel_data = getattr(dcm, "PixelData", None)
    if pixel_data:
        pixel_hash = hashlib.sha256(pixel_data).hexdigest()
    elif file_bytes:
        pixel_hash = hashlib.sha256(file_bytes).hexdigest()

    return (sum_type, tuple(sorted(ref_uids)), ref_beam, (frames, rows, cols), scaling, pixel_hash)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_dicom_files(directory: str) -> dict[str, list[str]]:
    """
    Returns dict keyed by Modality string, values are lists of file paths.
    Handles RTPLAN, RTDOSE, RTSTRUCT, RTIBTR/RTRECORD (RT Treatment Records).
    Deduplicates identical files within the directory (by SHA256, SOPInstanceUID, or RTDose fingerprint).
    Automatically unpacks any .zip archives present in the directory.
    """
    import zipfile
    dir_path = Path(directory)

    # Automatically unpack any zip archives found in the directory
    for zpath in list(dir_path.rglob("*")):
        if zpath.is_file() and zpath.name.lower().endswith(".zip"):
            extract_dir = zpath.parent / f"_extracted_{zpath.stem}"
            if not extract_dir.exists():
                try:
                    extract_dir.mkdir(exist_ok=True)
                    with zipfile.ZipFile(zpath, "r") as zf:
                        zf.extractall(str(extract_dir))
                except Exception as ze:
                    logger.warning(f"Could not extract zip archive {zpath.name}: {ze}")

    result: dict[str, list[str]] = {}
    candidates = set(dir_path.rglob("*.dcm")) | set(dir_path.rglob("*.DCM"))
    for p in dir_path.rglob("*"):
        if p.is_file() and p not in candidates and not p.name.startswith(".") and not p.name.lower().endswith(".zip"):
            candidates.add(p)

    seen_hashes: dict[str, str] = {}
    seen_sops: dict[str, str] = {}
    seen_dose_fps: dict[tuple, str] = {}

    for path in sorted(candidates):
        try:
            content = path.read_bytes()
            h = hashlib.sha256(content).hexdigest()
            if h in seen_hashes:
                logger.info(f"Skipping duplicate DICOM file {path.name} in upload (identical hash to {seen_hashes[h]})")
                continue
            seen_hashes[h] = path.name

            dcm = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
            sop = str(getattr(dcm, "SOPInstanceUID", "") or "")
            if sop:
                if sop in seen_sops:
                    logger.info(f"Skipping duplicate DICOM file {path.name} in upload (identical SOPInstanceUID to {seen_sops[sop]})")
                    continue
                seen_sops[sop] = path.name

            modality = str(dcm.get("Modality", "UNKNOWN")).upper()
            sop_class = str(dcm.get("SOPClassUID", ""))
            if modality in RTRECORD_MODALITIES or sop_class in RTRECORD_SOP_CLASSES:
                modality = "RTRECORD"
            elif modality == "RTDOSE":
                full_dcm = pydicom.dcmread(str(path), force=True)
                fp = get_rtdose_fingerprint(full_dcm, file_bytes=content)
                if fp in seen_dose_fps:
                    logger.info(f"Skipping duplicate RTDOSE {path.name} in upload (identical dose content to {seen_dose_fps[fp]})")
                    continue
                seen_dose_fps[fp] = path.name

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


def extract_plan_metadata(rtplan_dcm: pydicom.Dataset) -> dict:
    """
    Extracts metadata from an RTPLAN or RTIBTR dataset.
    """
    p_fields = parse_rtplan_fields(rtplan_dcm)
    p_label = str(rtplan_dcm.get("RTPlanLabel", "") or "")
    p_name = str(rtplan_dcm.get("RTPlanName", "") or p_label)
    p_uid = str(rtplan_dcm.get("SOPInstanceUID", ""))
    treatment_site = str(
        rtplan_dcm.get("RTPlanDescription", "")
        or rtplan_dcm.get("TargetPrescriptionDose", "")
        or ""
    ) or None

    n_frac = None
    frac_seq = getattr(rtplan_dcm, "FractionGroupSequence", None)
    if frac_seq:
        try:
            n_frac = int(frac_seq[0].NumberOfFractionsPlanned)
        except (IndexError, AttributeError, TypeError, ValueError):
            pass

    ref_struct_uid = None
    ref_ss = getattr(rtplan_dcm, "ReferencedStructureSetSequence", None)
    if ref_ss and len(ref_ss) > 0:
        try:
            ref_struct_uid = str(ref_ss[0].ReferencedSOPInstanceUID)
        except Exception:
            pass

    return {
        "fields": p_fields,
        "plan_label": p_label or "UNNAMED_PLAN",
        "plan_name": p_name or p_label or "Unnamed",
        "rtplan_uid": p_uid,
        "treatment_site": treatment_site,
        "number_of_fields": len(p_fields),
        "number_of_fractions": n_frac,
        "ref_struct_uid": ref_struct_uid,
    }


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

    has_plan = bool(classified.get("RTPLAN"))
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
    Filters out foreign RTPLAN or RTDOSE files that belong to a different plan UID
    to prevent cross-plan contamination when multi-plan/adaptive datasets are ingested.
    Returns the destination path.
    """
    dest = Path(settings.DICOM_STORE_PATH) / patient_id / plan_uid
    dest.mkdir(parents=True, exist_ok=True)
    for f in Path(source_folder).rglob("*"):
        if f.is_file():
            # Check if this file is a foreign plan or dose before archiving
            try:
                dcm = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
                mod = str(dcm.get("Modality", "")).upper()
                sop = str(getattr(dcm, "SOPInstanceUID", ""))

                if mod == "RTPLAN" and sop and sop != plan_uid:
                    logger.warning(
                        f"Skipping archive of foreign RTPLAN {f.name} (UID {sop}); target plan is {plan_uid}"
                    )
                    continue

                if mod == "RTDOSE":
                    ref_seq = getattr(dcm, "ReferencedRTPlanSequence", None)
                    if ref_seq and len(ref_seq) > 0:
                        ref_plan_uid = str(ref_seq[0].ReferencedSOPInstanceUID)
                        if ref_plan_uid and ref_plan_uid != plan_uid:
                            logger.warning(
                                f"Skipping archive of foreign RTDOSE {f.name} referencing {ref_plan_uid}; target plan is {plan_uid}"
                            )
                            continue
            except Exception:
                pass

            target = dest / f.name
            if f.resolve() != target.resolve():
                shutil.move(str(f), target)
    return str(dest)


def ingest_rtrecord_files(
    record_paths: list[str],
    db: Session,
    target_plan_id: Optional[int] = None,
) -> dict:
    """
    Ingests standalone RTRecord file(s), associates them with the correct patient
    and plan, and records/tracks them in the fractions table.
    If no plan exists yet for the patient, creates a provisional pending_plan so
    records can be stored and later reconciled when the RTPlan is uploaded.
    """
    if not record_paths:
        raise ValueError("No RTRecord files provided for ingestion.")

    matched_plans: dict[int, Plan] = {}
    linked_fractions: list[str] = []
    updated_fractions: list[tuple[int, int]] = []
    last_patient_info: dict = {}

    for rec_path in record_paths:
        dcm = pydicom.dcmread(rec_path, stop_before_pixels=True, force=True)
        pinfo = extract_patient_info(dcm)
        last_patient_info = pinfo
        patient_id_str = pinfo["patient_id"]

        # 1. Match to plan
        plan: Optional[Plan] = None
        if target_plan_id is not None:
            plan = db.query(Plan).filter_by(id=target_plan_id).first()

        if plan is None:
            try:
                matched_id = identify_plan_from_rtrecord(dcm, db, target_plan_id=None)
                plan = db.query(Plan).filter_by(id=matched_id).first()
            except ValueError:
                pass

        # 2. Match or create Patient
        patient: Optional[Patient] = None
        if plan is not None:
            patient = db.query(Patient).filter_by(id=plan.patient_id).first()

        if patient is None and patient_id_str and patient_id_str != "UNKNOWN":
            patient = db.query(Patient).filter_by(patient_id=patient_id_str).first()

        if patient is None:
            patient = Patient(
                patient_id=patient_id_str or f"PT_{uuid.uuid4().hex[:8]}",
                patient_name=pinfo["patient_name"],
                date_of_birth=pinfo["date_of_birth"],
                sex=pinfo["sex"],
            )
            db.add(patient)
            db.flush()

        # 3. If plan not yet resolved, search patient's plans or create provisional plan
        if plan is None:
            patient_plans = (
                db.query(Plan)
                .filter_by(patient_id=patient.id)
                .order_by(Plan.created_at.desc())
                .all()
            )
            if patient_plans:
                plan = patient_plans[0]
            else:
                ref_seq = getattr(dcm, "ReferencedRTPlanSequence", None)
                ref_uid = None
                if ref_seq and len(ref_seq) > 0:
                    try:
                        ref_uid = str(ref_seq[0].ReferencedSOPInstanceUID)
                    except Exception:
                        pass
                if not ref_uid:
                    ref_uid = f"provisional.{uuid.uuid4().hex[:16]}"

                plan_label = str(
                    dcm.get("RTPlanLabel", "")
                    or getattr(dcm, "SeriesDescription", "")
                    or f"Plan_{patient.patient_id}"
                )
                dest_dir = Path(settings.DICOM_STORE_PATH) / patient.patient_id / ref_uid
                dest_dir.mkdir(parents=True, exist_ok=True)

                provisional_plan = Plan(
                    patient_id=patient.id,
                    plan_label=plan_label,
                    plan_name=plan_label,
                    number_of_fields=len(_record_beam_names(dcm)) or 1,
                    number_of_fractions=None,
                    dicom_store_path=str(dest_dir),
                    rtplan_uid=ref_uid,
                    qa_status="pending_plan",
                )
                db.add(provisional_plan)
                db.commit()
                db.refresh(provisional_plan)
                plan = provisional_plan

        plan_id = plan.id
        matched_plans[plan.id] = plan

        deliv_type = record_delivery_type(dcm)
        fx_num = 0 if deliv_type == "verification" else (record_fraction_number(dcm) or 1)
        sop_uid = str(dcm.get("SOPInstanceUID", "") or "")
        uid_suffix = sop_uid.replace(".", "_")[-12:] if sop_uid else "rec"
        dest_filename = (
            f"RTRecord_verification_{uid_suffix}.dcm"
            if deliv_type == "verification"
            else f"RTRecord_fx{fx_num}_{uid_suffix}.dcm"
        )
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
        plan_dcm = _find_plan_dicom(plan.dicom_store_path, plan_uid=plan.rtplan_uid)
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

        updated_fractions.append((plan.id, fx_num))
        type_label = "Verification Run (Dry Run)" if deliv_type == "verification" else f"Fraction {fx_num} (Curative)"
        if frac.is_interrupted:
            linked_fractions.append(f"{type_label} [INTERRUPTED: {frac.interruption_reason}]")
        else:
            linked_fractions.append(f"{type_label} (UID: {frac.rtrecord_uid})")

    primary_plan = list(matched_plans.values())[0]
    plan_dcm = _find_plan_dicom(primary_plan.dicom_store_path, plan_uid=primary_plan.rtplan_uid)
    fields = parse_rtplan_fields(plan_dcm) if plan_dcm else []
    patient = db.query(Patient).filter_by(id=primary_plan.patient_id).first()

    warns = [
        f"Ingested and tracked RTRecord for {', '.join(linked_fractions)} under Plan '{primary_plan.plan_label}'"
    ]
    if primary_plan.qa_status == "pending_plan":
        warns.append(
            f"Provisional plan created for patient {patient.patient_id if patient else 'UNKNOWN'}. "
            "Awaiting accompanying RTPlan to run dose calculation and QA simulation."
        )

    return {
        "plan_id": primary_plan.id,
        "plan_ids": list(matched_plans.keys()),
        "patient_id": patient.patient_id if patient else last_patient_info.get("patient_id", "UNKNOWN"),
        "patient_name": patient.patient_name if patient else last_patient_info.get("patient_name", ""),
        "plan_label": primary_plan.plan_label,
        "latest_plan_label": primary_plan.plan_label,
        "plan_name": primary_plan.plan_name,
        "number_of_fields": primary_plan.number_of_fields,
        "number_of_fractions": primary_plan.number_of_fractions,
        "fields": fields,
        "warnings": warns,
        "dicom_files_found": {"RTRECORD": len(record_paths)},
        "is_record_only": True,
        "updated_fractions": updated_fractions,
        "is_all_duplicates": False,
        "new_files_count": len(record_paths),
    }


def ingest_standalone_rtdose_files(
    dose_paths: list[str],
    classified: dict[str, list[str]],
    db: Session,
) -> dict:
    """
    Ingests standalone RTDOSE file(s) into an existing matching plan.
    Detects and ignores duplicate dose files if already present in the plan store.
    """
    if not dose_paths:
        raise ValueError("No RTDOSE files provided for ingestion.")

    matched_plan: Optional[Plan] = None
    warnings: list[str] = []
    new_doses = 0
    duplicate_doses = 0

    for dp in dose_paths:
        dcm = pydicom.dcmread(dp, force=True)
        pinfo = extract_patient_info(dcm)
        patient_id_str = pinfo["patient_id"]
        patient = db.query(Patient).filter_by(patient_id=patient_id_str).first()
        if not patient:
            raise ValueError(
                f"No patient with ID '{patient_id_str}' found for uploaded RTDOSE. "
                "Please upload the RTPlan first."
            )

        # Match plan by ReferencedRTPlanSequence or patient's plans
        ref_seq = getattr(dcm, "ReferencedRTPlanSequence", None)
        ref_uid = str(ref_seq[0].ReferencedSOPInstanceUID) if ref_seq and len(ref_seq) > 0 else None
        target_plan: Optional[Plan] = None
        if ref_uid:
            target_plan = db.query(Plan).filter_by(rtplan_uid=ref_uid).first()
        if not target_plan:
            patient_plans = (
                db.query(Plan)
                .filter_by(patient_id=patient.id)
                .order_by(Plan.created_at.desc())
                .all()
            )
            if patient_plans:
                target_plan = patient_plans[0]

        if not target_plan:
            raise ValueError(
                f"No plan found for patient '{patient_id_str}' to associate RTDOSE {Path(dp).name}."
            )
        matched_plan = target_plan

        dest_dir = Path(target_plan.dicom_store_path)
        dest_dir.mkdir(parents=True, exist_ok=True)
        clean_store_duplicates(str(dest_dir))

        # Check if this RTDOSE is a duplicate of a file already in the store
        d_src = Path(dp)
        src_bytes = d_src.read_bytes()
        src_hash = hashlib.sha256(src_bytes).hexdigest()
        src_sop = str(getattr(dcm, "SOPInstanceUID", "") or "")
        src_fp = get_rtdose_fingerprint(dcm, file_bytes=src_bytes)

        is_dup = False
        dup_reason = ""
        for ef in dest_dir.glob("*.dcm"):
            try:
                ef_bytes = ef.read_bytes()
                if hashlib.sha256(ef_bytes).hexdigest() == src_hash:
                    is_dup = True
                    dup_reason = "identical file content (SHA256)"
                    break
                edcm = pydicom.dcmread(str(ef), stop_before_pixels=True, force=True)
                esop = str(getattr(edcm, "SOPInstanceUID", "") or "")
                if esop and src_sop and esop == src_sop:
                    is_dup = True
                    dup_reason = f"matching SOPInstanceUID {src_sop}"
                    break
                if str(getattr(edcm, "Modality", "")).upper() == "RTDOSE":
                    efp = get_rtdose_fingerprint(edcm, file_bytes=ef_bytes)
                    if efp == src_fp:
                        is_dup = True
                        dup_reason = "identical dose parameters and pixel data"
                        break
            except Exception:
                pass

        if is_dup:
            duplicate_doses += 1
            logger.info(f"Ignoring duplicate RTDOSE {d_src.name}: {dup_reason}")
            warnings.append(f"Ignored duplicate RTDOSE file '{d_src.name}' ({dup_reason})")
            continue

        # Non-duplicate: copy to store
        new_doses += 1
        d_target = dest_dir / d_src.name
        if d_src.resolve() != d_target.resolve():
            shutil.copy2(str(d_src), str(d_target))

        sum_type = str(dcm.get("DoseSummationType", "") or "").upper()
        if sum_type == "PLAN" or not target_plan.rtdose_uid:
            target_plan.rtdose_uid = src_sop
            db.commit()

    if matched_plan is None:
        raise ValueError("Could not match uploaded RTDOSE files to any plan.")

    is_all_duplicates = (new_doses == 0 and duplicate_doses > 0)
    if is_all_duplicates:
        warnings.append("All uploaded RTDOSE files were identical duplicates of files already in the store. Skipped duplicate dose calculation.")

    plan_dcm = _find_plan_dicom(matched_plan.dicom_store_path, plan_uid=matched_plan.rtplan_uid)
    fields = parse_rtplan_fields(plan_dcm) if plan_dcm else []
    patient = db.query(Patient).filter_by(id=matched_plan.patient_id).first()

    return {
        "plan_id": matched_plan.id,
        "plan_ids": [matched_plan.id],
        "patient_id": patient.patient_id if patient else "UNKNOWN",
        "patient_name": patient.patient_name if patient else "",
        "plan_label": matched_plan.plan_label,
        "latest_plan_label": matched_plan.plan_label,
        "plan_name": matched_plan.plan_name,
        "number_of_fields": matched_plan.number_of_fields,
        "number_of_fractions": matched_plan.number_of_fractions,
        "fields": fields,
        "warnings": warnings,
        "dicom_files_found": {"RTDOSE": len(dose_paths)},
        "is_all_duplicates": is_all_duplicates,
        "new_files_count": new_doses,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def ingest_dicom_directory(upload_path: str, db: Session, target_plan_uid: Optional[str] = None) -> dict:
    """
    Main entry point. Scans directory, classifies files, upserts Patient + Plan
    DB records, archives files, returns ingestion summary dict.
    Supports single plans, multiple plans (e.g. multiple beamsets from RayStation),
    individual and multi-plan doses, standalone RTDOSE, and standalone or companion RTRecords.
    Deduplicates identical RTDOSE/DICOM files and ignores duplicates if re-uploaded.
    """
    classified = classify_dicom_files(upload_path)
    if not classified:
        raise ValueError(f"No readable DICOM files found in: {upload_path}")

    # Gather all RTPlan/RTIonPlan files
    plan_paths = classified.get("RTPLAN", []) or classified.get("RTIBTR", [])
    record_paths = classified.get("RTRECORD", [])
    dose_paths = classified.get("RTDOSE", [])

    if not plan_paths:
        if record_paths:
            return ingest_rtrecord_files(record_paths, db)
        if dose_paths:
            return ingest_standalone_rtdose_files(dose_paths, classified, db)
        raise ValueError("No RTPlan, RTIonPlan, RTRecord, or RTDose file found — cannot ingest.")

    # 1. Parse metadata for all RTPLANs
    plans_meta = []
    for p_path in plan_paths:
        rtplan_dcm = pydicom.dcmread(p_path, stop_before_pixels=True, force=True)
        p_info = extract_patient_info(rtplan_dcm)
        p_fields = parse_rtplan_fields(rtplan_dcm)
        p_label = str(rtplan_dcm.get("RTPlanLabel", "") or "")
        p_name = str(rtplan_dcm.get("RTPlanName", "") or p_label)
        p_uid = str(rtplan_dcm.get("SOPInstanceUID", ""))

        n_frac = None
        frac_seq = getattr(rtplan_dcm, "FractionGroupSequence", None)
        if frac_seq:
            try:
                n_frac = int(frac_seq[0].NumberOfFractionsPlanned)
            except (IndexError, AttributeError, TypeError, ValueError):
                pass

        ref_struct_uid = None
        ref_ss = getattr(rtplan_dcm, "ReferencedStructureSetSequence", None)
        if ref_ss and len(ref_ss) > 0:
            try:
                ref_struct_uid = str(ref_ss[0].ReferencedSOPInstanceUID)
            except Exception:
                pass

        plans_meta.append({
            "path": p_path,
            "dcm": rtplan_dcm,
            "patient_info": p_info,
            "fields": p_fields,
            "plan_label": p_label,
            "plan_name": p_name,
            "plan_uid": p_uid,
            "n_fractions": n_frac,
            "ref_struct_uid": ref_struct_uid,
        })

    # If target_plan_uid is specified, prioritize it
    if target_plan_uid:
        matching = [pm for pm in plans_meta if pm["plan_uid"] == target_plan_uid]
        if matching:
            plans_meta = matching + [pm for pm in plans_meta if pm["plan_uid"] != target_plan_uid]

    # 2. Map RTDOSE files by ReferencedRTPlanSequence
    plan_to_doses: dict[str, list[str]] = {pm["plan_uid"]: [] for pm in plans_meta}
    multi_plan_doses: list[str] = []
    unassigned_doses: list[str] = []

    for dp in dose_paths:
        try:
            ddcm = pydicom.dcmread(dp, stop_before_pixels=True, force=True)
            summary_type = str(ddcm.get("DoseSummationType", "") or "").upper()
            ref_seq = getattr(ddcm, "ReferencedRTPlanSequence", None)
            ref_uids = []
            if ref_seq:
                for item in ref_seq:
                    u = getattr(item, "ReferencedSOPInstanceUID", None)
                    if u:
                        ref_uids.append(str(u))

            if summary_type == "MULTI_PLAN" or len(ref_uids) > 1:
                multi_plan_doses.append(dp)
            elif len(ref_uids) == 1 and ref_uids[0] in plan_to_doses:
                plan_to_doses[ref_uids[0]].append(dp)
            elif not ref_uids and len(plans_meta) == 1:
                # If only one plan in upload, unreferenced doses belong to it
                plan_to_doses[plans_meta[0]["plan_uid"]].append(dp)
            else:
                unassigned_doses.append(dp)
        except Exception:
            unassigned_doses.append(dp)

    # 3. Map RTSTRUCT files
    struct_paths = classified.get("RTSTRUCT", [])
    plan_to_structs: dict[str, list[str]] = {pm["plan_uid"]: [] for pm in plans_meta}
    for sp in struct_paths:
        try:
            sdcm = pydicom.dcmread(sp, stop_before_pixels=True, force=True)
            suid = str(sdcm.get("SOPInstanceUID", ""))
            matched = False
            for pm in plans_meta:
                if pm["ref_struct_uid"] and pm["ref_struct_uid"] == suid:
                    plan_to_structs[pm["plan_uid"]].append(sp)
                    matched = True
            if not matched:
                for pm in plans_meta:
                    plan_to_structs[pm["plan_uid"]].append(sp)
        except Exception:
            for pm in plans_meta:
                plan_to_structs[pm["plan_uid"]].append(sp)

    # 4. CT files (planning CT is shared across plans for this patient)
    ct_paths = classified.get("CT", [])

    # 5. Map RTRECORD files
    plan_to_records: dict[str, list[str]] = {pm["plan_uid"]: [] for pm in plans_meta}
    for rp in record_paths:
        try:
            rdcm = pydicom.dcmread(rp, stop_before_pixels=True, force=True)
            ref_seq = getattr(rdcm, "ReferencedRTPlanSequence", None)
            matched = False
            if ref_seq and len(ref_seq) > 0:
                ruid = str(ref_seq[0].ReferencedSOPInstanceUID)
                if ruid in plan_to_records:
                    plan_to_records[ruid].append(rp)
                    matched = True
            if not matched:
                rec_beams = _record_beam_names(rdcm)
                for pm in plans_meta:
                    plan_beams = {b["beam_name"] for b in pm["fields"]}
                    if rec_beams and rec_beams.issubset(plan_beams):
                        plan_to_records[pm["plan_uid"]].append(rp)
                        matched = True
                        break
            if not matched:
                plan_to_records[plans_meta[0]["plan_uid"]].append(rp)
        except Exception:
            pass

    # 6. Archive files into isolated stores per plan with duplicate detection
    warnings_list = validate_dicom_set(classified, plans_meta[0]["dcm"])
    new_files_count = 0
    duplicate_files_count = 0

    for pm in plans_meta:
        patient_id = pm["patient_info"]["patient_id"]
        plan_uid = pm["plan_uid"]
        dest_dir = Path(settings.DICOM_STORE_PATH) / patient_id / plan_uid
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Clean any pre-existing duplicates in store
        cleaned = clean_store_duplicates(str(dest_dir))
        if cleaned:
            warnings_list.append(f"Cleaned up {len(cleaned)} pre-existing duplicate file(s) from plan store.")

        # Index existing files in dest_dir
        existing_hashes: set[str] = set()
        existing_sops: dict[str, Path] = {}
        existing_dose_fps: dict[tuple, Path] = {}
        for ef in dest_dir.glob("*.dcm"):
            try:
                c_bytes = ef.read_bytes()
                existing_hashes.add(hashlib.sha256(c_bytes).hexdigest())
                edcm = pydicom.dcmread(str(ef), stop_before_pixels=True, force=True)
                esop = str(getattr(edcm, "SOPInstanceUID", "") or "")
                if esop:
                    existing_sops[esop] = ef
                emod = str(getattr(edcm, "Modality", "")).upper()
                if emod == "RTDOSE":
                    fp = get_rtdose_fingerprint(edcm, file_bytes=c_bytes)
                    existing_dose_fps[fp] = ef
            except Exception:
                pass

        # Copy the plan's own RTPLAN (skip if identical already present)
        plan_src = Path(pm["path"])
        plan_bytes = plan_src.read_bytes()
        plan_hash = hashlib.sha256(plan_bytes).hexdigest()
        plan_sop = pm["plan_uid"]
        if plan_hash in existing_hashes or (plan_sop and plan_sop in existing_sops):
            duplicate_files_count += 1
        else:
            plan_target = dest_dir / plan_src.name
            if plan_src.resolve() != plan_target.resolve():
                shutil.copy2(str(plan_src), str(plan_target))
            existing_hashes.add(plan_hash)
            if plan_sop:
                existing_sops[plan_sop] = plan_target
            new_files_count += 1

        # Copy the plan's own RTDOSE files with duplicate detection
        pm_rtdose_uid = None
        for dp in plan_to_doses[plan_uid]:
            d_src = Path(dp)
            d_bytes = d_src.read_bytes()
            d_hash = hashlib.sha256(d_bytes).hexdigest()
            ddcm = pydicom.dcmread(str(d_src), force=True)
            d_sop = str(getattr(ddcm, "SOPInstanceUID", "") or "")
            d_fp = get_rtdose_fingerprint(ddcm, file_bytes=d_bytes)

            is_dup = False
            dup_msg = ""
            if d_hash in existing_hashes:
                is_dup = True
                dup_msg = "identical file content (SHA256)"
            elif d_sop and d_sop in existing_sops:
                is_dup = True
                dup_msg = f"matching SOPInstanceUID {d_sop}"
            elif d_fp in existing_dose_fps:
                is_dup = True
                dup_msg = "identical dose parameters and pixel data"

            if is_dup:
                duplicate_files_count += 1
                logger.info(f"Ignoring duplicate RTDOSE {d_src.name}: {dup_msg}")
                warnings_list.append(f"Ignored duplicate RTDOSE file '{d_src.name}' ({dup_msg})")
                if pm_rtdose_uid is None:
                    pm_rtdose_uid = d_sop
                continue

            # New non-duplicate dose file
            new_files_count += 1
            d_target = dest_dir / d_src.name
            if d_src.resolve() != d_target.resolve():
                shutil.copy2(str(d_src), str(d_target))
            existing_hashes.add(d_hash)
            if d_sop:
                existing_sops[d_sop] = d_target
            existing_dose_fps[d_fp] = d_target

            stype = str(ddcm.get("DoseSummationType", "") or "").upper()
            if stype == "PLAN" and pm_rtdose_uid is None:
                pm_rtdose_uid = d_sop
            elif pm_rtdose_uid is None:
                pm_rtdose_uid = d_sop

        if pm_rtdose_uid is None and existing_sops:
            # Fallback to existing RTDOSE SOPInstanceUID in store
            for s_uid, pth in existing_sops.items():
                if "dose" in pth.name.lower() or "rtdose" in pth.name.lower():
                    pm_rtdose_uid = s_uid
                    break

        # Multi-plan doses are saved with a distinct prefix
        for mp in multi_plan_doses:
            mp_src = Path(mp)
            mp_hash = file_sha256(mp_src)
            if mp_hash not in existing_hashes:
                mp_target = dest_dir / f"MULTI_PLAN_{mp_src.name}"
                if mp_src.resolve() != mp_target.resolve():
                    shutil.copy2(str(mp_src), str(mp_target))
                existing_hashes.add(mp_hash)

        # Copy RTSTRUCT files
        pm_rtstruct_uid = None
        for sp in plan_to_structs[plan_uid]:
            s_src = Path(sp)
            s_hash = file_sha256(s_src)
            if s_hash not in existing_hashes:
                s_target = dest_dir / s_src.name
                if s_src.resolve() != s_target.resolve():
                    shutil.copy2(str(s_src), str(s_target))
                existing_hashes.add(s_hash)
            if pm_rtstruct_uid is None:
                try:
                    sdcm = pydicom.dcmread(str(s_src), stop_before_pixels=True, force=True)
                    pm_rtstruct_uid = str(sdcm.get("SOPInstanceUID", "") or "")
                except Exception:
                    pass

        # Copy CT slices
        for cp in ct_paths:
            c_src = Path(cp)
            c_hash = file_sha256(c_src)
            if c_hash not in existing_hashes:
                c_target = dest_dir / c_src.name
                if c_src.resolve() != c_target.resolve():
                    shutil.copy2(str(c_src), str(c_target))
                existing_hashes.add(c_hash)

        pm["dest_path"] = str(dest_dir)
        pm["rtdose_uid"] = pm_rtdose_uid
        pm["rtstruct_uid"] = pm_rtstruct_uid

    # 7. Clean up watch folder if ingest was from watch folder
    try:
        watch_folder = Path(settings.DICOM_WATCH_FOLDER).resolve() if settings.DICOM_WATCH_FOLDER else None
        source_dir = Path(upload_path).resolve()
        if watch_folder and (source_dir == watch_folder or str(source_dir).lower().startswith(str(watch_folder).lower())):
            for f in source_dir.rglob("*.dcm"):
                try:
                    f.unlink()
                except Exception:
                    pass
    except Exception:
        pass

    # 8. Database persistence for ALL plans
    ingested_plans: list[Plan] = []

    for pm in plans_meta:
        p_info = pm["patient_info"]
        plan_uid = pm["plan_uid"]
        dest_path = pm["dest_path"]

        patient = db.query(Patient).filter_by(patient_id=p_info["patient_id"]).first()
        if patient is None:
            patient = Patient(
                patient_id=p_info["patient_id"],
                patient_name=p_info["patient_name"],
                date_of_birth=p_info["date_of_birth"],
                sex=p_info["sex"],
            )
            db.add(patient)
            db.flush()

        existing_plan = db.query(Plan).filter_by(rtplan_uid=plan_uid).first()
        if not existing_plan:
            # Check if this patient has a provisional plan awaiting an RTPlan
            existing_plan = (
                db.query(Plan)
                .filter_by(patient_id=patient.id, qa_status="pending_plan")
                .first()
            )
        if existing_plan:
            old_store_path = existing_plan.dicom_store_path
            warnings_list.append(f"Plan UID {plan_uid} ({pm['plan_label']}) linked to patient {patient.patient_id}.")
            existing_plan.patient_id = patient.id
            existing_plan.plan_label = pm["plan_label"]
            existing_plan.plan_name = pm["plan_name"]
            existing_plan.number_of_fields = len(pm["fields"])
            existing_plan.number_of_fractions = pm["n_fractions"]
            existing_plan.dicom_store_path = dest_path
            existing_plan.rtplan_uid = plan_uid
            existing_plan.rtdose_uid = pm["rtdose_uid"]
            existing_plan.rtstruct_uid = pm["rtstruct_uid"]
            existing_plan.qa_status = "pending"

            # If old store path was different from dest_path, copy existing files into dest_path
            if old_store_path and Path(old_store_path).resolve() != Path(dest_path).resolve() and Path(old_store_path).exists():
                for old_f in Path(old_store_path).glob("*"):
                    if old_f.is_file() and not old_f.name.startswith("."):
                        new_f = Path(dest_path) / old_f.name
                        if not new_f.exists():
                            shutil.copy2(str(old_f), str(new_f))
                for f in existing_plan.fractions:
                    if f.rtrecord_path and Path(f.rtrecord_path).parent.resolve() == Path(old_store_path).resolve():
                        f.rtrecord_path = str(Path(dest_path) / Path(f.rtrecord_path).name)

            db.commit()
            plan = existing_plan
        else:
            plan = Plan(
                patient_id=patient.id,
                plan_label=pm["plan_label"],
                plan_name=pm["plan_name"],
                number_of_fields=len(pm["fields"]),
                number_of_fractions=pm["n_fractions"],
                dicom_store_path=dest_path,
                rtplan_uid=plan_uid,
                rtdose_uid=pm["rtdose_uid"],
                rtstruct_uid=pm["rtstruct_uid"],
                qa_status="pending",
            )
            db.add(plan)
            db.commit()

        db.refresh(plan)
        ingested_plans.append(plan)

        # Link and track RTRECORD files for this plan
        for rec_path in plan_to_records.get(plan_uid, []):
            try:
                rec_dcm = pydicom.dcmread(rec_path, stop_before_pixels=True, force=True)
                deliv_type = record_delivery_type(rec_dcm)
                fx_num = 0 if deliv_type == "verification" else (record_fraction_number(rec_dcm) or 1)
                sop_uid = str(rec_dcm.get("SOPInstanceUID", "") or "")
                uid_suffix = sop_uid.replace(".", "_")[-12:] if sop_uid else "rec"
                dest_filename = (
                    f"RTRecord_verification_{uid_suffix}.dcm"
                    if deliv_type == "verification"
                    else f"RTRecord_fx{fx_num}_{uid_suffix}.dcm"
                )
                dest_rec = Path(dest_path) / dest_filename
                if Path(rec_path).resolve() != dest_rec.resolve():
                    shutil.copy2(rec_path, dest_rec)

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
                frac.rtrecord_path = str(dest_rec)

                plan_dcm = _find_plan_dicom(dest_path, plan_uid=plan_uid)
                interruption_info = detect_record_interruption(rec_dcm, plan_dcm)
                frac.is_interrupted = interruption_info["is_interrupted"]
                frac.interruption_reason = interruption_info["interruption_reason"]
                if interruption_info["is_interrupted"]:
                    frac.qa_status = "interrupted"
                    warnings_list.append(
                        f"INTERRUPTED RECORD: Fraction {fx_num} ({plan.plan_label}) flagged as partial delivery ({interruption_info['interruption_reason']})"
                    )
                else:
                    frac.qa_status = "pending"
                db.commit()

                if deliv_type == "verification":
                    warnings_list.append(f"Linked Verification Run for {plan.plan_label} (Dry Run, UID: {frac.rtrecord_uid})")
                else:
                    warnings_list.append(f"Linked RTRecord for {plan.plan_label} Fraction {fx_num} (Curative, UID: {frac.rtrecord_uid})")
            except Exception as exc:
                logger.warning(f"Could not link RTRecord {rec_path}: {exc}")

    dicom_files_found = {k: len(v) for k, v in classified.items()}
    primary_plan = ingested_plans[0]
    primary_meta = plans_meta[0]

    plan_ids = [p.id for p in ingested_plans]
    plan_labels = [p.plan_label for p in ingested_plans]
    if len(ingested_plans) > 1:
        label_summary = f"{primary_plan.plan_label} (+{len(ingested_plans) - 1} beamsets/plans: {', '.join(plan_labels[1:])})"
        warnings_list.append(
            f"Ingested {len(ingested_plans)} beamsets/plans for patient {primary_meta['patient_info']['patient_id']}: "
            f"{', '.join(plan_labels)}"
        )
    else:
        label_summary = primary_plan.plan_label

    is_all_duplicates = (new_files_count == 0 and duplicate_files_count > 0)
    if is_all_duplicates:
        warnings_list.append("All uploaded files were identical duplicates of files already in the store. Skipped duplicate dose calculation.")

    return {
        "plan_id": primary_plan.id,
        "plan_ids": plan_ids,
        "patient_id": primary_meta["patient_info"]["patient_id"],
        "patient_name": primary_meta["patient_info"]["patient_name"],
        "plan_label": label_summary,
        "latest_plan_label": label_summary,
        "plan_name": primary_plan.plan_name,
        "number_of_fields": primary_plan.number_of_fields,
        "number_of_fractions": primary_plan.number_of_fractions,
        "fields": primary_meta["fields"],
        "warnings": warnings_list,
        "dicom_files_found": dicom_files_found,
        "is_all_duplicates": is_all_duplicates,
        "new_files_count": new_files_count,
        "plans": [
            {
                "plan_id": p.id,
                "plan_label": p.plan_label,
                "plan_name": p.plan_name,
                "number_of_fields": p.number_of_fields,
                "number_of_fractions": p.number_of_fractions,
                "rtdose_uid": p.rtdose_uid,
            }
            for p in ingested_plans
        ],
    }
