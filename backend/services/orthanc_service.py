"""
backend/services/orthanc_service.py -- Orthanc PACS / VNA Integration Service.

Provides:
1. Health and connection status verification for the Orthanc DICOM server.
2. Patient search by Patient ID or Patient Name.
3. Discovery and categorization of patient studies and series:
   - Plans (RTPLAN, RTDOSE, RTSTRUCT, Planning CT)
   - RT Treatment Records (RTRECORD, RTIBTR)
   - Offline Image Reviews (Daily CBCT scans and REG spatial registrations)
4. Automated downloading and importing of:
   - Plan packages into Virtual-PSQA (triggering Stage 1 pipeline)
   - RT Treatment records into plan fractions (triggering Stage 2 Delivery QA)
   - Daily CBCT and REG objects into OIR / Synthetic CT for specific fractions
"""
from __future__ import annotations

import io
import logging
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx
import pydicom
from sqlalchemy.orm import Session

from config import settings
from models.fraction import Fraction
from models.patient import Patient
from models.plan import Plan
from models.synthetic_ct import SyntheticCT
from services.dicom_ingestor import ingest_dicom_directory, ingest_rtrecord_files
from services.synthetic_ct_service import ingest_cbct_series

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP Client Helper
# ---------------------------------------------------------------------------

def get_orthanc_client(
    url: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout: Optional[float] = None,
) -> httpx.Client:
    """Create an HTTP client configured for the Orthanc REST API."""
    base_url = (url or settings.ORTHANC_URL or "http://localhost:8042").rstrip("/")
    user = username if username is not None else settings.ORTHANC_USERNAME
    pw = password if password is not None else settings.ORTHANC_PASSWORD

    auth = (user, pw) if user and pw else None
    req_timeout = timeout if timeout is not None else float(getattr(settings, "ORTHANC_TIMEOUT_SECONDS", 120))

    return httpx.Client(
        base_url=base_url,
        auth=auth,
        timeout=req_timeout,
        headers={"Accept": "application/json"},
    )


def check_orthanc_connection(
    url: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> dict[str, Any]:
    """Check if Orthanc is reachable and return system metadata."""
    base_url = (url or settings.ORTHANC_URL or "http://localhost:8042").rstrip("/")
    try:
        with get_orthanc_client(url=base_url, username=username, password=password, timeout=5.0) as client:
            resp = client.get("/system")
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "online": True,
                    "url": base_url,
                    "version": data.get("Version", "Unknown"),
                    "name": data.get("Name", "Orthanc"),
                    "dicom_aet": data.get("DicomAet", ""),
                    "dicom_port": data.get("DicomPort"),
                    "http_port": data.get("HttpPort"),
                    "storage_area": data.get("StorageArea"),
                }
            return {
                "online": False,
                "url": base_url,
                "error": f"Orthanc returned HTTP status {resp.status_code}: {resp.text[:200]}",
            }
    except Exception as exc:
        return {
            "online": False,
            "url": base_url,
            "error": f"Could not connect to Orthanc at {base_url}: {exc}",
        }


# ---------------------------------------------------------------------------
# Patient Search
# ---------------------------------------------------------------------------

