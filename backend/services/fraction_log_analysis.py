"""
services/fraction_log_analysis.py -- Comprehensive clinical delivery log analysis.

Extracts fraction workflow parameters, 6-DoF couch position/angles, gantry angles,
primary/secondary meterset deviations, and layer-by-layer spot positioning pass
rates (0.5 mm and 2.0 mm tolerances) directly from RT Ion Records and RT Ion Plans,
matching the standard clinical ProNova TRCS / vendor QA report structure.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pydicom
from sqlalchemy.orm import Session

from config import settings
from models.fraction import Fraction
from models.gamma_result import GammaResult
from models.plan import Plan
from services.log_reconstruction import (
    _extract_delivered_spots,
    _extract_prescribed_spots,
    _normalize_beam_name,
)
from services.interruption_detector import detect_record_interruption

logger = logging.getLogger(__name__)


def _find_plan_dicom(store_path: str, rtplan_uid: Optional[str] = None) -> Optional[pydicom.Dataset]:
    """Find the RT Ion Plan dataset in the plan store directory."""
    path = Path(store_path)
    if not path.is_absolute():
        path = (Path(settings.DICOM_STORE_PATH).parent / store_path).resolve()
        if not path.exists():
            path = Path(store_path).resolve()

    if not path.exists():
        return None

    for f in sorted(path.rglob("*.dcm")):
        try:
            ds = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
            if str(getattr(ds, "Modality", "")).upper() in ("RTPLAN", "RTIBTR"):
                if rtplan_uid:
                    if str(getattr(ds, "SOPInstanceUID", "")) == str(rtplan_uid):
                        return pydicom.dcmread(str(f), force=True)
                else:
                    return pydicom.dcmread(str(f), force=True)
        except Exception:
            continue
    return None


def _format_date(raw: str) -> str:
    if len(raw) == 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw


def _format_time(raw: str) -> str:
    if len(raw) >= 6:
        return f"{raw[:2]}:{raw[2:4]}:{raw[4:6]}"
    return raw


def analyze_fraction_log(plan_id: int, fraction_number: int, db: Session) -> Optional[dict[str, Any]]:
    """Generate the full clinical fraction log QA analysis for a single fraction."""
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        return None

    frac = (
        db.query(Fraction)
        .filter_by(plan_id=plan_id, fraction_number=fraction_number)
        .order_by(Fraction.id.desc())
        .first()
    )
    if not frac or not frac.rtrecord_path:
        return None

    rec_path = Path(frac.rtrecord_path)
    if not rec_path.is_absolute():
        rec_path = (Path(settings.DICOM_STORE_PATH).parent / frac.rtrecord_path).resolve()
        if not rec_path.exists():
            rec_path = Path(frac.rtrecord_path).resolve()

    if not rec_path.exists():
        logger.warning(f"RT Ion Record not found at {rec_path}")
        return None

    plan_ds = _find_plan_dicom(plan.dicom_store_path, plan.rtplan_uid)
    if not plan_ds:
        logger.warning(f"RT Ion Plan not found for plan {plan_id}")
        return None

    record_ds = pydicom.dcmread(str(rec_path), force=True)

    operator = str(getattr(record_ds, "OperatorsName", "Operator") or "Operator")
    room = "Franklin - TR1"
    try:
        room = str(record_ds.TreatmentMachineSequence[0].TreatmentMachineName)
    except Exception:
        pass

    raw_date = str(getattr(record_ds, "TreatmentDate", "") or "")
    raw_time = str(getattr(record_ds, "TreatmentTime", "") or "")
    tdate = _format_date(raw_date)
    ttime = _format_time(raw_time)

    # Pre-fetch gamma results for log_vs_Rx on this fraction
    gamma_rows = (
        db.query(GammaResult)
        .filter_by(plan_id=plan_id, fraction_number=fraction_number, comparison_type="log_vs_Rx")
        .all()
    )
    gamma_by_beam = {r.field_name: r for r in gamma_rows}

    # Map beams
    plan_beams = {
        _normalize_beam_name(b.BeamName).upper(): (idx, b)
        for idx, b in enumerate(plan_ds.IonBeamSequence)
    }

    beams_out = []
    total_rx_mu = 0.0
    total_del_pri_mu = 0.0

    for r_idx, r_beam in enumerate(record_ds.TreatmentSessionIonBeamSequence):
        raw_name = str(r_beam.BeamName)
        norm_key = _normalize_beam_name(raw_name).upper()
        if norm_key not in plan_beams:
            continue
        p_idx, p_beam = plan_beams[norm_key]
        canonical_name = str(p_beam.BeamName)

        # Gantry & Couch geometry
        planned_gantry = float(getattr(p_beam.IonControlPointSequence[0], "GantryAngle", 0.0))
        actual_gantry = float(getattr(r_beam.IonControlPointDeliverySequence[0], "GantryAngle", planned_gantry))

        cp0 = r_beam.IonControlPointDeliverySequence[0]
        table_lat = float(getattr(cp0, "TableTopLateralPosition", 0.0) or 0.0)
        table_long = float(getattr(cp0, "TableTopLongitudinalPosition", 0.0) or 0.0)
        table_vert = float(getattr(cp0, "TableTopVerticalPosition", 0.0) or 0.0)
        table_pitch = float(getattr(cp0, "TableTopPitchAngle", 0.0) or 0.0)
        table_roll = float(getattr(cp0, "TableTopRollAngle", 0.0) or 0.0)
        table_rot = float(getattr(cp0, "PatientSupportAngle", 0.0) or 0.0)

        spec_pri = float(getattr(r_beam, "SpecifiedPrimaryMeterset", getattr(p_beam, "FinalCumulativeMetersetWeight", 0.0)))
        spec_sec = float(getattr(r_beam, "SpecifiedSecondaryMeterset", spec_pri))
        del_pri = float(getattr(r_beam, "DeliveredPrimaryMeterset", 0.0))
        del_sec = float(getattr(r_beam, "DeliveredSecondaryMeterset", 0.0))

        dev_pri_mu = del_pri - spec_pri
        dev_pri_pct = (dev_pri_mu / spec_pri * 100.0) if spec_pri > 0 else 0.0
        dev_sec_mu = del_sec - spec_sec
        dev_sec_pct = (dev_sec_mu / spec_sec * 100.0) if spec_sec > 0 else 0.0

        total_rx_mu += spec_pri
        total_del_pri_mu += del_pri

        # Spot extraction
        rx = _extract_prescribed_spots(plan_ds, p_idx)
        dv = _extract_delivered_spots(record_ds, r_idx)
        dmask = dv[:, 0] != 0
        dv_clean = dv[dmask]

        n_matched = min(len(rx), len(dv_clean))
        if n_matched > 0:
            dx = dv_clean[:n_matched, 5] - rx[:n_matched, 4]
            dy = dv_clean[:n_matched, 6] - rx[:n_matched, 5]
            mag = np.hypot(dx, dy)
            x_p05 = float(np.round(100.0 * np.mean(np.abs(dx) <= 0.5), 2))
            x_p20 = float(np.round(100.0 * np.mean(np.abs(dx) <= 2.0), 2))
            y_p05 = float(np.round(100.0 * np.mean(np.abs(dy) <= 0.5), 2))
            y_p20 = float(np.round(100.0 * np.mean(np.abs(dy) <= 2.0), 2))
            mag_p05 = float(np.round(100.0 * np.mean(mag <= 0.5), 2))
            mag_p20 = float(np.round(100.0 * np.mean(mag <= 2.0), 2))
            max_abs_dx = float(np.round(np.max(np.abs(dx)), 3))
            max_abs_dy = float(np.round(np.max(np.abs(dy)), 3))
            max_mag = float(np.round(np.max(mag), 3))
        else:
            x_p05, x_p20, y_p05, y_p20, mag_p05, mag_p20 = 100.0, 100.0, 100.0, 100.0, 100.0, 100.0
            max_abs_dx, max_abs_dy, max_mag = 0.0, 0.0, 0.0

        # Layers breakdown
        energies = sorted(list(set(rx[:, 0])), reverse=True)
        layers = []
        cum_rx = 0.0
        cum_dv = 0.0
        for l_idx, E in enumerate(energies, 1):
            rx_l = rx[rx[:, 0] == E]
            dv_l = dv_clean[dv_clean[:, 1] == E]
            cum_rx += float(np.sum(rx_l[:, 1]))
            cum_dv += float(np.sum(dv_l[:, 2])) if len(dv_l) > 0 else 0.0

            n_sp = len(rx_l)
            if len(rx_l) == len(dv_l) and len(rx_l) > 0:
                l_dx = dv_l[:, 5] - rx_l[:, 4]
                l_dy = dv_l[:, 6] - rx_l[:, 5]
                l_mag = np.hypot(l_dx, l_dy)
                lx_p05 = float(np.round(100.0 * np.mean(np.abs(l_dx) <= 0.5), 1))
                lx_p20 = float(np.round(100.0 * np.mean(np.abs(l_dx) <= 2.0), 1))
                ly_p05 = float(np.round(100.0 * np.mean(np.abs(l_dy) <= 0.5), 1))
                ly_p20 = float(np.round(100.0 * np.mean(np.abs(l_dy) <= 2.0), 1))
                lmag_p05 = float(np.round(100.0 * np.mean(l_mag <= 0.5), 1))
                lmag_p20 = float(np.round(100.0 * np.mean(l_mag <= 2.0), 1))
                lmax_dx = float(np.round(np.max(np.abs(l_dx)), 2))
                lmax_dy = float(np.round(np.max(np.abs(l_dy)), 2))
                lmax_mag = float(np.round(np.max(l_mag), 2))
            else:
                lx_p05, lx_p20, ly_p05, ly_p20, lmag_p05, lmag_p20 = 100.0, 100.0, 100.0, 100.0, 100.0, 100.0
                lmax_dx, lmax_dy, lmax_mag = 0.0, 0.0, 0.0

            layers.append({
                "layer_index": l_idx,
                "energy_mev": float(np.round(E, 1)),
                "spot_count": n_sp,
                "cumulative_rx_mu": float(np.round(cum_rx, 2)),
                "cumulative_del_mu": float(np.round(cum_dv, 2)),
                "x_pass_05mm": lx_p05,
                "x_pass_20mm": lx_p20,
                "y_pass_05mm": ly_p05,
                "y_pass_20mm": ly_p20,
                "mag_pass_05mm": lmag_p05,
                "mag_pass_20mm": lmag_p20,
                "max_abs_dx_mm": lmax_dx,
                "max_abs_dy_mm": lmax_dy,
                "max_mag_mm": lmax_mag,
            })

        gamma_res = gamma_by_beam.get(canonical_name)
        g_pass = gamma_res.passing_rate if gamma_res else None
        g_status = gamma_res.passed if gamma_res else (g_pass >= 90.0 if g_pass is not None else True)

        beams_out.append({
            "beam_number": p_idx + 1,
            "beam_name": canonical_name,
            "record_beam_name": raw_name,
            "planned_gantry_angle": planned_gantry,
            "actual_gantry_angle": actual_gantry,
            "prescribed_mu": spec_pri,
            "delivered_primary_mu": del_pri,
            "delivered_secondary_mu": del_sec,
            "deviation_primary_mu": dev_pri_mu,
            "deviation_primary_pct": dev_pri_pct,
            "deviation_secondary_mu": dev_sec_mu,
            "deviation_secondary_pct": dev_sec_pct,
            "table_position": {
                "lateral_mm": table_lat,
                "longitudinal_mm": table_long,
                "vertical_mm": table_vert,
                "pitch_deg": table_pitch,
                "roll_deg": table_roll,
                "support_angle_deg": table_rot,
            },
            "position_pass_rates": {
                "x_05mm": x_p05,
                "x_20mm": x_p20,
                "y_05mm": y_p05,
                "y_20mm": y_p20,
                "mag_05mm": mag_p05,
                "mag_20mm": mag_p20,
                "max_abs_dx_mm": max_abs_dx,
                "max_abs_dy_mm": max_abs_dy,
                "max_mag_mm": max_mag,
            },
            "gamma_passing_rate": g_pass,
            "gamma_passed": g_status,
            "beam_termination_status": str(getattr(r_beam, "BeamTerminationStatus", "") or "NORMAL").strip().upper(),
            "n_layers": len(layers),
            "n_spots": len(rx),
            "layers": layers,
        })

    # Overall fraction gamma pass rate
    fraction_gamma = (
        float(np.mean([b["gamma_passing_rate"] for b in beams_out if b["gamma_passing_rate"] is not None]))
        if any(b["gamma_passing_rate"] is not None for b in beams_out)
        else None
    )

    interruption_info = detect_record_interruption(record_ds, plan_ds)
    is_interrupted = bool(getattr(frac, "is_interrupted", False) or interruption_info["is_interrupted"])
    interruption_reason = getattr(frac, "interruption_reason", None) or interruption_info["interruption_reason"]

    return {
        "plan_id": plan_id,
        "fraction_number": fraction_number,
        "patient_id": plan.patient.patient_id if plan.patient else "Unknown",
        "patient_name": plan.patient.patient_name if plan.patient else "Unknown",
        "plan_label": plan.plan_label,
        "fraction_number": fraction_number,
        "delivery_type": getattr(frac, "delivery_type", None) or ("verification" if fraction_number == 0 else "curative"),
        "is_verification": bool(getattr(frac, "delivery_type", None) == "verification" or fraction_number == 0),
        "is_interrupted": is_interrupted,
        "interruption_reason": interruption_reason,
        "interruption_type": interruption_info.get("interruption_type"),
        "interruption_details": interruption_info.get("details"),
        "operator": operator,
        "treatment_room": room,
        "treatment_date": tdate,
        "treatment_time": ttime,
        "n_fields": len(beams_out),
        "total_prescribed_mu": round(total_rx_mu, 2),
        "total_delivered_mu": round(total_del_pri_mu, 2),
        "total_mu_deviation_pct": round((total_del_pri_mu - total_rx_mu) / total_rx_mu * 100.0, 3) if total_rx_mu > 0 else 0.0,
        "fraction_gamma_passing_rate": round(fraction_gamma, 1) if fraction_gamma is not None else None,
        "fraction_gamma_passed": bool(fraction_gamma >= 90.0) if fraction_gamma is not None else True,
        "beams": beams_out,
    }


def list_plan_fraction_logs(plan_id: int, db: Session) -> list[dict[str, Any]]:
    """List summary metadata for all analyzed fractions of a plan."""
    fractions = (
        db.query(Fraction)
        .filter(Fraction.plan_id == plan_id, Fraction.rtrecord_path.isnot(None))
        .order_by(Fraction.fraction_number.asc())
        .all()
    )
    summaries = []
    for f in fractions:
        info = analyze_fraction_log(plan_id, f.fraction_number, db)
        if info:
            deliv_type = getattr(f, "delivery_type", None) or ("verification" if f.fraction_number == 0 else "curative")
            summaries.append({
                "fraction_number": f.fraction_number,
                "delivery_type": deliv_type,
                "is_verification": bool(deliv_type == "verification" or f.fraction_number == 0),
                "is_interrupted": bool(getattr(f, "is_interrupted", False) or info.get("is_interrupted", False)),
                "interruption_reason": getattr(f, "interruption_reason", None) or info.get("interruption_reason"),
                "qa_status": getattr(f, "qa_status", "pass"),
                "treatment_date": info["treatment_date"],
                "treatment_time": info["treatment_time"],
                "treatment_room": info["treatment_room"],
                "operator": info["operator"],
                "n_fields": info["n_fields"],
                "total_prescribed_mu": info["total_prescribed_mu"],
                "total_delivered_mu": info["total_delivered_mu"],
                "fraction_gamma_passing_rate": info["fraction_gamma_passing_rate"],
                "fraction_gamma_passed": info["fraction_gamma_passed"],
            })
    return summaries


def get_plan_couch_trends(plan_id: int, db: Session) -> dict[str, Any]:
    """
    Extract 6-DoF couch position and rotation tracking metrics across all delivered fractions.
    Returns per-beam series, multi-beam composite series, inter-fraction shifts (deltas from Fx 1),
    and clinical tolerance thresholds.
    """
    plan = db.query(Plan).filter(Plan.id == plan_id).first()
    if not plan:
        return {"plan_id": plan_id, "beams": [], "series_by_beam": {}, "fractions": []}

    plan_ds = _find_plan_dicom(plan.dicom_store_path, getattr(plan, "rtplan_uid", None))
    plan_beams_map: dict[str, dict[str, Any]] = {}
    beams_meta: list[dict[str, Any]] = []

    if plan_ds and hasattr(plan_ds, "IonBeamSequence"):
        for idx, b in enumerate(plan_ds.IonBeamSequence):
            b_name = str(b.BeamName)
            norm_key = _normalize_beam_name(b_name).upper()
            gantry = (
                float(getattr(b.IonControlPointSequence[0], "GantryAngle", 0.0))
                if hasattr(b, "IonControlPointSequence") and len(b.IonControlPointSequence) > 0
                else 0.0
            )
            beam_info = {
                "beam_number": idx + 1,
                "beam_name": b_name,
                "planned_gantry_angle": gantry,
            }
            plan_beams_map[norm_key] = beam_info
            beams_meta.append(beam_info)

    fractions = (
        db.query(Fraction)
        .filter(Fraction.plan_id == plan_id, Fraction.rtrecord_path.isnot(None))
        .order_by(Fraction.fraction_number.asc())
        .all()
    )

    if not fractions:
        return {
            "plan_id": plan_id,
            "plan_label": plan.plan_label,
            "beams": beams_meta,
            "series_by_beam": {},
            "fractions": [],
            "baseline_fraction": None,
            "tolerances": {
                "translation_action_mm": 3.0,
                "translation_tolerance_mm": 5.0,
                "rotation_action_deg": 1.0,
                "rotation_tolerance_deg": 2.0,
            },
        }

    extracted_fractions = []
    # Identify baseline fraction (first curative fraction, typically fx 1, or first available if none)
    baseline_fx_num = None
    for f in fractions:
        deliv = getattr(f, "delivery_type", None) or ("verification" if f.fraction_number == 0 else "curative")
        if deliv == "curative" and f.fraction_number > 0 and baseline_fx_num is None:
            baseline_fx_num = f.fraction_number

    if baseline_fx_num is None and fractions:
        baseline_fx_num = fractions[0].fraction_number

    baseline_by_beam: dict[str, dict[str, float]] = {}

    for f in fractions:
        try:
            record_ds = pydicom.dcmread(f.rtrecord_path, force=True)
        except Exception as e:
            logger.warning("Failed reading RT Record %s: %s", f.rtrecord_path, e)
            continue

        raw_date = str(getattr(record_ds, "TreatmentDate", "") or "")
        raw_time = str(getattr(record_ds, "TreatmentTime", "") or "")
        deliv_type = getattr(f, "delivery_type", None) or ("verification" if f.fraction_number == 0 else "curative")
        is_verif = bool(deliv_type == "verification" or f.fraction_number == 0)

        beam_positions: dict[str, dict[str, Any]] = {}

        for r_beam in getattr(record_ds, "TreatmentSessionIonBeamSequence", []):
            raw_name = str(r_beam.BeamName)
            norm_key = _normalize_beam_name(raw_name).upper()
            if norm_key in plan_beams_map:
                b_meta = plan_beams_map[norm_key]
                b_num = b_meta["beam_number"]
                canonical_name = b_meta["beam_name"]
            else:
                b_num = len(beam_positions) + 1
                canonical_name = raw_name

            cp0 = (
                r_beam.IonControlPointDeliverySequence[0]
                if hasattr(r_beam, "IonControlPointDeliverySequence") and len(r_beam.IonControlPointDeliverySequence) > 0
                else None
            )
            if not cp0:
                continue

            lat = float(getattr(cp0, "TableTopLateralPosition", 0.0) or 0.0)
            lng = float(getattr(cp0, "TableTopLongitudinalPosition", 0.0) or 0.0)
            vert = float(getattr(cp0, "TableTopVerticalPosition", 0.0) or 0.0)
            pitch = float(getattr(cp0, "TableTopPitchAngle", 0.0) or 0.0)
            roll = float(getattr(cp0, "TableTopRollAngle", 0.0) or 0.0)
            raw_rot = float(getattr(cp0, "PatientSupportAngle", 0.0) or 0.0)
            # Wrap support angle to [-180, 180)
            norm_rot = (raw_rot + 180.0) % 360.0 - 180.0

            pos_data = {
                "beam_number": b_num,
                "beam_name": canonical_name,
                "lateral_mm": round(lat, 2),
                "longitudinal_mm": round(lng, 2),
                "vertical_mm": round(vert, 2),
                "pitch_deg": round(pitch, 3),
                "roll_deg": round(roll, 3),
                "support_angle_deg": round(norm_rot, 2),
                "raw_support_angle_deg": round(raw_rot, 2),
            }

            beam_key = str(b_num)
            beam_positions[beam_key] = pos_data

            if f.fraction_number == baseline_fx_num and beam_key not in baseline_by_beam:
                baseline_by_beam[beam_key] = {
                    "lat": lat,
                    "lng": lng,
                    "vert": vert,
                    "pitch": pitch,
                    "roll": roll,
                    "rot": norm_rot,
                }

        # Calculate composite / mean across beams for this fraction
        if beam_positions:
            mean_lat = float(np.mean([p["lateral_mm"] for p in beam_positions.values()]))
            mean_lng = float(np.mean([p["longitudinal_mm"] for p in beam_positions.values()]))
            mean_vert = float(np.mean([p["vertical_mm"] for p in beam_positions.values()]))
            mean_pitch = float(np.mean([p["pitch_deg"] for p in beam_positions.values()]))
            mean_roll = float(np.mean([p["roll_deg"] for p in beam_positions.values()]))
            mean_rot = float(np.mean([p["support_angle_deg"] for p in beam_positions.values()]))
            mean_raw_rot = float(np.mean([p["raw_support_angle_deg"] for p in beam_positions.values()]))

            composite_pos = {
                "beam_number": 0,
                "beam_name": "Composite (All Beams)",
                "lateral_mm": round(mean_lat, 2),
                "longitudinal_mm": round(mean_lng, 2),
                "vertical_mm": round(mean_vert, 2),
                "pitch_deg": round(mean_pitch, 3),
                "roll_deg": round(mean_roll, 3),
                "support_angle_deg": round(mean_rot, 2),
                "raw_support_angle_deg": round(mean_raw_rot, 2),
            }
            beam_positions["all"] = composite_pos

            if f.fraction_number == baseline_fx_num and "all" not in baseline_by_beam:
                baseline_by_beam["all"] = {
                    "lat": mean_lat,
                    "lng": mean_lng,
                    "vert": mean_vert,
                    "pitch": mean_pitch,
                    "roll": mean_roll,
                    "rot": mean_rot,
                }

        extracted_fractions.append({
            "fraction_number": f.fraction_number,
            "treatment_date": _format_date(raw_date),
            "treatment_time": _format_time(raw_time),
            "delivery_type": deliv_type,
            "is_verification": is_verif,
            "beams": beam_positions,
        })

    # Now compute deltas for each beam relative to baseline fraction
    series_by_beam: dict[str, list[dict[str, Any]]] = {}

    for ef in extracted_fractions:
        fx_num = ef["fraction_number"]
        tdate = ef["treatment_date"]
        ttime = ef["treatment_time"]
        dtype = ef["delivery_type"]
        is_v = ef["is_verification"]

        for b_key, b_pos in ef["beams"].items():
            base = baseline_by_beam.get(b_key)
            if base:
                d_lat = b_pos["lateral_mm"] - base["lat"]
                d_lng = b_pos["longitudinal_mm"] - base["lng"]
                d_vert = b_pos["vertical_mm"] - base["vert"]
                d_3d = float(np.sqrt(d_lat**2 + d_lng**2 + d_vert**2))
                d_pitch = b_pos["pitch_deg"] - base["pitch"]
                d_roll = b_pos["roll_deg"] - base["roll"]
                d_rot = b_pos["support_angle_deg"] - base["rot"]
            else:
                d_lat = 0.0
                d_lng = 0.0
                d_vert = 0.0
                d_3d = 0.0
                d_pitch = 0.0
                d_roll = 0.0
                d_rot = 0.0

            pt = {
                "fraction_number": fx_num,
                "treatment_date": tdate,
                "treatment_time": ttime,
                "delivery_type": dtype,
                "is_verification": is_v,
                "lateral_mm": b_pos["lateral_mm"],
                "longitudinal_mm": b_pos["longitudinal_mm"],
                "vertical_mm": b_pos["vertical_mm"],
                "pitch_deg": b_pos["pitch_deg"],
                "roll_deg": b_pos["roll_deg"],
                "support_angle_deg": b_pos["support_angle_deg"],
                "raw_support_angle_deg": b_pos["raw_support_angle_deg"],
                "delta_lateral_mm": round(d_lat, 2),
                "delta_longitudinal_mm": round(d_lng, 2),
                "delta_vertical_mm": round(d_vert, 2),
                "delta_3d_mm": round(d_3d, 2),
                "delta_pitch_deg": round(d_pitch, 3),
                "delta_roll_deg": round(d_roll, 3),
                "delta_support_deg": round(d_rot, 2),
            }

            b_pos["delta_lateral_mm"] = pt["delta_lateral_mm"]
            b_pos["delta_longitudinal_mm"] = pt["delta_longitudinal_mm"]
            b_pos["delta_vertical_mm"] = pt["delta_vertical_mm"]
            b_pos["delta_3d_mm"] = pt["delta_3d_mm"]
            b_pos["delta_pitch_deg"] = pt["delta_pitch_deg"]
            b_pos["delta_roll_deg"] = pt["delta_roll_deg"]
            b_pos["delta_support_deg"] = pt["delta_support_deg"]

            if b_key not in series_by_beam:
                series_by_beam[b_key] = []
            series_by_beam[b_key].append(pt)

    # Compute summary stats across curative fractions for all beams
    curative_pts = [p for p in series_by_beam.get("all", []) if not p["is_verification"]]
    if curative_pts:
        max_d_lat = float(np.max([abs(p["delta_lateral_mm"]) for p in curative_pts]))
        max_d_lng = float(np.max([abs(p["delta_longitudinal_mm"]) for p in curative_pts]))
        max_d_vert = float(np.max([abs(p["delta_vertical_mm"]) for p in curative_pts]))
        max_d_3d = float(np.max([p["delta_3d_mm"] for p in curative_pts]))
        max_d_pitch = float(np.max([abs(p["delta_pitch_deg"]) for p in curative_pts]))
        max_d_roll = float(np.max([abs(p["delta_roll_deg"]) for p in curative_pts]))
        max_d_rot = float(np.max([abs(p["delta_support_deg"]) for p in curative_pts]))
    else:
        max_d_lat = max_d_lng = max_d_vert = max_d_3d = max_d_pitch = max_d_roll = max_d_rot = 0.0

    return {
        "plan_id": plan_id,
        "plan_label": plan.plan_label,
        "baseline_fraction": baseline_fx_num,
        "beams": beams_meta,
        "series_by_beam": series_by_beam,
        "fractions": extracted_fractions,
        "summary": {
            "max_delta_lateral_mm": round(max_d_lat, 2),
            "max_delta_longitudinal_mm": round(max_d_lng, 2),
            "max_delta_vertical_mm": round(max_d_vert, 2),
            "max_delta_3d_mm": round(max_d_3d, 2),
            "max_delta_pitch_deg": round(max_d_pitch, 3),
            "max_delta_roll_deg": round(max_d_roll, 3),
            "max_delta_support_deg": round(max_d_rot, 2),
        },
        "tolerances": {
            "translation_action_mm": 3.0,
            "translation_tolerance_mm": 5.0,
            "rotation_action_deg": 1.0,
            "rotation_tolerance_deg": 2.0,
        },
    }

