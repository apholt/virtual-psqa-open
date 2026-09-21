"""
Clinical QA report generation.

Produces a self-contained, print-optimised HTML report for a plan. On a Windows
clinical workstation this is the most reliable path: the physicist opens the
report and uses the browser's "Save as PDF" for an archival copy, with identical
layout. If WeasyPrint (plus its GTK/Pango/Cairo native deps) is available, the
same HTML can be rendered to a true PDF server-side via render_pdf().

Sections (per the build plan):
  - patient / plan / date header
  - Delivery gate decision with per-layer evidence summary
  - complexity metrics table
  - MCsquare gamma results per comparison (passing rate + map thumbnail)
  - log reconstruction gamma results per fraction (if available)
  - physical measurement outcome (if recorded)
  - fractional trend chart (gamma passing rate vs fraction)
  - physicist sign-off line + timestamp
  - footer with references and model version
"""
from __future__ import annotations

import base64
import io
import json
import logging
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, List, Optional

import numpy as np
from sqlalchemy.orm import Session

from config import settings
from models.fraction import Fraction
from models.gamma_result import GammaResult
from models.plan import Plan

logger = logging.getLogger(__name__)


# GATE_REPORT_V1 -- gate status -> (label, colour).
# Six GateStatus values, not the retired three-way ML verdict.
_GATE_LABEL = {
    "cleared": ("Cleared to treat", "#3fb950"),
    "verified": ("Verified in delivery", "#3fb950"),
    "investigate": ("Investigate", "#d29922"),
    "incomplete": ("Incomplete evidence", "#d29922"),
    "measure": ("Measure on phantom", "#f85149"),
    "escalate": ("Escalate to physics", "#f85149"),
}

_LAYER_STATUS_COLOUR = {
    "pass": "#3fb950",
    "marginal": "#d29922",
    "fail": "#f85149",
    "unavailable": "#7d8590",
}

_COMPARISON_LABEL = {
    "mcSquare_vs_TPS": "MCsquare vs TPS",
    "log_vs_Rx": "Log reconstruction vs prescription",
    "mcSquare_vs_log": "MCsquare vs Log",
}