def search_orthanc_patients(
    query: str = "",
    limit: int = 50,
    db: Optional[Session] = None,
) -> list[dict[str, Any]]:
    """
    Search Orthanc for patients matching query in PatientID or PatientName.
    Returns list of patient summaries with local DB import status.
    """
    matched_orthanc_ids: list[str] = []
    seen_ids: set[str] = set()

    with get_orthanc_client() as client:
        q = (query or "").strip()
        if not q or q == "*":
            # List recent patients
            try:
                resp = client.get(f"/patients?limit={limit}")
                if resp.status_code == 200:
                    for pid in resp.json():
                        if pid not in seen_ids:
                            seen_ids.add(pid)
                            matched_orthanc_ids.append(pid)
            except Exception as exc:
                logger.error(f"Failed to fetch Orthanc patient list: {exc}")
                raise ValueError(f"Failed to query Orthanc: {exc}")
        else:
            # Query by PatientID and PatientName
            for tag_name in ["PatientID", "PatientName"]:
                try:
                    payload = {
                        "Level": "Patient",
                        "Query": {tag_name: f"*{q}*"},
                        "Limit": limit,
                    }
                    resp = client.post("/tools/find", json=payload)
                    if resp.status_code == 200:
                        results = resp.json()
                        for item in results:
                            pid = item if isinstance(item, str) else item.get("ID")
                            if pid and pid not in seen_ids:
                                seen_ids.add(pid)
                                matched_orthanc_ids.append(pid)
                except Exception as exc:
                    logger.debug(f"Orthanc find by {tag_name} error: {exc}")

        # Fetch details for each matched patient
        patients_list: list[dict[str, Any]] = []
        for orthanc_id in matched_orthanc_ids[:limit]:
            try:
                p_resp = client.get(f"/patients/{orthanc_id}")
                if p_resp.status_code != 200:
                    continue
                p_data = p_resp.json()
                main_tags = p_data.get("MainDicomTags", {})
                pid = str(main_tags.get("PatientID", "UNKNOWN") or "UNKNOWN")
                pname = str(main_tags.get("PatientName", "") or "").replace("^", " ").strip()
                dob = str(main_tags.get("PatientBirthDate", "") or "")
                sex = str(main_tags.get("PatientSex", "") or "")

                # Check if patient exists in local Virtual-PSQA database
                is_imported = False
                local_patient_id: Optional[int] = None
                local_plans: list[dict[str, Any]] = []

                if db is not None:
                    local_p = db.query(Patient).filter_by(patient_id=pid).first()
                    if local_p:
                        is_imported = True
                        local_patient_id = local_p.id
                        local_plans = [
                            {
                                "id": pl.id,
                                "plan_label": pl.plan_label,
                                "plan_name": pl.plan_name,
                                "qa_status": pl.qa_status,
                            }
                            for pl in local_p.plans
                        ]

                patients_list.append({
                    "orthanc_id": orthanc_id,
                    "patient_id": pid,
                    "patient_name": pname,
                    "date_of_birth": dob,
                    "sex": sex,
                    "studies_count": len(p_data.get("Studies", [])),
                    "is_imported": is_imported,
                    "local_patient_id": local_patient_id,
                    "local_plans": local_plans,
                })
            except Exception as exc:
                logger.warning(f"Error reading patient {orthanc_id}: {exc}")

    return patients_list


# ---------------------------------------------------------------------------
# Patient Details & Series Discovery
# ---------------------------------------------------------------------------

