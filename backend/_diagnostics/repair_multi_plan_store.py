"""
repair_multi_plan_store.py -- Split combined multi-beamset/multi-plan stores into isolated folders and register plans.

Problem:
When multi-beamset plans (e.g. from RayStation) are ingested or uploaded together,
multiple RTPLAN and RTDOSE files may have been dumped into a single patient store directory.
This utility scans the DICOM store (or a specified directory), detects folders with multiple RTPLANs,
segregates matching RTDOSE, CT, and RTSTRUCT files into isolated plan folders:
    <DICOM_STORE_PATH>/<patient_id>/<plan_uid>/
and registers / updates the corresponding database records in psqa.db.

Usage:
    python backend/_diagnostics/repair_multi_plan_store.py                # Dry run scan
    python backend/_diagnostics/repair_multi_plan_store.py --apply        # Execute repair and DB update
    python backend/_diagnostics/repair_multi_plan_store.py --path <dir>   # Scan specific directory
    python backend/_diagnostics/repair_multi_plan_store.py --apply --run-stage1  # Repair and trigger pipeline
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# Add backend to sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import pydicom

from config import settings
from database import SessionLocal
from models.patient import Patient
from models.plan import Plan
from services.dicom_ingestor import (
    classify_dicom_files,
    extract_patient_info,
    extract_plan_metadata,
)


def inspect_store(store_path: Path) -> list[dict]:
    """
    Find directories containing more than one RTPLAN file.
    """
    multi_plan_dirs = []
    if not store_path.exists():
        return multi_plan_dirs

    # Search for all subdirectories containing .dcm files
    candidate_dirs = set()
    for f in store_path.rglob("*.dcm"):
        candidate_dirs.add(f.parent)

    for d in sorted(candidate_dirs):
        plans = []
        for f in d.glob("*.dcm"):
            try:
                ds = pydicom.dcmread(str(f), stop_before_pixels=True)
                mod = getattr(ds, "Modality", "")
                if mod in ("RTPLAN", "RTIBTR"):
                    plans.append({
                        "file": f,
                        "sop_uid": str(getattr(ds, "SOPInstanceUID", "")),
                        "label": str(getattr(ds, "RTPlanLabel", "") or getattr(ds, "RTPlanName", "") or "UNNAMED"),
                        "patient_id": str(getattr(ds, "PatientID", "") or "UNKNOWN"),
                    })
            except Exception:
                continue

        if len(plans) > 1:
            multi_plan_dirs.append({
                "directory": d,
                "plans": plans,
            })

    return multi_plan_dirs


def repair_directory(
    multi_info: dict,
    db,
    apply: bool = False,
    run_pipeline: bool = False,
) -> list[int]:
    """
    Split the combined folder into individual plan directories and upsert DB rows.
    """
    src_dir: Path = multi_info["directory"]
    plans: list[dict] = multi_info["plans"]
    print(f"\n[FOUND] Multi-plan store: {src_dir}")
    print(f"  Contains {len(plans)} RTPLANs:")
    for p in plans:
        print(f"    - {p['label']} (UID: {p['sop_uid']})")

    # Classify all files in src_dir
    classified = classify_dicom_files(str(src_dir))
    plan_paths = classified.get("RTPLAN", [])
    dose_paths = classified.get("RTDOSE", [])
    ct_paths = classified.get("CT", [])
    struct_paths = classified.get("RTSTRUCT", [])

    print(f"  Total files: {len(plan_paths)} RTPLAN, {len(dose_paths)} RTDOSE, {len(ct_paths)} CT, {len(struct_paths)} RTSTRUCT")

    # Map doses to plan UIDs
    doses_by_plan: dict[str, list[Path]] = {p["sop_uid"]: [] for p in plans}
    multi_plan_doses: list[Path] = []
    unmatched_doses: list[Path] = []

    for dpath in dose_paths:
        try:
            ds = pydicom.dcmread(str(dpath), stop_before_pixels=True)
            dose_sum_type = str(getattr(ds, "DoseSummationType", "") or "").upper()
            ref_seq = getattr(ds, "ReferencedRTPlanSequence", None)
            if dose_sum_type == "MULTI_PLAN" or (ref_seq and len(ref_seq) > 1):
                multi_plan_doses.append(dpath)
                continue

            matched = False
            if ref_seq and len(ref_seq) > 0:
                ref_uid = str(getattr(ref_seq[0], "ReferencedSOPInstanceUID", ""))
                if ref_uid in doses_by_plan:
                    doses_by_plan[ref_uid].append(dpath)
                    matched = True

            if not matched:
                unmatched_doses.append(dpath)
        except Exception:
            unmatched_doses.append(dpath)

    for p in plans:
        uid = p["sop_uid"]
        print(f"  Plan '{p['label']}': {len(doses_by_plan.get(uid, []))} associated RTDOSE files")
    if multi_plan_doses:
        print(f"  Composite/Multi-plan doses (shared): {len(multi_plan_doses)} files")
    if unmatched_doses:
        print(f"  Unmatched doses: {len(unmatched_doses)} files")

    if not apply:
        print("  [DRY RUN] No changes applied. Use --apply to execute.")
        return []

    # Execute repair
    repaired_plan_ids = []
    for plan_path in plan_paths:
        try:
            plan_dcm = pydicom.dcmread(str(plan_path), stop_before_pixels=True)
        except Exception as exc:
            print(f"  [ERROR] Could not read plan file {plan_path}: {exc}")
            continue

        patient_info = extract_patient_info(plan_dcm)
        plan_meta = extract_plan_metadata(plan_dcm)
        patient_id = patient_info["patient_id"]
        plan_uid = plan_meta["rtplan_uid"]

        # Destination directory for this plan
        dest_dir = Path(settings.DICOM_STORE_PATH) / patient_id / plan_uid
        dest_dir.mkdir(parents=True, exist_ok=True)

        # 1. Copy RTPLAN
        dest_plan_file = dest_dir / f"RP.{plan_uid}.dcm"
        if Path(plan_path).resolve() != dest_plan_file.resolve():
            shutil.copy2(plan_path, dest_plan_file)

        # 2. Copy matching RTDOSE files
        plan_doses = doses_by_plan.get(plan_uid, [])
        primary_dose_uid = None
        for dp in plan_doses:
            try:
                dds = pydicom.dcmread(str(dp), stop_before_pixels=True)
                duid = str(getattr(dds, "SOPInstanceUID", dp.name))
                d_dest = dest_dir / f"RD.{duid}.dcm"
                if Path(dp).resolve() != d_dest.resolve():
                    shutil.copy2(dp, d_dest)

                dsum = str(getattr(dds, "DoseSummationType", "") or "").upper()
                if dsum == "PLAN" or not primary_dose_uid:
                    primary_dose_uid = duid
            except Exception:
                continue

        # 3. Copy shared multi-plan doses
        for mp in multi_plan_doses:
            try:
                dds = pydicom.dcmread(str(mp), stop_before_pixels=True)
                duid = str(getattr(dds, "SOPInstanceUID", mp.name))
                m_dest = dest_dir / f"MULTI_PLAN_{duid}.dcm"
                if Path(mp).resolve() != m_dest.resolve():
                    shutil.copy2(mp, m_dest)
            except Exception:
                continue

        # 4. Copy CT slices
        for cp in ct_paths:
            try:
                c_dest = dest_dir / Path(cp).name
                if Path(cp).resolve() != c_dest.resolve():
                    shutil.copy2(cp, c_dest)
            except Exception:
                continue

        # 5. Copy RTSTRUCT
        primary_struct_uid = None
        for sp in struct_paths:
            try:
                sds = pydicom.dcmread(str(sp), stop_before_pixels=True)
                suid = str(getattr(sds, "SOPInstanceUID", Path(sp).name))
                s_dest = dest_dir / f"RS.{suid}.dcm"
                if Path(sp).resolve() != s_dest.resolve():
                    shutil.copy2(sp, s_dest)
                if not primary_struct_uid:
                    primary_struct_uid = suid
            except Exception:
                continue

        # 6. Upsert Patient and Plan in DB
        patient = db.query(Patient).filter_by(patient_id=patient_info["patient_id"]).first()
        if patient is None:
            patient = Patient(
                patient_id=patient_info["patient_id"],
                patient_name=patient_info["patient_name"],
                date_of_birth=patient_info.get("date_of_birth"),
                sex=patient_info.get("sex"),
            )
            db.add(patient)
            db.flush()

        plan_record = db.query(Plan).filter_by(rtplan_uid=plan_uid).first()
        store_path_str = str(dest_dir.as_posix())

        if plan_record:
            plan_record.patient_id = patient.id
            plan_record.plan_label = plan_meta["plan_label"]
            plan_record.plan_name = plan_meta["plan_name"]
            plan_record.treatment_site = plan_meta["treatment_site"]
            plan_record.number_of_fractions = plan_meta["number_of_fractions"]
            plan_record.number_of_fields = plan_meta["number_of_fields"]
            plan_record.dicom_store_path = store_path_str
            if primary_dose_uid:
                plan_record.rtdose_uid = primary_dose_uid
            if primary_struct_uid:
                plan_record.rtstruct_uid = primary_struct_uid
        else:
            plan_record = Plan(
                patient_id=patient.id,
                plan_label=plan_meta["plan_label"],
                plan_name=plan_meta["plan_name"],
                treatment_site=plan_meta["treatment_site"],
                number_of_fractions=plan_meta["number_of_fractions"],
                number_of_fields=plan_meta["number_of_fields"],
                dicom_store_path=store_path_str,
                rtplan_uid=plan_uid,
                rtdose_uid=primary_dose_uid,
                rtstruct_uid=primary_struct_uid,
                qa_status="pending",
            )
            db.add(plan_record)

        db.commit()
        db.refresh(plan_record)
        repaired_plan_ids.append(plan_record.id)
        print(f"  [SAVED] Plan {plan_record.id} ('{plan_record.plan_label}') -> {dest_dir}")

    if run_pipeline and repaired_plan_ids:
        print("  [PIPELINE] Triggering Stage 1 for repaired plans...")
        from services.pipeline import run_stage1
        for pid in repaired_plan_ids:
            try:
                run_stage1(pid, background=True)
                print(f"    Triggered Stage 1 for plan {pid}")
            except Exception as exc:
                print(f"    [WARN] Failed to trigger Stage 1 for plan {pid}: {exc}")

    return repaired_plan_ids


def main():
    parser = argparse.ArgumentParser(description="Repair and isolate multi-plan / multi-beamset DICOM directories.")
    parser.add_argument("--path", default=settings.DICOM_STORE_PATH, help="Path to search for DICOM files")
    parser.add_argument("--apply", action="store_true", help="Execute the repair and write changes to disk and database")
    parser.add_argument("--run-stage1", action="store_true", help="Automatically trigger Stage 1 QA pipeline for repaired plans")
    args = parser.parse_args()

    search_path = Path(args.path).resolve()
    print(f"Scanning for multi-plan stores under: {search_path}")

    multi_dirs = inspect_store(search_path)
    if not multi_dirs:
        print("No multi-plan combined stores found.")
        return

    print(f"Found {len(multi_dirs)} store directory/directories with multiple plans.")
    db = SessionLocal()
    try:
        total_repaired = 0
        for info in multi_dirs:
            pids = repair_directory(info, db, apply=args.apply, run_pipeline=args.run_stage1)
            total_repaired += len(pids)

        if args.apply:
            print(f"\n[DONE] Successfully processed and registered {total_repaired} plans.")
        else:
            print("\n[NOTE] Dry-run complete. Run with --apply to execute repair.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