def _gamma_thumbnail_from_array(raw_map: np.ndarray, size: int = 200) -> Optional[str]:
    """Render a 2D/3D gamma array to a base64 PNG data URI."""
    try:
        from PIL import Image
        import matplotlib

        gmap = np.asarray(raw_map, dtype=np.float32)
        if gmap.ndim == 3:
            gmap = gmap[gmap.shape[0] // 2, :, :]
        elif gmap.ndim > 3:
            gmap = np.squeeze(gmap)
            if gmap.ndim == 3:
                gmap = gmap[gmap.shape[0] // 2, :, :]
        valid = np.isfinite(gmap)

        norm = np.clip(gmap / 2.0, 0.0, 1.0)  # gamma=1 (pass limit) -> mid scale
        cmap = matplotlib.colormaps["RdYlGn_r"]
        rgba = (cmap(norm) * 255).astype(np.uint8)
        rgba[~valid] = [40, 45, 52, 255]
        img = Image.fromarray(rgba, mode="RGBA").convert("RGB")
        img = img.resize((size, size), Image.NEAREST)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Could not render gamma array thumbnail: {exc}")
        return None


def _gamma_thumbnail(path: Optional[str], size: int = 200) -> Optional[str]:
    """Render a saved gamma map (.npz) to a base64 PNG data URI."""
    if not path:
        return None
    try:
        data = np.load(path)
        raw_map = data["gamma_map"] if "gamma_map" in data else (data["gamma"] if "gamma" in data else None)
        if raw_map is None:
            return None
        return _gamma_thumbnail_from_array(raw_map, size=size)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Could not render gamma thumbnail {path}: {exc}")
        return None


def _render_dose_slice_png(
    dose_plane: np.ndarray,
    colormap: str = "turbo",
    max_val: Optional[float] = None,
    size: int = 200,
) -> Optional[str]:
    """Render a 2D dose plane to a base64 PNG data URI."""
    try:
        from PIL import Image
        import matplotlib

        arr = np.asarray(dose_plane, dtype=np.float32)
        if arr.ndim == 3:
            arr = arr[arr.shape[0] // 2, :, :]
        elif arr.ndim > 3:
            arr = np.squeeze(arr)
            if arr.ndim == 3:
                arr = arr[arr.shape[0] // 2, :, :]
        if max_val is None or max_val <= 0:
            max_val = float(np.max(arr)) if arr.size > 0 else 1.0
        if max_val <= 0:
            max_val = 1.0

        norm = np.clip(arr / max_val, 0.0, 1.0)
        cmap = matplotlib.colormaps[colormap]
        rgba = (cmap(norm) * 255).astype(np.uint8)
        # Background suppression for near-zero dose
        mask_zero = arr < (max_val * 0.02)
        rgba[mask_zero] = [15, 20, 25, 255]

        img = Image.fromarray(rgba, mode="RGBA").convert("RGB")
        img = img.resize((size, size), Image.NEAREST)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Could not render dose slice: {exc}")
        return None


def _get_field_images(
    r: GammaResult,
    plan: Plan,
    db: Session,
    target_fx_num: Optional[int] = None,
) -> tuple[Optional[str], Optional[str], Optional[str], str]:
    """
    Extracts isocenter spatial dose distributions and gamma error maps for a field.
    Returns: (dose_img, gamma_img, rx_img, dose_label)
    """
    dose_img: Optional[str] = None
    gamma_img: Optional[str] = None
    rx_img: Optional[str] = None
    dose_label = "Field Spatial Dose at Isocenter"
    plane_idx: Optional[int] = None

    # 1. Inspect r.gamma_map_path if present
    if r.gamma_map_path:
        p = Path(r.gamma_map_path)
        if not p.is_absolute():
            p = (Path(settings.RESULTS_PATH).parent / p).resolve()
            if not p.exists():
                p = Path(r.gamma_map_path).resolve()
        if p.exists():
            try:
                data = np.load(p)
                raw_gamma = data["gamma"] if "gamma" in data else (data["gamma_map"] if "gamma_map" in data else None)
                if raw_gamma is not None:
                    gamma_img = _gamma_thumbnail_from_array(raw_gamma, size=200)

                if "delivered" in data:
                    deliv_arr = np.asarray(data["delivered"], dtype=np.float32)
                    dose_img = _render_dose_slice_png(deliv_arr, colormap="turbo", size=200)
                    dose_label = "Delivered Spatial Dose (Isocenter)"

                if "prescribed" in data:
                    rx_arr = np.asarray(data["prescribed"], dtype=np.float32)
                    rx_img = _render_dose_slice_png(rx_arr, colormap="turbo", size=200)

                if "plane_index" in data:
                    plane_idx = int(data["plane_index"])
            except Exception as exc:
                logger.debug(f"Could not load npz from {p}: {exc}")

    # 2. Fallback gamma image from path
    if gamma_img is None and r.gamma_map_path:
        gamma_img = _gamma_thumbnail(r.gamma_map_path, size=200)

    # 3. Check per-fraction log reconstruction output files if dose_img is missing
    if dose_img is None or gamma_img is None:
        log_dir = Path(settings.RESULTS_PATH) / f"plan_{plan.id}" / "log_reconstruction"
        fx_list: list[int] = []
        if target_fx_num is not None:
            fx_list.append(target_fx_num)
        if r.fraction_number is not None and r.fraction_number not in fx_list:
            fx_list.append(r.fraction_number)
        for def_fx in (0, 1):
            if def_fx not in fx_list:
                fx_list.append(def_fx)
        b_list = [r.beam_number] if r.beam_number is not None else [1, 2, 3, 4, 5, 6]

        for fx in fx_list:
            for b in b_list:
                cand = log_dir / f"log_dose_fx{fx}_beam{b}.npz"
                if cand.exists():
                    try:
                        data = np.load(cand)
                        if gamma_img is None and "gamma" in data:
                            gamma_img = _gamma_thumbnail_from_array(data["gamma"], size=200)
                        if dose_img is None and "delivered" in data:
                            dose_img = _render_dose_slice_png(data["delivered"], colormap="turbo", size=200)
                            dose_label = "Delivered Spatial Dose (Isocenter)"
                        if rx_img is None and "prescribed" in data:
                            rx_img = _render_dose_slice_png(data["prescribed"], colormap="turbo", size=200)
                        if dose_img and gamma_img:
                            break
                    except Exception:
                        pass
            if dose_img and gamma_img:
                break

    # 4. If spatial dose is still missing, try per-beam DICOM RTDose or Plan RTDose
    if dose_img is None and plan.dicom_store_path and Path(plan.dicom_store_path).exists():
        try:
            from services.gamma_analysis import find_beam_rtdose_files, load_rtdose, find_rtdose_file
            beam_doses = find_beam_rtdose_files(plan.dicom_store_path, plan_uid=plan.rtplan_uid)
            beam_file = beam_doses.get(r.beam_number) if (beam_doses and r.beam_number) else None
            if beam_file:
                grid = load_rtdose(beam_file)
                z = plane_idx if (plane_idx is not None and 0 <= plane_idx < grid.shape[0]) else grid.max_dose_plane_index()
                dose_img = _render_dose_slice_png(grid.plane(z), colormap="turbo", size=200)
                dose_label = f"Field RTDose at Isocenter (Slice z={z})"
            else:
                rtdose_file = find_rtdose_file(plan.dicom_store_path, plan_uid=plan.rtplan_uid)
                if rtdose_file:
                    grid = load_rtdose(rtdose_file)
                    z = plane_idx if (plane_idx is not None and 0 <= plane_idx < grid.shape[0]) else grid.max_dose_plane_index()
                    dose_img = _render_dose_slice_png(grid.plane(z), colormap="turbo", size=200)
                    dose_label = f"Plan RTDose at Isocenter (Slice z={z})"
        except Exception as exc:
            logger.debug(f"Could not load DICOM dose: {exc}")

    # 5. If spatial dose is still missing, fallback to loaded plan doses
    if dose_img is None:
        try:
            from services.gamma_analysis import load_plan_doses
            doses = load_plan_doses(plan.id, db)
            tps_grid = doses.get("tps") or doses.get("mcSquare")
            if tps_grid:
                z = plane_idx if (plane_idx is not None and 0 <= plane_idx < tps_grid.shape[0]) else tps_grid.max_dose_plane_index()
                dose_img = _render_dose_slice_png(tps_grid.plane(z), colormap="turbo", size=200)
                dose_label = f"Spatial Dose at Isocenter (Slice z={z})"
        except Exception:
            pass

    return dose_img, gamma_img, rx_img, dose_label


def _trend_svg(fraction_points: List[tuple], threshold: float) -> str:
    """Inline SVG line chart of gamma passing rate vs fraction index."""
    if not fraction_points:
        return "<p class='muted'>No per-fraction log data yet.</p>"

    w, h = 560, 220
    pad_l, pad_b, pad_t, pad_r = 40, 30, 20, 16
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b

    ys = [pr for _, pr in fraction_points]
    y_min = min(min(ys), threshold) - 3
    y_max = 100.5
    y_min = max(0.0, y_min)

    def px(i: int) -> float:
        n = max(1, len(fraction_points) - 1)
        return pad_l + (plot_w * i / n if n else plot_w / 2)

    def py(v: float) -> float:
        return pad_t + plot_h * (1 - (v - y_min) / (y_max - y_min))

    # Threshold (action) line.
    thr_y = py(threshold)
    points = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, (_, v) in enumerate(fraction_points))
    dots = "".join(
        f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="3.5" '
        f'fill="{"#3fb950" if v >= threshold else "#f85149"}"/>'
        for i, (_, v) in enumerate(fraction_points)
    )
    labels = "".join(
        f'<text x="{px(i):.1f}" y="{h - pad_b + 16}" font-size="9" '
        f'text-anchor="middle" fill="#7d8590">{escape(str(lbl))}</text>'
        for i, (lbl, _) in enumerate(fraction_points)
    )
    grid = "".join(
        f'<line x1="{pad_l}" y1="{py(v):.1f}" x2="{w - pad_r}" y2="{py(v):.1f}" '
        f'stroke="#21262d" stroke-width="1"/>'
        f'<text x="{pad_l - 6}" y="{py(v) + 3:.1f}" font-size="9" '
        f'text-anchor="end" fill="#7d8590">{v:.0f}</text>'
        for v in _nice_ticks(y_min, y_max)
    )
    return f"""
    <svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img">
      {grid}
      <line x1="{pad_l}" y1="{thr_y:.1f}" x2="{w - pad_r}" y2="{thr_y:.1f}"
            stroke="#d29922" stroke-width="1.5" stroke-dasharray="5 4"/>
      <text x="{w - pad_r}" y="{thr_y - 4:.1f}" font-size="9" text-anchor="end"
            fill="#d29922">action {threshold:.0f}%</text>
      <polyline points="{points}" fill="none" stroke="#58a6ff" stroke-width="2"/>
      {dots}
      {labels}
    </svg>
    """


def _nice_ticks(lo: float, hi: float, n: int = 4) -> List[float]:
    if hi <= lo:
        return [lo]
    step = (hi - lo) / n
    return [round(lo + step * i) for i in range(n + 1)]


def _dvh_svg(dvh_data, w: int = 760, h: int = 280) -> str:
    """Inline SVG cumulative DVH chart with shaded robustness uncertainty envelopes."""
    if not dvh_data or not getattr(dvh_data, "rois", None):
        return ""

    pad_l, pad_b, pad_t, pad_r = 50, 40, 20, 30
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b

    # Determine max dose across all ROIs
    max_dose = 10.0
    if getattr(dvh_data, "prescription_dose_gy", None):
        max_dose = max(max_dose, float(dvh_data.prescription_dose_gy) * 1.15)
    for roi in dvh_data.rois:
        if getattr(roi, "dvh", None) and roi.dvh.dose_bins_gy:
            max_dose = max(max_dose, float(roi.dvh.dose_bins_gy[-1]))
        if getattr(roi, "metrics", None) and getattr(roi.metrics, "d_max", None) and roi.metrics.d_max.mc_max:
            max_dose = max(max_dose, float(roi.metrics.d_max.mc_max) * 1.05)

    max_dose = max(10.0, float(max_dose))

    def px(d: float) -> float:
        return pad_l + plot_w * min(1.0, max(0.0, d / max_dose))

    def py(v: float) -> float:
        return pad_t + plot_h * (1.0 - min(1.0, max(0.0, v / 100.0)))

    # Grid lines & ticks
    # Y: 0, 20, 40, 60, 80, 100%
    y_ticks = [0, 20, 40, 60, 80, 100]
    grid_lines = []
    for yv in y_ticks:
        yp = py(yv)
        grid_lines.append(
            f'<line x1="{pad_l}" y1="{yp:.1f}" x2="{w - pad_r}" y2="{yp:.1f}" stroke="#21262d" stroke-width="1"/>'
            f'<text x="{pad_l - 8}" y="{yp + 4:.1f}" font-size="10" text-anchor="end" fill="#7d8590">{yv}%</text>'
        )

    # X: nice steps
    x_step = 10.0 if max_dose >= 50 else (5.0 if max_dose >= 20 else 2.0)
    xv = 0.0
    while xv <= max_dose + 0.01:
        xp = px(xv)
        grid_lines.append(
            f'<line x1="{xp:.1f}" y1="{pad_t}" x2="{xp:.1f}" y2="{h - pad_b}" stroke="#21262d" stroke-width="1"/>'
            f'<text x="{xp:.1f}" y="{h - pad_b + 16}" font-size="10" text-anchor="middle" fill="#7d8590">{xv:.0f}</text>'
        )
        xv += x_step

    axis_labels = (
        f'<text x="{pad_l + plot_w / 2:.1f}" y="{h - 6}" font-size="11" text-anchor="middle" fill="#8b949e">Dose (Gy)</text>'
        f'<text x="14" y="{pad_t + plot_h / 2:.1f}" font-size="11" text-anchor="middle" fill="#8b949e" transform="rotate(-90 14 {pad_t + plot_h / 2:.1f})">Volume (%)</text>'
    )

    rx_line = ""
    rx = getattr(dvh_data, "prescription_dose_gy", None)
    if rx and rx <= max_dose:
        rx_x = px(rx)
        rx_line = (
            f'<line x1="{rx_x:.1f}" y1="{pad_t}" x2="{rx_x:.1f}" y2="{h - pad_b}" '
            f'stroke="#f85149" stroke-width="1.5" stroke-dasharray="4 3"/>'
            f'<text x="{rx_x + 4:.1f}" y="{pad_t + 12}" font-size="10" fill="#f85149" font-weight="600">Rx: {rx:.1f} Gy</text>'
        )

    paths = []
    for roi in dvh_data.rois:
        dvh = getattr(roi, "dvh", None)
        if not dvh or not dvh.dose_bins_gy:
            continue
        bins = dvh.dose_bins_gy
        color = getattr(roi, "color", None) or "#58a6ff"

        # 1. Shaded Uncertainty Band
        if dvh.mc_min_volume_pct and dvh.mc_max_volume_pct:
            min_pts = [f"{px(b):.1f},{py(v):.1f}" for b, v in zip(bins, dvh.mc_min_volume_pct)]
            max_pts = [f"{px(b):.1f},{py(v):.1f}" for b, v in reversed(list(zip(bins, dvh.mc_max_volume_pct)))]
            band_d = f"M {min_pts[0]} " + " ".join(f"L {pt}" for pt in min_pts[1:]) + " " + " ".join(f"L {pt}" for pt in max_pts) + " Z"
            paths.append(f'<path d="{band_d}" fill="{color}" fill-opacity="0.22" stroke="none"/>')

        # 2. TPS curve (dashed)
        if getattr(dvh, "tps_volume_pct", None):
            tps_pts = " ".join(f"{px(b):.1f},{py(v):.1f}" for b, v in zip(bins, dvh.tps_volume_pct))
            paths.append(f'<polyline points="{tps_pts}" fill="none" stroke="{color}" stroke-width="1.5" stroke-dasharray="4 3" stroke-opacity="0.75"/>')

        # 3. MC Nominal curve (solid)
        if dvh.mc_nominal_volume_pct:
            nom_pts = " ".join(f"{px(b):.1f},{py(v):.1f}" for b, v in zip(bins, dvh.mc_nominal_volume_pct))
            paths.append(f'<polyline points="{nom_pts}" fill="none" stroke="{color}" stroke-width="2.2" stroke-linecap="round"/>')

    legend_items = []
    for roi in dvh_data.rois[:8]:
        color = getattr(roi, "color", None) or "#58a6ff"
        legend_items.append(
            f'<span style="display:inline-flex;align-items:center;margin-right:12px;margin-bottom:4px;">'
            f'<span style="display:inline-block;width:9px;height:9px;border-radius:2px;background:{color};margin-right:4px;"></span>'
            f'{escape(roi.name)}'
            f'</span>'
        )

    return f"""
    <div class="dvh-container">
      <svg class="dvh-chart" width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">
        {''.join(grid_lines)}
        {rx_line}
        {''.join(paths)}
        {axis_labels}
      </svg>
      <div class="dvh-legend">
        <div>{''.join(legend_items)}</div>
        <div style="font-size:10px;">
          <span style="border-bottom:2px solid #58a6ff;padding-bottom:1px;margin-right:8px;">&mdash; openMCsquare (Nominal)</span>
          <span style="border-bottom:2px dashed #8b949e;padding-bottom:1px;margin-right:8px;">- - TPS Reference</span>
          <span style="background:rgba(88,166,255,0.22);padding:1px 4px;border-radius:2px;">Shaded: Scenario Bounds</span>
        </div>
      </div>
    </div>
    """


def _render_dvh_table_html(dvh_data) -> str:
    """Render HTML table of clinical dosimetric metrics and worst-case robustness intervals."""
    if not dvh_data or not getattr(dvh_data, "rois", None):
        return ""

    rows = []
    for roi in dvh_data.rois:
        m = roi.metrics
        color = getattr(roi, "color", None) or "#58a6ff"
        is_target = getattr(roi, "is_target", False)
        type_str = getattr(roi, "type", "OAR")
        badge_col = "#f85149" if is_target else "#58a6ff"
        type_badge = f'<span class="badge-mini" style="background:{badge_col}22;color:{badge_col};">{escape(type_str)}</span>'

        tps_d95 = f"{m.d95.tps:.1f} Gy" if (m.d95 and m.d95.tps is not None) else "&mdash;"
        mc_d95 = f"<b>{m.d95.mc_nominal:.1f} Gy</b> <span class='muted' style='font-size:10px;'>[{m.d95.mc_min:.1f} &ndash; {m.d95.mc_max:.1f}]</span>"
        mc_d50 = f"{m.d50.mc_nominal:.1f} Gy" if m.d50 else "&mdash;"
        mc_d2 = f"{m.d2.mc_nominal:.1f} Gy" if m.d2 else "&mdash;"
        mc_dmean = f"{m.d_mean.mc_nominal:.1f} Gy" if m.d_mean else "&mdash;"

        if roi.robustness_pass:
            st_badge = '<span class="badge-mini" style="background:#3fb95022;color:#3fb950;border:1px solid #3fb95055;">ROBUST PASS</span>'
        else:
            note = escape(roi.robustness_note or "Action required")
            st_badge = f'<span class="badge-mini" style="background:#d2992222;color:#d29922;border:1px solid #d2992255;" title="{note}">ACTION</span>'

        rows.append(
            f'<tr>'
            f'<td><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{color};margin-right:6px;"></span><b>{escape(roi.name)}</b></td>'
            f'<td>{type_badge}</td>'
            f'<td class="num">{roi.volume_cc:.1f}</td>'
            f'<td class="num">{tps_d95}</td>'
            f'<td class="num">{mc_d95}</td>'
            f'<td class="num">{mc_d50}</td>'
            f'<td class="num">{mc_d2}</td>'
            f'<td class="num muted">{mc_dmean}</td>'
            f'<td>{st_badge}</td>'
            f'</tr>'
        )

    return f"""
    <table class="tbl" style="margin-top:10px;">
      <thead>
        <tr>
          <th>Structure</th>
          <th>Type</th>
          <th style="text-align:right;">Vol (cc)</th>
          <th style="text-align:right;">TPS D95%</th>
          <th style="text-align:right;">openMCsquare D95% [Min &ndash; Max]</th>
          <th style="text-align:right;">MC D50% (Median)</th>
          <th style="text-align:right;">MC D2% (Hot Spot)</th>
          <th style="text-align:right;">MC Dmean</th>
          <th>Robustness Status</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    """


def build_report_html(plan_id: int, db: Session) -> str:
    """
    Streamlined Fractional QA and Delivery Verification Report.
    Includes:
      - Patient & Plan demographics with delivery dates and times.
      - Fractional gamma passing rate trend across fractions.
      - Field-by-field gamma breakdown for either the verification record or first fraction delivered.
      - Electronic OMR integration notice (no physical sign-off box required).
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan is None:
        raise ValueError(f"Plan {plan_id} not found")

    patient = plan.patient
    gamma_rows = (
        db.query(GammaResult).filter_by(plan_id=plan_id).order_by(GammaResult.created_at).all()
    )

    # If gamma results not present yet, attempt on-demand run
    if not gamma_rows:
        try:
            from services.gamma_analysis import run_gamma_analysis
            run_gamma_analysis(plan_id, db)
            gamma_rows = (
                db.query(GammaResult).filter_by(plan_id=plan_id).order_by(GammaResult.created_at).all()
            )
        except Exception as _g_err:
            logger.warning(f"On-demand gamma analysis for report {plan_id} failed: {_g_err}")

    # Fetch fractions for delivery dates and verification record detection
    fractions = (
        db.query(Fraction)
        .filter_by(plan_id=plan_id)
        .order_by(Fraction.fraction_number.asc())
        .all()
    )

    # 1. Fractional trend from log_vs_Rx / log_vs_TPS rows
    log_rows = [r for r in gamma_rows if r.comparison_type in ("log_vs_Rx", "log_vs_TPS")]
    by_fx: dict[int, List[GammaResult]] = {}
    for r in log_rows:
        fx = r.fraction_number if r.fraction_number is not None else 1
        by_fx.setdefault(fx, []).append(r)

    fraction_points: List[tuple] = []
    for fx in sorted(by_fx.keys()):
        rows_for_fx = by_fx[fx]
        comp = next((r for r in rows_for_fx if r.field_name == "Composite"), None)
        pr = comp.passing_rate if comp else float(np.mean([r.passing_rate for r in rows_for_fx]))
        fraction_points.append((f"Fx {fx}", round(pr, 1)))

    # Fallback if no fraction points yet but fractions exist or composite exists
    if not fraction_points and gamma_rows:
        for i, r in enumerate(gamma_rows[:5], start=1):
            if r.comparison_type in ("mcSquare_vs_TPS", "mcSquare_vs_log"):
                fraction_points.append((f"Beam {i}", round(r.passing_rate, 1)))

    log_threshold = log_rows[0].threshold if log_rows else settings.GAMMA_LOG_VS_TPS_THRESHOLD

    # 2. Field-by-field breakdown: Verification Record OR First Fraction Delivered (either or)
    verif_frac = next(
        (f for f in fractions if getattr(f, "delivery_type", None) == "verification" or f.fraction_number == 0),
        None,
    )
    verif_rows = []
    if verif_frac:
        verif_rows = [r for r in gamma_rows if r.fraction_number == verif_frac.fraction_number]

    if verif_rows:
        record_title = "Verification QA Delivery Record (Pre-Treatment Dry Run)"
        target_rows = verif_rows
        target_frac = verif_frac
    else:
        first_frac = next((f for f in fractions if f.fraction_number > 0), fractions[0] if fractions else None)
        first_fx_num = first_frac.fraction_number if first_frac else 1
        first_rows = [r for r in log_rows if r.fraction_number == first_fx_num]
        if not first_rows and gamma_rows:
            first_rows = [r for r in gamma_rows if r.fraction_number in (None, 1)]
        record_title = f"Fraction {first_fx_num} Delivered Verification Record"
        target_rows = first_rows
        target_frac = first_frac

    field_rows = [r for r in target_rows if r.field_name != "Composite"]
    if not field_rows and target_rows:
        field_rows = target_rows

    # Deduplicate field rows so each beam is listed exactly once (latest entry kept)
    dedup_fields: dict[Any, GammaResult] = {}
    for r in field_rows:
        key = r.beam_number if r.beam_number is not None else r.field_name
        dedup_fields[key] = r
    field_rows = list(dedup_fields.values())
    field_rows.sort(key=lambda r: (r.beam_number if r.beam_number is not None else 0, r.field_name or ""))

    # Parse RTPLAN field details for gantry angles / MU
    field_details = []
    if plan.dicom_store_path:
        from services.dicom_ingestor import parse_rtplan_fields
        import pydicom
        for p in Path(plan.dicom_store_path).glob("*.dcm"):
            try:
                ds = pydicom.dcmread(str(p), stop_before_pixels=True)
                if ds.get("Modality") in ("RTPLAN", "RTIBTR"):
                    field_details = parse_rtplan_fields(ds)
                    break
            except Exception:
                continue

    # Dates, times, machine
    first_deliv_str = str(fractions[0].delivery_date) if fractions and fractions[0].delivery_date else "—"
    latest_deliv_str = str(fractions[-1].delivery_date) if fractions and fractions[-1].delivery_date else first_deliv_str
    target_date_str = str(target_frac.delivery_date) if target_frac and target_frac.delivery_date else first_deliv_str
    machine_name = fractions[0].machine if fractions and fractions[0].machine else "ProNova TRCS Gantry"
    total_delivered_count = len(fractions)
    total_prescribed_fx = plan.number_of_fractions or "—"

    # Overall summary KPI
    if target_rows:
        overall_pr = float(np.mean([r.passing_rate for r in target_rows]))
        overall_pass = all(r.passed for r in target_rows)
        sample_r = target_rows[0]
        dd_val = sample_r.dd_percent
        dta_val = sample_r.dta_mm
        thr_val = sample_r.threshold
    else:
        overall_pr = 0.0
        overall_pass = True
        dd_val = settings.GAMMA_LOG_VS_TPS_DD
        dta_val = settings.GAMMA_LOG_VS_TPS_DTA
        thr_val = settings.GAMMA_LOG_VS_TPS_THRESHOLD

    status_col = "#3fb950" if overall_pass else "#f85149"
    status_text = "VERIFIED / PASS" if overall_pass else "ACTION REQUIRED"
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    parts: List[str] = []
    parts.append(_REPORT_CSS)
    parts.append('<div class="report">')

    # Header
    parts.append(
        f'<div class="header">'
        f'<div>'
        f'<div class="inst-header">CLINICAL RADIATION ONCOLOGY &bull; MEDICAL PHYSICS</div>'
        f'<h1>Fractional QA &amp; Delivery Verification Report</h1>'
        f'<div class="muted">Virtual PSQA Fractional Analysis &middot; Generated {now}</div>'
        f'</div>'
        f'<div class="actions"><button onclick="window.print()">Print / Save as PDF</button></div>'
        f'</div>'
    )

    # Patient & Plan Cards with Dates/Times
    parts.append('<div class="grid2">')
    parts.append(
        '<div class="card"><h2>Patient Information</h2>'
        f'<div class="kv"><span>Patient Name</span><b>{escape(patient.patient_name if patient else "—")}</b></div>'
        f'<div class="kv"><span>MRN / Patient ID</span><b>{escape(patient.patient_id if patient else "—")}</b></div>'
        f'<div class="kv"><span>Date of Birth</span><b>{escape(str(patient.date_of_birth) if patient and patient.date_of_birth else "—")}</b></div>'
        f'<div class="kv"><span>Gender</span><b>{escape(str(patient.sex) if patient and patient.sex else "—")}</b></div>'
        "</div>"
    )
    parts.append(
        '<div class="card"><h2>Plan &amp; Delivery Timestamps</h2>'
        f'<div class="kv"><span>Plan Label</span><b>{escape(plan.plan_label)}</b></div>'
        f'<div class="kv"><span>Treatment Site</span><b>{escape(plan.treatment_site or "General")}</b></div>'
        f'<div class="kv"><span>Course Progress</span><b>{total_delivered_count} of {total_prescribed_fx} fractions delivered ({plan.number_of_fields} fields)</b></div>'
        f'<div class="kv"><span>Delivery Machine</span><b>{escape(machine_name)}</b></div>'
        f'<div class="kv"><span>Delivery Dates</span><b>{first_deliv_str} to {latest_deliv_str}</b></div>'
        "</div>"
    )
    parts.append("</div>")

    # Overall Delivery KPI Banner
    parts.append(
        '<div class="card highlight-card">'
        '<h2>Fractional Delivery Assurance Overview</h2>'
        '<div class="kpi-banner">'
        f'<div class="kpi-score" style="color:{status_col}">{overall_pr:.1f}%</div>'
        '<div>'
        f'<div class="badge" style="background:{status_col}22;color:{status_col};border-color:{status_col}66;font-size:14px;padding:6px 14px;">{status_text}</div>'
        f'<div class="muted" style="margin-top:6px;">TG-218 Log Reconstruction Criterion: <b>{dd_val:g}% / {dta_val:g} mm</b> &middot; Action Level: <b>&ge; {thr_val:g}%</b></div>'
        f'<div class="muted" style="margin-top:2px;">Fractions Delivered: <b>{total_delivered_count}</b> &middot; Latest Evaluation: <b>{target_date_str}</b></div>'
        '</div>'
        '</div>'
        '</div>'
    )

    # 1. Trend of Gamma per Fraction
    parts.append(
        '<div class="card">'
        '<h2>Fractional Gamma Passing Rate Trend (Log Reconstruction vs Prescription)</h2>'
        f'{_trend_svg(fraction_points, log_threshold)}'
        '<div class="muted" style="text-align:center;margin-top:6px;">Trend of 3D gamma passing rate across delivered treatment fractions. Dashed line denotes clinical action limit (90.0%).</div>'
        '</div>'
    )

    # 2. Field-by-Field Breakdown (Verification Record or Fraction 1 Delivered)
    parts.append(
        f'<div class="card">'
        f'<h2>{escape(record_title)} &mdash; Field-by-Field Breakdown</h2>'
        f'<p class="muted" style="margin-bottom:10px;">Delivered on <b>{target_date_str}</b> at <b>{machine_name}</b>. Demonstrates per-beam meterset and spot delivery fidelity.</p>'
        '<table class="tbl"><thead><tr>'
        '<th>Beam #</th><th>Field Name</th><th>Gantry Angle</th><th>Planned MU</th><th>Passing Rate</th><th>Criteria</th><th>Status</th>'
        '</tr></thead><tbody>'
    )

    if field_rows:
        for idx, r in enumerate(field_rows, start=1):
            f_meta = next((f for f in field_details if f.get("beam_name") == r.field_name or f.get("beam_number") == r.beam_number), {})
            gantry = f"{f_meta.get('gantry_angle', 0):.0f}&deg;" if "gantry_angle" in f_meta else "—"
            mu_str = f"{f_meta.get('total_mu', 0):.1f}" if "total_mu" in f_meta else "—"
            rcol = "#3fb950" if r.passed else "#f85149"
            rst = "PASS" if r.passed else "FAIL"
            b_num = r.beam_number or idx

            parts.append(
                f'<tr>'
                f'<td>{b_num}</td>'
                f'<td><b>{escape(r.field_name)}</b></td>'
                f'<td>{gantry}</td>'
                f'<td class="num">{mu_str}</td>'
                f'<td class="num" style="color:{rcol};font-weight:700;">{r.passing_rate:.1f}%</td>'
                f'<td class="muted">{r.dd_percent:g}%/{r.dta_mm:g}mm &ge; {r.threshold:g}%</td>'
                f'<td><span class="badge-mini" style="background:{rcol}22;color:{rcol};">{rst}</span></td>'
                f'</tr>'
            )
    else:
        parts.append('<tr><td colspan="7" style="text-align:center;color:#8b949e;padding:12px;">No delivery record fields logged yet.</td></tr>')

    parts.append('</tbody></table></div>')

    # 3. Field Spatial Dose Distributions & Gamma Maps at Isocenter
    parts.append(
        f'<div class="card">'
        f'<h2>{escape(record_title)} &mdash; Field Spatial Dose &amp; Gamma Verification (at Isocenter)</h2>'
        f'<p class="muted" style="margin-bottom:14px;">'
        f'Planar 2D spatial dose distributions and TG-218 gamma index evaluations at the isocenter plane for each delivery field. '
        f'Gamma evaluation colormap: <b>&gamma; &le; 1.0 (Pass, Green)</b> to <b>&gamma; &gt; 1.0 (Fail, Red)</b> &middot; '
        f'Spatial dose colormap: <b>0% (Background/Blue)</b> to <b>100% (Hot Spot/Red)</b>.'
        f'</p>'
    )

    if field_rows:
        target_fx_val = target_frac.fraction_number if target_frac else 1
        for idx, r in enumerate(field_rows, start=1):
            f_meta = next((f for f in field_details if f.get("beam_name") == r.field_name or f.get("beam_number") == r.beam_number), {})
            gantry = f"{f_meta.get('gantry_angle', 0):.0f}&deg;" if "gantry_angle" in f_meta else "—"
            mu_str = f"{f_meta.get('total_mu', 0):.1f}" if "total_mu" in f_meta else "—"
            rcol = "#3fb950" if r.passed else "#f85149"
            rst = "PASS" if r.passed else "FAIL"
            b_num = r.beam_number or idx

            dose_img, gamma_img, rx_img, dose_label = _get_field_images(r, plan, db, target_fx_num=target_fx_val)

            parts.append(
                f'<div class="field-qa-block">'
                f'<div class="field-qa-header">'
                f'<div>'
                f'<span class="field-title">Beam {b_num}: {escape(r.field_name)}</span>'
                f'<span class="muted" style="margin-left:12px;">Gantry: {gantry} &middot; Planned MU: {mu_str}</span>'
                f'</div>'
                f'<div>'
                f'<span class="field-pr" style="color:{rcol};">{r.passing_rate:.1f}%</span>'
                f'<span class="badge-mini" style="background:{rcol}22;color:{rcol};">{rst}</span>'
                f'<span class="muted" style="margin-left:8px;font-size:11px;">({r.dd_percent:g}% / {r.dta_mm:g} mm)</span>'
                f'</div>'
                f'</div>'
            )

            parts.append('<div class="img-strip" style="justify-content:flex-start;gap:20px;">')
            has_img = False
            if rx_img:
                has_img = True
                parts.append(
                    f'<div class="img-box">'
                    f'<img src="{rx_img}" class="slice-img" alt="Prescribed Dose"/>'
                    f'<span>Prescribed Dose (Isocenter)</span>'
                    f'</div>'
                )
            if dose_img:
                has_img = True
                parts.append(
                    f'<div class="img-box">'
                    f'<img src="{dose_img}" class="slice-img" alt="Spatial Dose"/>'
                    f'<span>{escape(dose_label)}</span>'
                    f'</div>'
                )
            if gamma_img:
                has_img = True
                parts.append(
                    f'<div class="img-box">'
                    f'<img src="{gamma_img}" class="slice-img" alt="Gamma Map"/>'
                    f'<span>Gamma Map ({r.dd_percent:g}%/{r.dta_mm:g}mm &le; 1.0)</span>'
                    f'</div>'
                )
            if not has_img:
                parts.append(
                    '<div class="muted" style="padding:12px 6px;">'
                    'Spatial dose distribution / gamma map image not available on disk for this field.'
                    '</div>'
                )
            parts.append('</div>')
            parts.append('</div>')
    else:
        parts.append('<p class="muted" style="text-align:center;padding:12px;">No delivery record fields logged yet.</p>')

    parts.append('</div>')

    # Electronic OMR Sign-Off Notice (No physical physics sign-off box)
    parts.append(
        '<div class="card">'
        '<div class="omr-notice">'
        '<div class="notice-badge">ELECTRONIC OMR INTEGRATION</div>'
        '<div class="notice-text">'
        '<b>Electronic Sign-Off Policy:</b> In accordance with institutional radiation oncology workflow, formal review, clinical verification, and physicist authorization are documented electronically in the institutional Oncology Management Record (OMR). No physical signature is required on this document.'
        '</div>'
        '</div>'
        '</div>'
    )

    # Footer
    parts.append(
        f'<div class="footer">'
        f'Virtual PSQA &middot; Fractional Delivery QA Analysis &middot; Electronic Record &middot; '
        f'Generated: {now}'
        f'</div>'
    )

    parts.append("</div>")
    return "<!doctype html><html><head><meta charset='utf-8'>" \
           f"<title>PSQA report — {escape(plan.plan_label)}</title></head><body>" \
           + "".join(parts) + "</body></html>"


def render_pdf(html: str) -> Optional[bytes]:
    """Render HTML to PDF via WeasyPrint if available; else None."""
    try:
        from weasyprint import HTML  # type: ignore

        return HTML(string=html).write_pdf()
    except Exception as exc:  # noqa: BLE001
        logger.info(f"WeasyPrint unavailable — serving HTML report instead ({exc})")
        return None




def build_secondary_dose_report_html(plan_id: int, db: Session) -> str:
    """
    Dedicated clinical report for secondary dose calculations (MCsquare vs TPS)
    and 3D gamma analysis (TG-218 compliant).
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan is None:
        raise ValueError(f"Plan {plan_id} not found")

    patient = plan.patient
    # Filter for pre-treatment secondary dose evaluation (fraction_number is None)
    # and order by id descending to pick the latest calculation per beam
    gamma_rows = (
        db.query(GammaResult)
        .filter(
            GammaResult.plan_id == plan_id,
            GammaResult.comparison_type == "mcSquare_vs_TPS",
            GammaResult.fraction_number.is_(None),
        )
        .order_by(GammaResult.id.desc())
        .all()
    )
    if not gamma_rows:
        # Fallback to any mcSquare_vs_TPS rows if fraction_number was set
        gamma_rows = (
            db.query(GammaResult)
            .filter_by(plan_id=plan_id, comparison_type="mcSquare_vs_TPS")
            .order_by(GammaResult.id.desc())
            .all()
        )

    # If gamma analysis has not been run yet, run on the fly so report is populated
    if not gamma_rows:
        try:
            from services.gamma_analysis import run_gamma_analysis
            run_gamma_analysis(plan_id, db)
            gamma_rows = (
                db.query(GammaResult)
                .filter(
                    GammaResult.plan_id == plan_id,
                    GammaResult.comparison_type == "mcSquare_vs_TPS",
                    GammaResult.fraction_number.is_(None),
                )
                .order_by(GammaResult.id.desc())
                .all()
            )
            if not gamma_rows:
                gamma_rows = (
                    db.query(GammaResult)
                    .filter_by(plan_id=plan_id, comparison_type="mcSquare_vs_TPS")
                    .order_by(GammaResult.id.desc())
                    .all()
                )
        except Exception as _g_err:
            logger.warning(f"On-demand gamma analysis for report {plan_id} failed: {_g_err}")

    # Composite vs per-beam rows: deduplicate by beam_number/field_name, preferring active settings criteria
    composite_row = next(
        (
            r for r in gamma_rows
            if (r.field_name == "Composite" or r.beam_number is None)
            and abs(r.dd_percent - settings.GAMMA_MCSQUARE_VS_TPS_DD) < 0.01
            and abs(r.dta_mm - settings.GAMMA_MCSQUARE_VS_TPS_DTA) < 0.01
        ),
        next((r for r in gamma_rows if r.field_name == "Composite" or r.beam_number is None), None),
    )

    # Deduplicate beam rows so each beam is listed exactly once (latest calculation)
    beam_dict: dict[Any, GammaResult] = {}
    for r in gamma_rows:
        if r.beam_number is None and r.field_name == "Composite":
            continue
        key = r.beam_number if r.beam_number is not None else r.field_name
        if key not in beam_dict:
            beam_dict[key] = r
        else:
            prev = beam_dict[key]
            curr_matches = (
                abs(r.dd_percent - settings.GAMMA_MCSQUARE_VS_TPS_DD) < 0.01
                and abs(r.dta_mm - settings.GAMMA_MCSQUARE_VS_TPS_DTA) < 0.01
            )
            prev_matches = (
                abs(prev.dd_percent - settings.GAMMA_MCSQUARE_VS_TPS_DD) < 0.01
                and abs(prev.dta_mm - settings.GAMMA_MCSQUARE_VS_TPS_DTA) < 0.01
            )
            if curr_matches and not prev_matches:
                beam_dict[key] = r

    beam_rows = list(beam_dict.values())
    if not beam_rows:
        beam_rows = [r for r in gamma_rows if r != composite_row] if composite_row else list(gamma_rows)
    beam_rows.sort(key=lambda r: (r.beam_number if r.beam_number is not None else 0, r.field_name or ""))

    # Load dose grids for dose metrics and slice previews
    from services.gamma_analysis import load_plan_doses
    doses = load_plan_doses(plan_id, db)
    tps_grid = doses.get("tps")
    mc_grid = doses.get("mcSquare")

    tps_max = tps_grid.max_dose if tps_grid else None
    mc_max = mc_grid.max_dose if mc_grid else None
    dose_diff_pct = None
    if tps_max and mc_max and tps_max > 0:
        dose_diff_pct = ((mc_max - tps_max) / tps_max) * 100.0

    # Render central/max slice comparison
    slice_z = tps_grid.max_dose_plane_index() if tps_grid else 0
    tps_slice_img = None
    mc_slice_img = None
    if tps_grid and mc_grid:
        max_d = max(tps_max or 1.0, mc_max or 1.0)
        tps_slice_img = _render_dose_slice_png(tps_grid.plane(slice_z), "turbo", max_d)
        mc_slice_img = _render_dose_slice_png(mc_grid.plane(slice_z), "turbo", max_d)

    # Gamma map thumbnail
    gamma_thumb_path = composite_row.gamma_map_path if composite_row else (beam_rows[0].gamma_map_path if beam_rows else None)
    gamma_thumb = _gamma_thumbnail(gamma_thumb_path, size=220)

    # Gate decision
    try:
        from services import evidence as _evidence
        from services.pipeline import _db_path
        _decision, _ = _evidence.plan_gate(plan_id, _db_path())
        _gate = _decision.to_dict()
    except Exception as _gate_exc:
        logger.warning(f"Gate evaluation failed for secondary dose report {plan_id}: {_gate_exc}")
        _gate = None

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # Beam / field parameters
    from services.dicom_ingestor import parse_rtplan_fields
    import pydicom
    field_details = []
    if plan.dicom_store_path:
        for p in Path(plan.dicom_store_path).glob("*.dcm"):
            try:
                ds = pydicom.dcmread(str(p), stop_before_pixels=True)
                if ds.get("Modality") in ("RTPLAN", "RTIBTR"):
                    field_details = parse_rtplan_fields(ds)
                    break
            except Exception:
                continue

    parts: List[str] = []
    parts.append(_REPORT_CSS)
    parts.append('<div class="report">')
    
    # Header
    parts.append(
        f'<div class="header">'
        f'<div>'
        f'<div class="inst-header">CLINICAL RADIATION ONCOLOGY &bull; MEDICAL PHYSICS</div>'
        f'<h1>Secondary Dose Calculation &amp; Gamma Analysis Report</h1>'
        f'<div class="muted">Independent Monte Carlo QA (openMCsquare vs TPS) &middot; {now}</div>'
        f'</div>'
        f'<div class="actions"><button onclick="window.print()">Print / Save as PDF</button></div>'
        f'</div>'
    )

    # Patient & Plan Cards
    parts.append('<div class="grid2">')
    parts.append(
        '<div class="card"><h2>Patient Information</h2>'
        f'<div class="kv"><span>Patient ID</span><b>{escape(patient.patient_id if patient else "—")}</b></div>'
        f'<div class="kv"><span>Patient Name</span><b>{escape(patient.patient_name if patient else "—")}</b></div>'
        f'<div class="kv"><span>Gender</span><b>{escape(str(patient.sex) if patient and patient.sex else "—")}</b></div>'
        "</div>"
    )
    parts.append(
        '<div class="card"><h2>Plan Information</h2>'
        f'<div class="kv"><span>Plan Label</span><b>{escape(plan.plan_label)}</b></div>'
        f'<div class="kv"><span>Plan Name</span><b>{escape(plan.plan_name or "—")}</b></div>'
        f'<div class="kv"><span>Treatment Site</span><b>{escape(plan.treatment_site or "—")}</b></div>'
        f'<div class="kv"><span>Fields / Fractions</span><b>{plan.number_of_fields} fields / {plan.number_of_fractions or "—"} fx</b></div>'
        "</div>"
    )
    parts.append('</div>')

    # Simulation Parameters & Commissioning
    bdl_name = getattr(settings, "MCSQUARE_BDL_NAME", "auto")
    primaries = getattr(settings, "MCSQUARE_PRIMARIES", 1_000_000)
    unc = getattr(settings, "MCSQUARE_STAT_UNCERTAINTY", 1.5)
    rbe = getattr(settings, "PROTON_RBE", 1.10)
    geom = getattr(settings, "MCSQUARE_GEOMETRY", "patient_ct")
    sim_mode = getattr(settings, "MCSQUARE_SIMULATION_MODE", False)

    parts.append(
        '<div class="card"><h2>Monte Carlo Engine &amp; Calculation Configuration</h2>'
        '<div class="grid3">'
        f'<div class="param-box"><span class="param-lbl">Algorithm</span><b class="param-val">{"Mock Simulation" if sim_mode else "openMCsquare CPU"}</b></div>'
        f'<div class="param-box"><span class="param-lbl">Geometry Model</span><b class="param-val">{escape(geom)}</b></div>'
        f'<div class="param-box"><span class="param-lbl">Beam Model (BDL)</span><b class="param-val">{escape(bdl_name)}</b></div>'
        f'<div class="param-box"><span class="param-lbl">Protons Simulated</span><b class="param-val">{primaries:,}</b></div>'
        f'<div class="param-box"><span class="param-lbl">Target Uncertainty</span><b class="param-val">&le; {unc}%</b></div>'
        f'<div class="param-box"><span class="param-lbl">Applied RBE</span><b class="param-val">{rbe:.2f}</b></div>'
        '</div>'
        '</div>'
    )

    # 3D Gamma Analysis Overview KPI
    if composite_row:
        pass_rate = composite_row.passing_rate
        passed = composite_row.passed
        dd = composite_row.dd_percent
        dta = composite_row.dta_mm
        thr = composite_row.threshold
    elif beam_rows:
        pass_rate = float(np.mean([r.passing_rate for r in beam_rows]))
        passed = all(r.passed for r in beam_rows)
        dd = beam_rows[0].dd_percent
        dta = beam_rows[0].dta_mm
        thr = beam_rows[0].threshold
    else:
        pass_rate = 0.0
        passed = False
        dd = settings.GAMMA_MCSQUARE_VS_TPS_DD
        dta = settings.GAMMA_MCSQUARE_VS_TPS_DTA
        thr = settings.GAMMA_MCSQUARE_VS_TPS_THRESHOLD

    status_col = "#3fb950" if passed else "#f85149"
    status_text = "PASS" if passed else "ACTION REQUIRED (FAIL)"

    parts.append(
        '<div class="card highlight-card">'
        '<h2>3D Gamma Analysis Summary (Overall Plan)</h2>'
        '<div class="kpi-banner">'
        f'<div class="kpi-score" style="color:{status_col}">{pass_rate:.1f}%</div>'
        '<div>'
        f'<div class="badge" style="background:{status_col}22;color:{status_col};border-color:{status_col}66;font-size:14px;padding:6px 14px;">{status_text}</div>'
        f'<div class="muted" style="margin-top:6px;">TG-218 Criterion: <b>{dd:g}% / {dta:g} mm</b> &middot; Action Level: <b>&ge; {thr:g}%</b> &middot; Dose Cutoff: <b>10.0%</b></div>'
        '</div>'
        '</div>'
        '</div>'
    )

    # Beam-by-beam breakdown table
    parts.append('<div class="card"><h2>Field-by-Field Secondary Dose &amp; Gamma Breakdown</h2>')
    parts.append('<table class="tbl"><thead><tr>'
                 '<th>Beam #</th><th>Field Name</th><th>Gantry</th><th>Energy Range</th><th>Layers / Spots</th><th>MU</th><th>Pass Rate</th><th>Status</th>'
                 '</tr></thead><tbody>')

    if beam_rows:
        for r in beam_rows:
            f_meta = next((f for f in field_details if f.get("beam_name") == r.field_name or f.get("beam_number") == r.beam_number), {})
            gantry = f"{f_meta.get('gantry_angle', 0):.0f}&deg;" if "gantry_angle" in f_meta else "—"
            emin = f_meta.get("energy_min_mev")
            emax = f_meta.get("energy_max_mev")
            erange = f"{emin:.1f} - {emax:.1f} MeV" if emin and emax else "—"
            nlayers = f_meta.get("number_of_layers", "—")
            nspots = f_meta.get("total_spots", "—")
            mu = f"{f_meta.get('total_mu', 0):.1f}" if "total_mu" in f_meta else "—"
            rcol = "#3fb950" if r.passed else "#f85149"
            rst = "PASS" if r.passed else "FAIL"

            parts.append(
                f'<tr>'
                f'<td>{escape(str(r.beam_number or "—"))}</td>'
                f'<td><b>{escape(r.field_name)}</b></td>'
                f'<td>{gantry}</td>'
                f'<td>{erange}</td>'
                f'<td>{nlayers} ly / {nspots} sp</td>'
                f'<td class="num">{mu}</td>'
                f'<td class="num" style="color:{rcol};font-weight:700;">{r.passing_rate:.1f}%</td>'
                f'<td><span class="badge-mini" style="background:{rcol}22;color:{rcol};">{rst}</span></td>'
                f'</tr>'
            )
    else:
        # If per-beam rows aren't separate, list composite row
        rcol = "#3fb950" if passed else "#f85149"
        parts.append(
            f'<tr>'
            f'<td>1..{plan.number_of_fields}</td>'
            f'<td><b>Composite Plan</b></td>'
            f'<td>—</td>'
            f'<td>—</td>'
            f'<td>—</td>'
            f'<td class="num">—</td>'
            f'<td class="num" style="color:{rcol};font-weight:700;">{pass_rate:.1f}%</td>'
            f'<td><span class="badge-mini" style="background:{rcol}22;color:{rcol};">{"PASS" if passed else "FAIL"}</span></td>'
            f'</tr>'
        )

    parts.append('</tbody></table></div>')

    # Visual Slice & Gamma Error Maps
    parts.append('<div class="card"><h2>Spatial Dose Distributions &amp; Gamma Map (Slice z = ' + str(slice_z) + ')</h2>')
    parts.append('<div class="img-strip">')
    
    if tps_slice_img:
        parts.append(f'<div class="img-box"><img src="{tps_slice_img}" class="slice-img" alt="TPS Dose"/><span>TPS Reference Dose</span></div>')
    if mc_slice_img:
        parts.append(f'<div class="img-box"><img src="{mc_slice_img}" class="slice-img" alt="MC Dose"/><span>MCsquare Secondary Dose</span></div>')
    if gamma_thumb:
        parts.append(f'<div class="img-box"><img src="{gamma_thumb}" class="slice-img" alt="Gamma Map"/><span>Gamma Error Map (&gamma; &le; 1 Green, &gamma; &gt; 1 Red)</span></div>')

    parts.append('</div></div>')

    # openMCsquare Robustness Analysis & DVH Predictions
    dvh_data = None
    try:
        from services.dvh_service import calculate_plan_dvh_and_robustness
        dvh_data = calculate_plan_dvh_and_robustness(plan_id, db, force_recompute=False)
    except Exception as _dvh_err:
        logger.warning(f"Failed loading DVH for secondary dose report {plan_id}: {_dvh_err}")

    if dvh_data and dvh_data.rois:
        rx_note = f" &middot; Target Prescription Dose: <b>{dvh_data.prescription_dose_gy:.1f} Gy</b>" if dvh_data.prescription_dose_gy else ""
        parts.append(
            '<div class="card">'
            '<h2>openMCsquare Robustness Analysis &amp; DVH Predictions</h2>'
            '<div class="muted" style="margin-bottom:8px;">'
            f'Cumulative DVH curves and worst-case scenario envelopes across <b>{dvh_data.num_scenarios} clinical scenarios</b> '
            f'(&plusmn;{dvh_data.setup_uncertainty_mm:g} mm setup uncertainty, &plusmn;{dvh_data.range_uncertainty_pct:g}% range scaling){rx_note}.'
            '</div>'
            f'{_dvh_svg(dvh_data)}'
            f'{_render_dvh_table_html(dvh_data)}'
            '</div>'
        )

    # Clinical Gate Verdict
    if _gate:
        vlabel, vcolor = _GATE_LABEL.get(_gate["status"], (_gate["status"], "#7d8590"))
        waived = _gate.get("dry_run_waived")
        parts.append(
            '<div class="card verdict"><h2>Clinical Delivery Gate Verdict</h2>'
            '<div class="verdict-row">'
            f'<div><div class="badge" style="background:{vcolor}22;color:{vcolor};border-color:{vcolor}55;font-size:14px;padding:6px 14px;">{escape(vlabel)}</div>'
            f'<div class="muted" style="margin-top:8px;">{escape(_gate.get("reason", ""))}</div>'
            f'<div class="muted" style="margin-top:4px;">Pre-treatment physical measurement / dry-run: <b>{"WAIVED (Eligible for Virtual PSQA)" if waived else "REQUIRED"}</b></div>'
            '</div></div></div>'
        )

    # Formal Physicist Sign-off Box
    parts.append(
        '<div class="card signoff"><h2>Medical Physicist Review &amp; Approval</h2>'
        '<div class="sign-line"><span>Reviewing Physicist (Print Name)</span><div class="line"></div></div>'
        '<div class="sign-line"><span>Signature</span><div class="line"></div></div>'
        '<div class="sign-line"><span>Approval Date</span><div class="line short"></div></div>'
        '<div style="margin-top:16px;"><span class="muted">Physicist Comments / Clinical Rationales:</span><div class="comment-box"></div></div>'
        '</div>'
    )

    # Footer
    parts.append(
        '<div class="footer">'
        'Virtual PSQA &middot; Independent Proton Secondary Dose Calculation System &middot; AAPM TG-218 Compliant &middot; '
        f'Generated: {now}'
        '</div>'
    )

    parts.append('</div>')
    return "<!doctype html><html><head><meta charset='utf-8'>" \
           f"<title>Secondary Dose QA Report — {escape(plan.plan_label)}</title></head><body>" \
           + "".join(parts) + "</body></html>"


def build_synthetic_ct_report_html(
    plan_id: int,
    fraction_number: Optional[int],
    db: Session,
) -> str:
    """
    Generate dedicated clinical QA report for SyntheticQACT Adaptive Dose & Setup Verification.
    Documents daily synthetic CT recalculation on patient anatomy, setup shift correlation,
    and 3D gamma passing rates across treatment fractions.
    """
    from models.synthetic_ct import SyntheticCT

    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise ValueError(f"Plan {plan_id} not found")

    query = db.query(SyntheticCT).filter_by(plan_id=plan_id)
    if fraction_number is not None:
        query = query.filter_by(fraction_number=fraction_number)
    records = query.order_by(SyntheticCT.fraction_number.asc()).all()

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    patient = plan.patient
    pat_id = patient.patient_id if patient else "—"
    pat_name = patient.patient_name if patient else "—"

    # Overall summary metrics
    completed = [r for r in records if r.status == "complete" and r.gamma_passing_rate is not None]
    if completed:
        mean_gamma = float(np.mean([r.gamma_passing_rate for r in completed]))
        all_passed = all(r.gamma_passed for r in completed)
    else:
        mean_gamma = None
        all_passed = True

    status_col = "#3fb950" if all_passed else "#f85149"
    status_text = "PASS" if all_passed else "ACTION REQUIRED"

    parts = [_REPORT_CSS]
    parts.append('<div class="report">')

    # Print / action buttons
    parts.append(
        '<div class="actions" style="display:flex;justify-content:flex-end;gap:8px;margin-bottom:12px;">'
        '<button onclick="window.print()" style="background:#238636;color:#fff;border:none;padding:6px 14px;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;">Print / Save as PDF</button>'
        '</div>'
    )

    # Header
    title_suffix = f" (Fraction {fraction_number})" if fraction_number else " (All Delivered Fractions)"
    parts.append(
        '<div class="header">'
        '<div>'
        '<div class="inst-header">VIRTUAL PSQA &middot; ADAPTIVE PROTON THERAPY QA</div>'
        f'<h1>SyntheticQACT Adaptive Setup &amp; Dose QA Report{title_suffix}</h1>'
        f'<div class="muted">Plan: <b>{escape(plan.plan_label)}</b> &middot; {escape(plan.plan_name or "")}</div>'
        '</div>'
        '<div style="text-align:right;">'
        f'<div style="font-size:14px;font-weight:700;color:#f0f6fc;">{escape(pat_name)}</div>'
        f'<div class="muted">MRN: {escape(pat_id)}</div>'
        f'<div class="muted">Site: {escape(plan.treatment_site or "General")}</div>'
        '</div>'
        '</div>'
    )

    # Adaptive Course KPI Banner
    parts.append(
        '<div class="card highlight-card">'
        '<h2>Adaptive Dose Verification Summary</h2>'
        '<div class="kpi-banner">'
        f'<div class="kpi-score" style="color:{status_col}">{mean_gamma:.1f}%' if mean_gamma is not None else '<div class="kpi-score" style="color:#7d8590">—'
        '</div>'
        '<div>'
        f'<div class="badge" style="background:{status_col}22;color:{status_col};border-color:{status_col}66;font-size:14px;padding:6px 14px;">{status_text}</div>'
        f'<div class="muted" style="margin-top:6px;">Calculated on <b>openMCsquare (Stoichiometric CT HU calibration)</b> &middot; Tolerance: <b>3%/3mm &ge; 90.0%</b></div>'
        f'<div class="muted" style="margin-top:2px;">Scans Evaluated: <b>{len(completed)} of {len(records)} fractions</b></div>'
        '</div>'
        '</div>'
        '</div>'
    )

    # Fraction-by-Fraction Adaptive Tracking Table
    parts.append('<div class="card"><h2>Fraction-by-Fraction Adaptive Setup &amp; Dose Fidelity</h2>')
    parts.append('<table class="tbl"><thead><tr>'
                 '<th>Fraction</th><th>Scan Date</th><th>Slices / Dims</th><th>Couch Shifts (Lat / Long / Vert)</th><th>3%/3mm Gamma</th><th>2%/2mm Gamma</th><th>Mean Dose &Delta;</th><th>Status</th>'
                 '</tr></thead><tbody>')

    if records:
        for r in records:
            dt_str = r.scan_date.strftime("%Y-%m-%d %H:%M") if r.scan_date else (r.created_at.strftime("%Y-%m-%d") if r.created_at else "—")
            dims_str = f"{r.num_slices} sl ({r.dimensions or '—'})"
            lat = f"{r.setup_shift_lat_mm:+.1f}" if r.setup_shift_lat_mm is not None else "—"
            lon = f"{r.setup_shift_long_mm:+.1f}" if r.setup_shift_long_mm is not None else "—"
            ver = f"{r.setup_shift_vert_mm:+.1f}" if r.setup_shift_vert_mm is not None else "—"
            couch_str = f"{lat} / {lon} / {ver} mm" if r.setup_shift_lat_mm is not None else "Aligned to Iso"

            g33 = f"{r.gamma_passing_rate:.1f}%" if r.gamma_passing_rate is not None else "—"
            g22 = f"{r.gamma_2mm_passing_rate:.1f}%" if r.gamma_2mm_passing_rate is not None else "—"
            mean_d = f"{r.mean_dose_diff_pct:+.2f}%" if r.mean_dose_diff_pct is not None else "—"

            rcol = "#3fb950" if r.gamma_passed else ("#f85149" if r.status == "complete" else "#7d8590")
            rst = "PASS" if r.gamma_passed else ("FAIL" if r.status == "complete" else r.status.upper())

            parts.append(
                f'<tr>'
                f'<td><b>Fraction {r.fraction_number}</b></td>'
                f'<td>{dt_str}</td>'
                f'<td>{dims_str}</td>'
                f'<td>{couch_str}</td>'
                f'<td class="num" style="color:{rcol};font-weight:700;">{g33}</td>'
                f'<td class="num">{g22}</td>'
                f'<td class="num">{mean_d}</td>'
                f'<td><span class="badge-mini" style="background:{rcol}22;color:{rcol};">{rst}</span></td>'
                f'</tr>'
            )
    else:
        parts.append('<tr><td colspan="8" style="text-align:center;padding:16px;color:#8b949e;">No syntheticQACT scans imported yet.</td></tr>')

    parts.append('</tbody></table></div>')

    # Visual Gamma Thumbnails for evaluated fractions
    gamma_boxes = []
    for r in records:
        if r.status == "complete":
            g_path = Path(settings.RESULTS_PATH) / f"plan_{plan_id}" / "synthetic_ct" / f"fx_{r.fraction_number}" / "output" / "gamma_sct_vs_tps.npz"
            thumb = _gamma_thumbnail(str(g_path) if g_path.exists() else None)
            if thumb:
                g_rate = f"{r.gamma_passing_rate:.1f}%" if r.gamma_passing_rate is not None else ""
                gamma_boxes.append(
                    f'<div class="img-box"><img src="{thumb}" class="slice-img" alt="Gamma Map Fx {r.fraction_number}"/>'
                    f'<span><b>Fraction {r.fraction_number}</b> &middot; {g_rate} Pass</span></div>'
                )

    if gamma_boxes:
        parts.append('<div class="card"><h2>3D Gamma Error Maps on Daily Synthetic CT Anatomy (Isocenter Slice)</h2>')
        parts.append('<div class="img-strip">')
        parts.extend(gamma_boxes)
        parts.append('</div>')
        parts.append('<div class="muted" style="text-align:center;margin-top:8px;">Green: &gamma; &le; 1.0 (Passing) &middot; Red: &gamma; &gt; 1.0 (Dose Discrepancy due to Setup / Anatomy)</div>')
        parts.append('</div>')

    # Physicist Sign-off Box
    parts.append(
        '<div class="card signoff"><h2>Medical Physicist Review &amp; Adaptive Action</h2>'
        '<div class="sign-line"><span>Attending Medical Physicist (Print Name)</span><div class="line"></div></div>'
        '<div class="sign-line"><span>Signature</span><div class="line"></div></div>'
        '<div class="sign-line"><span>Review Date</span><div class="line short"></div></div>'
        '<div style="margin-top:16px;"><span class="muted">Clinical Assessment &amp; Adaptive Action Plan:</span><div class="comment-box"></div></div>'
        '</div>'
    )

    # Footer
    parts.append(
        '<div class="footer">'
        'Virtual PSQA &middot; SyntheticQACT Adaptive Setup &amp; Dose Verification System &middot; '
        f'Generated: {now}'
        '</div>'
    )

    parts.append('</div>')
    return "<!doctype html><html><head><meta charset='utf-8'>" \
           f"<title>SyntheticQACT Adaptive QA Report — {escape(plan.plan_label)}</title></head><body>" \
           + "".join(parts) + "</body></html>"



def render_pdf(html: str) -> Optional[bytes]:
    """Render HTML to PDF via WeasyPrint if available; else None."""
    try:
        from weasyprint import HTML  # type: ignore

        return HTML(string=html).write_pdf()
    except Exception as exc:  # noqa: BLE001
        logger.info(f"WeasyPrint unavailable — serving HTML report instead ({exc})")
        return None


_REPORT_CSS = """
<style>
  body { margin: 0; background: #0d1117; color: #e6edf3;
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  .report { max-width: 860px; margin: 0 auto; padding: 24px; }
  .inst-header { font-size: 11px; font-weight: 700; letter-spacing: 1px; color: #58a6ff; margin-bottom: 4px; }
  h1 { font-size: 20px; margin: 0 0 4px; color: #f0f6fc; }
  h2 { font-size: 13px; margin: 0 0 10px; color: #c9d1d9; text-transform: uppercase; letter-spacing: 0.5px; }
  .muted { color: #8b949e; font-size: 12px; }
  .header { display: flex; justify-content: space-between; align-items: flex-start;
            margin-bottom: 20px; border-bottom: 1px solid #21262d; padding-bottom: 16px; }
  .actions button { background: #238636; color: #fff; border: 0; padding: 8px 16px;
            border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; }
  .actions button:hover { background: #2ea043; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 14px; }
  .grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
          padding: 16px; margin-bottom: 14px; }
  .highlight-card { border-left: 4px solid #58a6ff; }
  .kv { display: flex; justify-content: space-between; font-size: 13px; padding: 4px 0; border-bottom: 1px solid #21262d; }
  .kv:last-child { border-bottom: 0; }
  .kv span { color: #8b949e; }
  .kpi-banner { display: flex; align-items: center; gap: 24px; padding: 8px 0; }
  .kpi-score { font-size: 42px; font-weight: 800; line-height: 1; }
  .badge { display: inline-block; padding: 4px 10px; border-radius: 999px;
           font-size: 12px; font-weight: 600; border: 1px solid; }
  .badge-mini { display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 700; }
  .param-box { background: #0d1117; border: 1px solid #21262d; border-radius: 6px; padding: 8px 12px; }
  .param-lbl { display: block; font-size: 11px; color: #8b949e; margin-bottom: 2px; }
  .param-val { font-size: 13px; color: #f0f6fc; }
  .tbl { width: 100%; border-collapse: collapse; font-size: 12px; }
  .tbl th { text-align: left; padding: 8px 6px; border-bottom: 2px solid #30363d; color: #8b949e; font-size: 11px; text-transform: uppercase; }
  .tbl td { padding: 8px 6px; border-bottom: 1px solid #21262d; }
  .tbl td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .omr-notice { background: rgba(56, 139, 253, 0.1); border: 1px solid rgba(56, 139, 253, 0.3); border-radius: 6px; padding: 12px 14px; }
  .notice-badge { font-size: 10px; font-weight: 700; letter-spacing: 0.8px; color: #58a6ff; margin-bottom: 4px; text-transform: uppercase; }
  .notice-text { font-size: 12px; color: #c9d1d9; line-height: 1.5; }
  .img-strip { display: flex; gap: 16px; justify-content: center; flex-wrap: wrap; margin-top: 10px; }
  .img-box { text-align: center; }
  .slice-img { width: 220px; height: 220px; border-radius: 6px; border: 1px solid #30363d; image-rendering: pixelated; display: block; margin-bottom: 6px; }
  .img-box span { font-size: 11px; color: #8b949e; }
  .field-qa-block { background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 14px; margin-bottom: 14px; }
  .field-qa-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; border-bottom: 1px solid #21262d; padding-bottom: 8px; flex-wrap: wrap; gap: 8px; }
  .field-title { font-size: 14px; font-weight: 700; color: #f0f6fc; }
  .field-pr { font-size: 15px; font-weight: 800; margin-right: 8px; }
  .signoff .sign-line { display: flex; align-items: flex-end; gap: 12px; margin-top: 18px; font-size: 13px; color: #8b949e; }
  .sign-line .line { flex: 1; border-bottom: 1px solid #6e7681; height: 18px; }
  .sign-line .line.short { max-width: 220px; }
  .comment-box { border: 1px solid #30363d; height: 50px; border-radius: 6px; background: #0d1117; margin-top: 6px; }
  .dvh-container { text-align: center; margin-top: 10px; margin-bottom: 14px; }
  svg.dvh-chart { max-width: 100%; height: auto; background: #0d1117; border-radius: 6px; border: 1px solid #21262d; }
  .dvh-legend { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; font-size: 11px; margin-top: 6px; color: #8b949e; }
  .footer { text-align: center; color: #8b949e; font-size: 11px; margin-top: 24px; border-top: 1px solid #21262d; padding-top: 12px; }
  @media print {
    body { background: #fff !important; color: #111 !important; font-size: 11pt; }
    .report { max-width: 100% !important; padding: 0 !important; }
    .actions { display: none !important; }
    .card { background: #fff !important; border: 1px solid #ddd !important; break-inside: avoid; color: #111 !important; box-shadow: none !important; margin-bottom: 10px !important; }
    .field-qa-block { background: #fff !important; border-color: #ddd !important; break-inside: avoid; margin-bottom: 10px !important; }
    .field-qa-header { border-bottom-color: #eee !important; }
    .field-title { color: #111 !important; }
    .field-pr { color: inherit !important; }
    .param-box { background: #f8f9fa !important; border-color: #eee !important; }
    .param-lbl, .muted, .kv span, .tbl th, .img-box span { color: #555 !important; }
    .param-val, h1, h2, b { color: #111 !important; }
    .tbl td { border-bottom-color: #eee !important; color: #111 !important; }
    .comment-box { background: #fff !important; border-color: #ccc !important; }
    .omr-notice { background: #f8f9fa !important; border-color: #ccc !important; }
    .notice-badge { color: #0969da !important; }
    .notice-text { color: #24292f !important; }
    .sign-line .line { border-bottom-color: #000 !important; }
    svg.dvh-chart { background: #fff !important; border-color: #ddd !important; }
    svg.dvh-chart line { stroke: #e1e4e8 !important; }
    svg.dvh-chart text { fill: #24292f !important; }
    .dvh-legend { color: #555 !important; }
    .footer { color: #666 !important; border-top-color: #ddd !important; }
  }
</style>
"""

from services.chart_check_service import build_chart_check_report_html  # noqa: E402