def get_orthanc_patient_details(
    patient_orthanc_id: str,
    db: Optional[Session] = None,
) -> dict[str, Any]:
    """
    Retrieve all studies and series for a patient in Orthanc,
    categorized into Plans, RT Records, and Offline Images (CBCT / REG).
    """
    with get_orthanc_client() as client:
        p_resp = client.get(f"/patients/{patient_orthanc_id}")
        if p_resp.status_code == 404:
            raise ValueError(f"Patient {patient_orthanc_id} not found in Orthanc.")
        if p_resp.status_code != 200:
            raise ValueError(f"Orthanc error {p_resp.status_code}: {p_resp.text}")

        p_data = p_resp.json()
        main_tags = p_data.get("MainDicomTags", {})
        pid = str(main_tags.get("PatientID", "UNKNOWN") or "UNKNOWN")
        pname = str(main_tags.get("PatientName", "") or "").replace("^", " ").strip()
        dob = str(main_tags.get("PatientBirthDate", "") or "")
        sex = str(main_tags.get("PatientSex", "") or "")

        studies_data: list[dict[str, Any]] = []
        all_series: list[dict[str, Any]] = []

        study_ids = p_data.get("Studies", [])
        for sid in study_ids:
            s_resp = client.get(f"/studies/{sid}")
            if s_resp.status_code != 200:
                continue
            study = s_resp.json()
            st_tags = study.get("MainDicomTags", {})
            study_desc = str(st_tags.get("StudyDescription", "") or "")
            study_date = str(st_tags.get("StudyDate", "") or "")
            study_uid = str(st_tags.get("StudyInstanceUID", "") or "")

            series_in_study: list[dict[str, Any]] = []
            for ser_id in study.get("Series", []):
                ser_resp = client.get(f"/series/{ser_id}")
                if ser_resp.status_code != 200:
                    continue
                ser = ser_resp.json()
                ser_tags = ser.get("MainDicomTags", {})
                modality = str(ser_tags.get("Modality", "") or "").upper()
                ser_desc = str(ser_tags.get("SeriesDescription", "") or "")
                ser_date = str(ser_tags.get("SeriesDate", "") or study_date)
                ser_time = str(ser_tags.get("SeriesTime", "") or "")
                ser_uid = str(ser_tags.get("SeriesInstanceUID", "") or "")
                ser_num = str(ser_tags.get("SeriesNumber", "") or "")
                instances = ser.get("Instances", [])
                num_instances = len(instances)

                # Inspect first instance for deeper DICOM tags
                extra_tags: dict[str, Any] = {}
                if instances:
                    try:
                        inst_resp = client.get(f"/instances/{instances[0]}/simplified-tags")
                        if inst_resp.status_code == 200:
                            extra_tags = inst_resp.json()
                    except Exception:
                        pass

                series_item = {
                    "series_id": ser_id,
                    "study_id": sid,
                    "series_instance_uid": ser_uid,
                    "modality": modality,
                    "series_description": ser_desc,
                    "series_number": ser_num,
                    "series_date": ser_date,
                    "series_time": ser_time,
                    "num_instances": num_instances,
                    "study_description": study_desc,
                    "study_date": study_date,
                    "study_instance_uid": study_uid,
                    "sop_class_uid": str(extra_tags.get("SOPClassUID", "")),
                    "sop_instance_uid": str(extra_tags.get("SOPInstanceUID", "")),
                    "extra": extra_tags,
                }
                series_in_study.append(series_item)
                all_series.append(series_item)

            studies_data.append({
                "study_id": sid,
                "study_description": study_desc,
                "study_date": study_date,
                "study_instance_uid": study_uid,
                "series_count": len(series_in_study),
                "series": series_in_study,
            })

        # Categorize into Plans, RT Records, and Offline Images
        plans: list[dict[str, Any]] = []
        rt_records: list[dict[str, Any]] = []
        offline_images: list[dict[str, Any]] = []

        # Find RT Plans
        rtplan_series = [
            s for s in all_series
            if s["modality"] in ("RTPLAN", "RTIBTR")
            and s["sop_class_uid"] not in ("1.2.840.10008.5.1.4.1.1.481.4", "1.2.840.10008.5.1.4.1.1.481.7")
        ]

        for ps in rtplan_series:
            p_extra = ps["extra"]
            plan_label = str(p_extra.get("RTPlanLabel", "") or p_extra.get("RTPlanName", "") or ps["series_description"] or "Unnamed Plan")
            plan_name = str(p_extra.get("RTPlanName", "") or plan_label)
            plan_sop = ps["sop_instance_uid"]

            # Number of fractions from FractionGroupSequence
            n_fractions: Optional[int] = None
            fg_seq = p_extra.get("FractionGroupSequence", [])
            if fg_seq and isinstance(fg_seq, list) and len(fg_seq) > 0:
                try:
                    n_fractions = int(fg_seq[0].get("NumberOfFractionsPlanned", 0)) or None
                except (TypeError, ValueError):
                    pass

            # Match RTDOSE, RTSTRUCT, Planning CT in same study
            study_series = [s for s in all_series if s["study_id"] == ps["study_id"]]

            dose_cand = [s for s in study_series if s["modality"] == "RTDOSE"]
            struct_cand = [s for s in study_series if s["modality"] == "RTSTRUCT"]
            ct_cand = [s for s in study_series if s["modality"] == "CT" and not ("cbct" in s["series_description"].lower())]
            if not ct_cand:
                # Fallback: largest CT series in study
                all_cts = [s for s in study_series if s["modality"] == "CT"]
                if all_cts:
                    ct_cand = [max(all_cts, key=lambda c: c["num_instances"])]

            # Check if this plan is already imported into local DB
            is_imported = False
            local_plan_id: Optional[int] = None
            if db is not None and plan_sop:
                existing_p = db.query(Plan).filter_by(rtplan_uid=plan_sop).first()
                if existing_p:
                    is_imported = True
                    local_plan_id = existing_p.id

            plans.append({
                "plan_series_id": ps["series_id"],
                "plan_label": plan_label,
                "plan_name": plan_name,
                "rtplan_uid": plan_sop,
                "study_id": ps["study_id"],
                "study_description": ps["study_description"],
                "study_date": ps["study_date"],
                "series_date": ps["series_date"],
                "number_of_fractions": n_fractions,
                "dose_series_id": dose_cand[0]["series_id"] if dose_cand else None,
                "dose_series_description": dose_cand[0]["series_description"] if dose_cand else None,
                "struct_series_id": struct_cand[0]["series_id"] if struct_cand else None,
                "struct_series_description": struct_cand[0]["series_description"] if struct_cand else None,
                "planning_ct_series_id": ct_cand[0]["series_id"] if ct_cand else None,
                "planning_ct_slices": ct_cand[0]["num_instances"] if ct_cand else 0,
                "is_imported": is_imported,
                "local_plan_id": local_plan_id,
            })

        # Find RT Records (modality RTRECORD or RT Beams Treatment Record SOP Classes)
        rec_series = [
            s for s in all_series
            if s["modality"] in ("RTRECORD", "RTIBTR")
            or s["sop_class_uid"] in ("1.2.840.10008.5.1.4.1.1.481.4", "1.2.840.10008.5.1.4.1.1.481.7")
        ]
        for rs in rec_series:
            r_extra = rs["extra"]
            deliv_type = "verification" if (
                "verif" in rs["series_description"].lower()
                or "verification" in str(r_extra).lower()
                or r_extra.get("TreatmentDeliveryType") == "TREATMENT_VERIF"
            ) else "curative"

            fx_num = 0 if deliv_type == "verification" else 1
            fg_seq = r_extra.get("FractionGroupSequence", [])
            if fg_seq and isinstance(fg_seq, list) and len(fg_seq) > 0:
                try:
                    fx_num = int(fg_seq[0].get("ReferencedFractionNumber", fx_num))
                except (TypeError, ValueError):
                    pass

            treat_date = str(r_extra.get("TreatmentDate", rs["series_date"]) or "")
            treat_time = str(r_extra.get("TreatmentTime", rs["series_time"]) or "")

            # Check if imported in local fractions
            is_imported = False
            local_fx_id: Optional[int] = None
            is_interrupted = False
            interruption_reason: Optional[str] = None
            if db is not None and rs["sop_instance_uid"]:
                existing_f = db.query(Fraction).filter_by(rtrecord_uid=rs["sop_instance_uid"]).first()
                if existing_f:
                    is_imported = True
                    local_fx_id = existing_f.id
                    is_interrupted = bool(getattr(existing_f, "is_interrupted", False))
                    interruption_reason = getattr(existing_f, "interruption_reason", None)

            if not is_imported:
                term_status = str(r_extra.get("TreatmentTerminationStatus", "") or "").upper()
                status_comm = str(r_extra.get("TreatmentStatusComment", "") or "").upper()
                if term_status and term_status not in ("NORMAL", ""):
                    is_interrupted = True
                    interruption_reason = f"Termination status: {term_status}"
                elif any(kw in status_comm for kw in ("INTERRUPT", "PARTIAL", "ABORT", "INCOMPLETE")):
                    is_interrupted = True
                    interruption_reason = f"Status comment indicates partial delivery ('{status_comm}')"

            rt_records.append({
                "series_id": rs["series_id"],
                "series_instance_uid": rs["series_instance_uid"],
                "sop_instance_uid": rs["sop_instance_uid"],
                "series_description": rs["series_description"],
                "delivery_type": deliv_type,
                "fraction_number": fx_num,
                "treatment_date": treat_date,
                "treatment_time": treat_time,
                "study_description": rs["study_description"],
                "is_imported": is_imported,
                "local_fraction_id": local_fx_id,
                "is_interrupted": is_interrupted,
                "interruption_reason": interruption_reason,
            })

        # Find Offline Images (CBCT CT scans & REG registrations)
        # Avoid CT series that were flagged as the primary planning CT
        planning_ct_ids = {p["planning_ct_series_id"] for p in plans if p["planning_ct_series_id"]}

        for s in all_series:
            mod = s["modality"]
            is_reg = mod == "REG" or s["sop_class_uid"] in ("1.2.840.10008.5.1.4.1.1.66.1", "1.2.840.10008.5.1.4.1.1.66.2")
            is_ct = mod == "CT"

            if is_reg or is_ct:
                is_planning_ct = s["series_id"] in planning_ct_ids
                offline_images.append({
                    "series_id": s["series_id"],
                    "study_id": s["study_id"],
                    "series_instance_uid": s["series_instance_uid"],
                    "modality": mod,
                    "kind": "reg" if is_reg else ("planning_ct" if is_planning_ct else "cbct"),
                    "series_description": s["series_description"],
                    "series_date": s["series_date"],
                    "series_time": s["series_time"],
                    "num_instances": s["num_instances"],
                    "study_description": s["study_description"],
                    "frame_of_reference_uid": str(s["extra"].get("FrameOfReferenceUID", "")),
                })

    # Check local patient status
    local_p = db.query(Patient).filter_by(patient_id=pid).first() if db else None

    return {
        "patient": {
            "orthanc_id": patient_orthanc_id,
            "patient_id": pid,
            "patient_name": pname,
            "date_of_birth": dob,
            "sex": sex,
            "is_imported": local_p is not None,
            "local_patient_id": local_p.id if local_p else None,
        },
        "plans": plans,
        "rt_records": sorted(rt_records, key=lambda r: (r["treatment_date"], r["treatment_time"])),
        "offline_images": sorted(offline_images, key=lambda i: (i["series_date"], i["series_time"])),
        "studies": studies_data,
    }


