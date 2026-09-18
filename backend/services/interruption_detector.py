"""
services/interruption_detector.py -- Detection and diagnosis of interrupted / partial RT records.

Flags partial or interrupted treatment deliveries where:
  1. DICOM termination status indicates premature stop (BeamTerminationStatus != 'NORMAL',
     TreatmentTerminationStatus != 'NORMAL', TreatmentTerminationCode set).
  2. DICOM comments / delivery types indicate an interrupted delivery ('INTERRUPTED',
     'PARTIAL', 'ABORTED', 'CONTINUATION').
  3. Delivered meterset is substantially below prescribed/specified meterset (< 98%).
  4. Delivered spot count is substantially below prescribed spot count (< 95%).
  5. One or more planned beams were omitted from the delivery record.
  6. The session carried zero meterset (aborted before beam-on).
"""
from __future__ import annotations

import logging
from typing import Any, Optional
import pydicom

logger = logging.getLogger(__name__)

NORMAL_TERMINATION_STATUSES = {"NORMAL", ""}
INTERRUPTED_COMMENT_KEYWORDS = [
    "INTERRUPT",
    "PARTIAL",
    "ABORT",
    "INCOMPLETE",
    "TERMINAT",
    "PAUS",
    "STOPPED",
    "FAULT",
]


def _normalize_beam_name(name: str) -> str:
    """'LA:TX' -> 'LA'."""
    return str(name).split(":")[0].strip().upper()


def _get_tag_str(dcm: Any, tag: tuple[int, int], attr_name: str) -> str:
    """Safely extracts string value from DICOM element without returning DataElement repr."""
    try:
        if hasattr(dcm, "__contains__") and tag in dcm:
            val = dcm[tag].value
            return str(val or "").strip()
    except Exception:
        pass
    val = getattr(dcm, attr_name, "")
    if hasattr(val, "value"):
        val = val.value
    return str(val or "").strip()