# ---------------------------------------------------------------------------
# Import Plan from Orthanc
# ---------------------------------------------------------------------------

def import_plan_from_orthanc(
    plan_series_id: str,
    dose_series_id: Optional[str] = None,
    struct_series_id: Optional[str] = None,
    ct_series_id: Optional[str] = None,
    db: Optional[Session] = None,
    background_tasks: Optional[Any] = None,
) -> dict[str, Any]:
    """
    Downloads RTPLAN, RTDOSE, RTSTRUCT, and Planning CT series archives from Orthanc,
    extracts them to a temporary workspace, and ingests them into Virtual-PSQA.
    """
    if db is None:
        raise ValueError("Database session is required for plan ingestion.")

    series_to_fetch = [plan_series_id]
    if dose_series_id:
        series_to_fetch.append(dose_series_id)
    if struct_series_id:
        series_to_fetch.append(struct_series_id)
    if ct_series_id:
        series_to_fetch.append(ct_series_id)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        with get_orthanc_client(timeout=180.0) as client:
            for s_idx, sid in enumerate(series_to_fetch):
                logger.info(f"Downloading series archive from Orthanc: {sid}")
                resp = client.get(f"/series/{sid}/archive")
                if resp.status_code != 200:
                    logger.warning(f"Could not download series archive for {sid}: HTTP {resp.status_code}")
                    continue

                # Extract zip in memory
                try:
                    with zipfile.ZipFile(io.BytesIO(resp.content), "r") as zf:
                        for member in zf.infolist():
                            if member.is_dir():
                                continue
                            filename = Path(member.filename).name
                            dest_file = tmp_path / f"s{s_idx}_{filename}"
                            dest_file.write_bytes(zf.read(member.filename))
                except Exception as exc:
                    logger.error(f"Error extracting archive for series {sid}: {exc}")
                    raise ValueError(f"Failed to extract DICOM files for series {sid}: {exc}")

        # Ingest the extracted directory into Virtual-PSQA
        try:
            result = ingest_dicom_directory(str(tmp_path), db)
        except Exception as exc:
            logger.error(f"Failed to ingest downloaded DICOM files from Orthanc: {exc}", exc_info=True)
            raise ValueError(f"Ingestion failed: {exc}")

        # Auto-run Stage 1 pipeline if configured
        if settings.PIPELINE_AUTO_RUN and result.get("plan_id"):
            from services.pipeline import run_stage1
            if background_tasks is not None:
                background_tasks.add_task(run_stage1, result["plan_id"], False)
            else:
                import threading
                threading.Thread(target=run_stage1, args=(result["plan_id"], False), daemon=True).start()

        return result


# ---------------------------------------------------------------------------
# Import RT Records from Orthanc
# ---------------------------------------------------------------------------

def list_orthanc_rtrecords_for_plan(plan_id: int, db: Session) -> list[dict[str, Any]]:
    """List all RT Records in Orthanc matching the patient for this plan."""
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise ValueError(f"Plan {plan_id} not found")

    patient = db.query(Patient).filter_by(id=plan.patient_id).first()
    if not patient:
        raise ValueError(f"Patient for plan {plan_id} not found")

    # Search for patient in Orthanc
    orthanc_patients = search_orthanc_patients(query=patient.patient_id, limit=5, db=db)
    if not orthanc_patients:
        # Try search by patient name
        orthanc_patients = search_orthanc_patients(query=patient.patient_name, limit=5, db=db)
    if not orthanc_patients:
        return []

    target_orthanc_id = orthanc_patients[0]["orthanc_id"]
    details = get_orthanc_patient_details(target_orthanc_id, db=db)
    return details.get("rt_records", [])


def import_rtrecords_from_orthanc(
    plan_id: int,
    series_ids: list[str],
    db: Session,
) -> dict[str, Any]:
    """
    Downloads selected RT Record series archives from Orthanc, extracts .dcm files,
    and ingests them to record fraction delivery and enable Stage 2 Delivery QA.
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise ValueError(f"Plan {plan_id} not found")

    if not series_ids:
        raise ValueError("No RT Record series specified for import.")

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        dcm_files: list[str] = []

        with get_orthanc_client(timeout=120.0) as client:
            for s_idx, sid in enumerate(series_ids):
                resp = client.get(f"/series/{sid}/archive")
                if resp.status_code != 200:
                    logger.warning(f"Could not download RT Record series archive {sid}: HTTP {resp.status_code}")
                    continue
                try:
                    with zipfile.ZipFile(io.BytesIO(resp.content), "r") as zf:
                        for member in zf.infolist():
                            if member.is_dir():
                                continue
                            filename = Path(member.filename).name
                            dest_file = tmp_path / f"rec_{s_idx}_{filename}"
                            dest_file.write_bytes(zf.read(member.filename))
                            dcm_files.append(str(dest_file))
                except Exception as exc:
                    logger.error(f"Error extracting RT Record archive {sid}: {exc}")

        if not dcm_files:
            raise ValueError("No valid DICOM files downloaded from the specified RT Record series.")

        result = ingest_rtrecord_files(dcm_files, db)
        return result


# ---------------------------------------------------------------------------
# Import Offline Images (CBCT & REG) from Orthanc
# ---------------------------------------------------------------------------

def list_orthanc_offline_images_for_plan(plan_id: int, db: Session) -> dict[str, Any]:
    """List available CBCT series and REG objects in Orthanc for this plan's patient."""
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise ValueError(f"Plan {plan_id} not found")

    patient = db.query(Patient).filter_by(id=plan.patient_id).first()
    if not patient:
        raise ValueError(f"Patient for plan {plan_id} not found")

    orthanc_patients = search_orthanc_patients(query=patient.patient_id, limit=5, db=db)
    if not orthanc_patients:
        orthanc_patients = search_orthanc_patients(query=patient.patient_name, limit=5, db=db)
    if not orthanc_patients:
        return {"cbct_series": [], "reg_series": [], "existing_fractions": []}

    target_orthanc_id = orthanc_patients[0]["orthanc_id"]
    details = get_orthanc_patient_details(target_orthanc_id, db=db)

    offline_all = details.get("offline_images", [])
    cbct_series = [i for i in offline_all if i["kind"] in ("cbct", "planning_ct")]
    reg_series = [i for i in offline_all if i["kind"] == "reg"]

    # Gather existing fractions in Virtual-PSQA
    existing_fractions = []
    fractions = db.query(Fraction).filter_by(plan_id=plan_id).order_by(Fraction.fraction_number).all()
    scts = db.query(SyntheticCT).filter_by(plan_id=plan_id).all()
    sct_fx_set = {s.fraction_number for s in scts}

    for f in fractions:
        existing_fractions.append({
            "fraction_number": f.fraction_number,
            "delivery_date": str(f.delivery_date) if f.delivery_date else None,
            "delivery_type": f.delivery_type,
            "has_cbct": f.fraction_number in sct_fx_set,
        })

    return {
        "cbct_series": cbct_series,
        "reg_series": reg_series,
        "existing_fractions": existing_fractions,
    }