def detect_record_interruption(
    record_dcm: pydicom.Dataset,
    plan_dcm: Optional[pydicom.Dataset] = None,
) -> dict[str, Any]:
    """
    Analyzes an RT Ion Record or RT Beams Treatment Record to determine if
    a partial / interrupted delivery was present.

    Returns:
      {
        "is_interrupted": bool,
        "interruption_reason": str | None,
        "interruption_type": str | None,
        "details": dict
      }
    """
    reasons: list[str] = []
    interruption_types: list[str] = []

    # 1. Dataset-level DICOM termination tags
    treatment_term_status = _get_tag_str(
        record_dcm, (0x3008, 0x002A), "TreatmentTerminationStatus"
    ).upper()
    if treatment_term_status and treatment_term_status not in NORMAL_TERMINATION_STATUSES:
        reasons.append(f"Treatment termination status: {treatment_term_status}")
        interruption_types.append("termination_status")

    rt_term_status = _get_tag_str(
        record_dcm, (0x300A, 0x0714), "RTTreatmentTerminationStatus"
    ).upper()
    if rt_term_status and rt_term_status not in NORMAL_TERMINATION_STATUSES:
        reasons.append(f"RT treatment termination status: {rt_term_status}")
        interruption_types.append("termination_status")

    comment = _get_tag_str(
        record_dcm, (0x3008, 0x0202), "TreatmentStatusComment"
    ).upper()
    for kw in INTERRUPTED_COMMENT_KEYWORDS:
        if kw in comment:
            reasons.append(f"Treatment status comment indicates interrupted delivery ('{comment}')")
            interruption_types.append("status_comment")
            break

    term_desc = _get_tag_str(
        record_dcm, (0x300A, 0x0730), "TreatmentTerminationDescription"
    )
    if term_desc:
        reasons.append(f"Termination description: {term_desc}")
        interruption_types.append("termination_description")

    reason_omission = _get_tag_str(
        record_dcm, (0x300C, 0x0112), "ReasonForOmission"
    )
    if reason_omission:
        reasons.append(f"Reason for omission: {reason_omission}")
        interruption_types.append("omission")

    deliv_type_top = _get_tag_str(
        record_dcm, (0x3008, 0x0024), "TreatmentDeliveryType"
    ).upper()
    if "CONTINUATION" in deliv_type_top:
        reasons.append("Treatment delivery type is marked CONTINUATION (continuation of interrupted session)")
        interruption_types.append("continuation_delivery")

    # 2. Extract record beams
    record_beams = list(
        getattr(record_dcm, "TreatmentSessionIonBeamSequence", None)
        or getattr(record_dcm, "TreatmentSessionBeamSequence", None)
        or []
    )

    # 3. Extract planned beams if plan dataset provided
    plan_beams_map: dict[str, Any] = {}
    if plan_dcm is not None:
        p_seq = list(
            getattr(plan_dcm, "IonBeamSequence", None)
            or getattr(plan_dcm, "BeamSequence", None)
            or []
        )
        for p_b in p_seq:
            b_name = str(getattr(p_b, "BeamName", "") or "")
            if b_name:
                plan_beams_map[_normalize_beam_name(b_name)] = p_b

    # 4. Per-beam inspection
    beam_details: list[dict[str, Any]] = []
    total_prescribed_mu = 0.0
    total_delivered_mu = 0.0
    missing_beams: list[str] = []

    seen_plan_keys: set[str] = set()

    for idx, r_b in enumerate(record_beams):
        r_name = str(getattr(r_b, "BeamName", f"Beam_{idx + 1}") or f"Beam_{idx + 1}")
        norm_key = _normalize_beam_name(r_name)
        seen_plan_keys.add(norm_key)

        b_term_status = str(
            getattr(r_b, "BeamTerminationStatus", "") or ""
        ).strip().upper()
        b_term_code = str(getattr(r_b, "BeamTerminationCode", "") or "")
        b_deliv_type = str(getattr(r_b, "TreatmentDeliveryType", "") or "").strip().upper()

        # Meterset check
        spec_mu = float(
            getattr(r_b, "SpecifiedPrimaryMeterset", None)
            or getattr(r_b, "SpecifiedMeterset", None)
            or 0.0
        )
        del_mu = float(
            getattr(r_b, "DeliveredPrimaryMeterset", None)
            or getattr(r_b, "DeliveredMeterset", None)
            or 0.0
        )

        # Fall back to plan beam if specified MU not in record
        p_b = plan_beams_map.get(norm_key)
        if spec_mu <= 0.0 and p_b is not None:
            spec_mu = float(getattr(p_b, "FinalCumulativeMetersetWeight", 0.0) or 0.0)

        total_prescribed_mu += spec_mu
        total_delivered_mu += del_mu

        b_interrupted = False
        b_reasons: list[str] = []

        if b_term_status and b_term_status not in NORMAL_TERMINATION_STATUSES:
            b_interrupted = True
            b_reasons.append(f"Termination status: {b_term_status}")
            reasons.append(f"Beam '{r_name}' termination status is {b_term_status}")
            interruption_types.append("beam_termination_status")

        if "CONTINUATION" in b_deliv_type:
            b_reasons.append("Marked as CONTINUATION beam")

        # Zero MU on non-zero prescribed
        if spec_mu > 0.0 and del_mu <= 1e-6:
            b_interrupted = True
            b_reasons.append("Zero MU delivered")
            reasons.append(f"Beam '{r_name}' delivered 0.0 MU (prescribed: {spec_mu:.1f} MU)")
            interruption_types.append("zero_delivery")
        # Severe meterset shortfall: delivered < 98% of prescribed (and not zero)
        elif spec_mu > 1.0 and del_mu < (spec_mu * 0.98):
            pct = (del_mu / spec_mu) * 100.0
            b_interrupted = True
            b_reasons.append(f"Meterset shortfall: {del_mu:.1f} / {spec_mu:.1f} MU ({pct:.1f}%)")
            reasons.append(
                f"Beam '{r_name}' delivered only {del_mu:.1f} of {spec_mu:.1f} prescribed MU ({pct:.1f}%)"
            )
            interruption_types.append("meterset_shortfall")

        # Spot count comparison if control points are present
        cps = getattr(r_b, "IonControlPointDeliverySequence", None) or []
        del_spots = 0
        for cp in cps:
            ms = getattr(cp, "ScanSpotMetersetsDelivered", None)
            if ms is not None:
                del_spots += len(ms) if hasattr(ms, "__len__") else 1

        rx_spots = 0
        if p_b is not None:
            p_cps = getattr(p_b, "IonControlPointSequence", None) or []
            for p_cp in p_cps:
                p_ms = getattr(p_cp, "ScanSpotMetersetWeights", None)
                if p_ms is not None:
                    rx_spots += len(p_ms) if hasattr(p_ms, "__len__") else 1

        if rx_spots > 10 and del_spots > 0 and del_spots < int(rx_spots * 0.95):
            pct_spots = (del_spots / rx_spots) * 100.0
            b_interrupted = True
            b_reasons.append(f"Spot shortfall: {del_spots} / {rx_spots} spots ({pct_spots:.1f}%)")
            reasons.append(
                f"Beam '{r_name}' delivered only {del_spots} of {rx_spots} spots ({pct_spots:.1f}%)"
            )
            interruption_types.append("spot_shortfall")

        beam_details.append({
            "beam_name": r_name,
            "normalized_name": norm_key,
            "termination_status": b_term_status or "NORMAL",
            "termination_code": b_term_code,
            "delivery_type": b_deliv_type or "TREATMENT",
            "prescribed_mu": round(spec_mu, 2),
            "delivered_mu": round(del_mu, 2),
            "mu_delivered_ratio_pct": round((del_mu / spec_mu * 100.0), 1) if spec_mu > 0 else 100.0,
            "delivered_spots": del_spots,
            "prescribed_spots": rx_spots,
            "is_interrupted": b_interrupted,
            "reasons": b_reasons,
        })

    # 5. Missing planned beams check
    if plan_beams_map:
        for p_norm_key, p_b in plan_beams_map.items():
            if p_norm_key not in seen_plan_keys:
                missing_p_name = str(getattr(p_b, "BeamName", p_norm_key))
                missing_beams.append(missing_p_name)

        if missing_beams:
            reasons.append(
                f"Partial delivery: missing planned beam(s) {', '.join(missing_beams)} "
                f"({len(record_beams)} of {len(plan_beams_map)} fields present)"
            )
            interruption_types.append("missing_beams")

    # 6. Overall session delivery zero check
    if total_delivered_mu <= 1e-6 and total_prescribed_mu > 0.0:
        if "zero_delivery" not in interruption_types:
            reasons.append("Overall delivery session carried 0.0 MU (aborted before beam-on)")
            interruption_types.append("zero_delivery")

    is_interrupted = len(reasons) > 0
    final_reason = "; ".join(reasons) if reasons else None
    primary_type = interruption_types[0] if interruption_types else None

    return {
        "is_interrupted": is_interrupted,
        "interruption_reason": final_reason,
        "interruption_type": primary_type,
        "all_reasons": reasons,
        "details": {
            "total_prescribed_mu": round(total_prescribed_mu, 2),
            "total_delivered_mu": round(total_delivered_mu, 2),
            "total_mu_ratio_pct": round((total_delivered_mu / total_prescribed_mu * 100.0), 1) if total_prescribed_mu > 0 else 100.0,
            "missing_beams": missing_beams,
            "planned_fields_count": len(plan_beams_map) if plan_beams_map else None,
            "delivered_fields_count": len(record_beams),
            "beams": beam_details,
        },
    }