def import_offline_images_from_orthanc(
    plan_id: int,
    fraction_number: int,
    cbct_series_id: str,
    reg_series_id: Optional[str] = None,
    db: Optional[Session] = None,
) -> dict[str, Any]:
    """
    Downloads CBCT series and optional REG spatial registration series from Orthanc,
    ingests them into `synthetic_ct/fx_{fraction_number}/cbct_raw` and `reg`,
    and updates the database and cache for immediate Offline Image Review (OIR).
    """
    if db is None:
        raise ValueError("Database session is required.")

    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise ValueError(f"Plan {plan_id} not found")

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        dcm_files: list[Path] = []

        with get_orthanc_client(timeout=180.0) as client:
            # 1. Download CBCT
            logger.info(f"Downloading CBCT series {cbct_series_id} for plan {plan_id} fx {fraction_number}...")
            c_resp = client.get(f"/series/{cbct_series_id}/archive")
            if c_resp.status_code != 200:
                raise ValueError(f"Failed to download CBCT series archive: HTTP {c_resp.status_code}")

            try:
                with zipfile.ZipFile(io.BytesIO(c_resp.content), "r") as zf:
                    for member in zf.infolist():
                        if member.is_dir():
                            continue
                        filename = Path(member.filename).name
                        dest_file = tmp_path / f"cbct_{filename}"
                        dest_file.write_bytes(zf.read(member.filename))
                        dcm_files.append(dest_file)
            except Exception as exc:
                raise ValueError(f"Failed to extract CBCT series archive: {exc}")

            # 2. Download REG if specified
            if reg_series_id:
                logger.info(f"Downloading REG series {reg_series_id}...")
                r_resp = client.get(f"/series/{reg_series_id}/archive")
                if r_resp.status_code == 200:
                    try:
                        with zipfile.ZipFile(io.BytesIO(r_resp.content), "r") as zf:
                            for member in zf.infolist():
                                if member.is_dir():
                                    continue
                                filename = Path(member.filename).name
                                dest_file = tmp_path / f"reg_{filename}"
                                dest_file.write_bytes(zf.read(member.filename))
                                dcm_files.append(dest_file)
                    except Exception as exc:
                        logger.warning(f"Failed to extract REG series archive: {exc}")

        # Ingest CBCT slices and REG files
        sct, dest_dir = ingest_cbct_series(
            plan_id=plan_id,
            fraction_number=fraction_number,
            source_paths=dcm_files,
            db=db,
        )

        # Clear OIR in-memory volume cache so fresh data loads immediately
        try:
            from services.oir_service import _VOLUME_CACHE
            _VOLUME_CACHE.clear()
        except Exception:
            pass

        return {
            "plan_id": plan_id,
            "fraction_number": fraction_number,
            "status": "cbct_uploaded",
            "num_slices": sct.cbct_num_slices or sct.num_slices,
            "cbct_dir": str(dest_dir),
            "message": f"Successfully imported CBCT ({sct.cbct_num_slices or sct.num_slices} slices) and registrations for Fraction {fraction_number}.",
        }
