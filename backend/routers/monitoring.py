"""
routers/monitoring.py  --  evidence-based verdict and per-fraction monitoring.

Replaces the complexity-derived "pass probability" gauge, which scores plan
characteristics (SAS, spots/layer, energy range) as if they predict QA outcome.
They do not: log-QA failures at this center are transient machine events, not
plan properties, and a leave-one-plan-out test of the complexity model scored
at or below a constant baseline on a third of held-out plans.

This endpoint instead reports what the evidence actually shows:

  * GATE     is this plan cleared to treat without a pre-treatment dry run?
             Delegated to services/gate.py, which combines four evidence
             layers: MCsquare secondary dose, deliverability against the
             commissioned machine envelope, room-level control state, and
             per-fraction in-vivo log verification. The thresholds live in
             gate.py alone so this router and services/pipeline.py cannot
             drift apart.

             Fail-closed: a layer that has not been evaluated blocks
             clearance rather than being treated as a pass.

  * CHART    per-plan, per-beam mean-radial-position control series, with
             robust limits (median +/- k * 1.4826 * MAD) established from that
             plan-beam's own baseline, and Western Electric violations. This is
             the series that flagged the plan-17 excursion -- including on a
             beam whose gamma still passed.

Self-contained: opens its own read-only SQLite connection to the same database
file the app writes (resolved from database.engine), so it does not depend on
the ORM models or the session dependency. Mount in main.py:

    from routers import monitoring
    app.include_router(monitoring.router)

Must be included BEFORE the SPA catch-all route in main.py (it already is, as
all include_router calls precede spa_fallback).

Endpoints:
    GET /api/plans/{plan_id}/monitoring     full payload (gate + charts + gamma)
    GET /api/monitoring/health              sanity check
"""

from __future__ import annotations

import math
import os
import sqlite3
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Depends
from sqlalchemy.orm import Session
from database import get_db

from services import evidence, gate, prediction_model

router = APIRouter(tags=["monitoring"])

# ---- constants -----------------------------------------------------------
ACTION_LEVEL = 90.0          # % passing at 2%/2mm (TG-218)
MARGINAL_LEVEL = 93.0        # amber band: passes, but worth a look
K_SIGMA = 3.0
MAD_TO_SIGMA = 1.4826
MATCH_ARTIFACT_MM = 10.0     # pos_max above this = reconstruction mismatch
MIN_BASELINE_N = 8           # control limits need at least this many points

# Verified envelope (Section 5 of the protocol). Descriptive here; not yet
# wired into the gate. Regenerate as the verified set grows.
ENVELOPE = {
    "n_layers":         (11, 29),
    "beam_mu":          (139.0, 2768.0),
    "min_layer_mu":     (0.29, 3.45),
    "mean_mu_per_spot": (0.05, 0.23),
    "n_spots":          (640, 22325),
    "energy_max":       (69.4, 201.5),
}


# ---- database ------------------------------------------------------------

def _db_path() -> str:
    """Resolve the SQLite file the app itself uses, via database.engine."""
    try:
        from database import engine
        url = str(engine.url)                 # e.g. sqlite:///./data/psqa.db
        if url.startswith("sqlite"):
            p = url.split("///", 1)[-1]
            if os.path.exists(p):
                return p
    except Exception:
        pass
    for c in (os.environ.get("PSQA_DB", ""),
              os.path.join("data", "psqa.db")):
        if c and os.path.exists(c):
            return c
    return ""


def _db() -> sqlite3.Connection:
    p = _db_path()
    if not p:
        raise HTTPException(status_code=500, detail="psqa.db not found on server")
    cx = sqlite3.connect(p, timeout=15.0)
    cx.row_factory = sqlite3.Row
    return cx


# ---- robust statistics ---------------------------------------------------

def _median(v):
    s = sorted(v)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _limits(vals, k=K_SIGMA):
    c = _median(vals)
    if c is None:
        return None, None
    mad = _median([abs(x - c) for x in vals])
    sigma = MAD_TO_SIGMA * mad if mad else 0.0
    if sigma == 0.0 and len(vals) > 1:
        m = sum(vals) / len(vals)
        sigma = math.sqrt(sum((x - m) ** 2 for x in vals) / (len(vals) - 1))
    return c, sigma


def _western_electric(vals, centre, sigma):
    """{index: [rule names]} for out-of-control points."""
    flags = {}
    if not sigma or sigma <= 0:
        return flags
    z = [(x - centre) / sigma for x in vals]
    for i, zi in enumerate(z):
        if abs(zi) > 3:
            flags.setdefault(i, []).append("beyond_3sigma")
    for i in range(len(z) - 2):
        w = z[i:i + 3]
        for sign in (1, -1):
            if sum(1 for x in w if sign * x > 2) >= 2:
                for j, x in enumerate(w):
                    if sign * x > 2:
                        flags.setdefault(i + j, []).append("two_of_three_2sigma")
    run, sgn = 0, 0
    for i, zi in enumerate(z):
        s = 1 if zi > 0 else (-1 if zi < 0 else 0)
        run = run + 1 if (s and s == sgn) else 1
        sgn = s
        if run >= 8:
            for j in range(i - 7, i + 1):
                flags.setdefault(j, [])
                if "run_of_8" not in flags[j]:
                    flags[j].append("run_of_8")
    return flags


# ---- data access ---------------------------------------------------------

def _plan(cx, plan_id):
    r = cx.execute(
        "SELECT id, plan_label, plan_name, treatment_site, number_of_fractions,"
        " number_of_fields FROM plans WHERE id=?", (plan_id,)).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail=f"plan {plan_id} not found")
    return r


def _log_gamma_rows(cx, plan_id):
    return cx.execute(
        "SELECT fraction_number, field_name, passing_rate, passed "
        "FROM gamma_results WHERE plan_id=? AND comparison_type='log_vs_Rx' "
        "ORDER BY fraction_number", (plan_id,)).fetchall()


def _beam_series(cx, plan_id):
    return cx.execute(
        "SELECT fraction_number, beam_name, treatment_date, machine, "
        "pos_mean_radial_mm, pos_max_radial_mm, mu_deviation_pct, "
        "gamma_passing_rate FROM beam_deliveries "
        "WHERE plan_id=? AND record_incomplete=0 "
        "ORDER BY beam_name, treatment_date, fraction_number", (plan_id,)
    ).fetchall()


# ---- verdict assembly ----------------------------------------------------

def _build_charts(rows):
    by_beam = {}
    for r in rows:
        if r["pos_max_radial_mm"] is not None and \
           r["pos_max_radial_mm"] > MATCH_ARTIFACT_MM:
            continue  # reconstruction matching artifact
        if r["pos_mean_radial_mm"] is None:
            continue
        by_beam.setdefault(r["beam_name"], []).append(r)

    charts = []
    any_signal = False
    for beam, series in sorted(by_beam.items()):
        vals = [float(r["pos_mean_radial_mm"]) for r in series]
        established = len(vals) >= MIN_BASELINE_N
        centre, sigma = _limits(vals) if established else (None, None)
        flags = _western_electric(vals, centre, sigma) if established else {}
        points = []
        for i, r in enumerate(series):
            f = flags.get(i, [])
            if f:
                any_signal = True
            points.append({
                "fraction": r["fraction_number"],
                "date": r["treatment_date"],
                "machine": r["machine"],
                "value": round(float(r["pos_mean_radial_mm"]), 4),
                "mu_deviation_pct": (round(float(r["mu_deviation_pct"]), 3)
                                     if r["mu_deviation_pct"] is not None
                                     else None),
                "gamma": (round(float(r["gamma_passing_rate"]), 2)
                          if r["gamma_passing_rate"] is not None else None),
                "flags": f,
            })
        charts.append({
            "beam": beam,
            "metric": "pos_mean_radial_mm",
            "unit": "mm",
            "established": established,
            "centre": round(centre, 4) if centre is not None else None,
            "sigma": round(sigma, 4) if sigma is not None else None,
            "ucl": round(centre + K_SIGMA * sigma, 4) if established else None,
            "lcl": (round(max(0.0, centre - K_SIGMA * sigma), 4)
                    if established else None),
            "n": len(vals),
            "points": points,
        })
    return charts, any_signal


def _log_layer(log_rows, any_signal) -> gate.LayerResult:
    """Build evidence layer 4 from this router's own log-gamma rows.

    Layer 4 is constructed here rather than in services/evidence.py because
    the per-plan-beam chart logic above -- MAD limits, Western Electric rules,
    and the MATCH_ARTIFACT_MM filter -- was validated against the plan-17
    excursion. A second implementation elsewhere computing a slightly
    different control signal would be a silent divergence in a clinical gate.
    """
    rates = [r["passing_rate"] for r in log_rows
             if r["passing_rate"] is not None]
    detail = ""
    if any_signal:
        detail = ("Position control-chart signal present. On plan 17 this "
                  "series flagged beam LP while its gamma still passed.")
    return gate.evaluate_log_verification(rates, any_signal, detail)


@router.get("/api/plans/{plan_id}/monitoring")
def plan_monitoring(plan_id: int):
    cx = _db()
    try:
        plan = _plan(cx, plan_id)
        log_rows = _log_gamma_rows(cx, plan_id)
        beam_rows = _beam_series(cx, plan_id)
    finally:
        cx.close()

    charts, any_signal = _build_charts(beam_rows)

    # Pre-treatment per-field prediction, if one has been computed and stored.
    # Read-only here: predicting costs seconds per beam and must not run on a
    # page load.
    try:
        predictions = prediction_model.plan_predictions(plan_id, _db_path())
    except Exception:  # noqa: BLE001
        predictions = []

    # Layers 1-3 come from the shared assembler; layer 4 from this router's
    # own validated chart analysis.
    try:
        l_dose, l_deliv, l_machine, ev = evidence.pretreatment_layers(
            plan_id, _db_path())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500,
                            detail=f"gate evaluation failed: {exc}") from exc
    decision = gate.decide(l_dose, l_deliv, l_machine,
                           _log_layer(log_rows, any_signal))

    prs = [r["passing_rate"] for r in log_rows if r["passing_rate"] is not None]
    log_summary = {
        "fractions_analysed": len(sorted({r["fraction_number"]
                                          for r in log_rows})),
        "plan_fractions": plan["number_of_fractions"],
        "mean_passing_rate": round(sum(prs) / len(prs), 2) if prs else None,
        "min_passing_rate": round(min(prs), 2) if prs else None,
        "criterion": "2%/2mm",
        "action_level": ACTION_LEVEL,
    }

    return {
        "plan_id": plan_id,
        "plan_label": plan["plan_label"] or plan["plan_name"],
        "gate": decision.to_dict(),
        "machine": ev.get("machine"),
        "log_summary": log_summary,
        "predictions": predictions,
        "charts": charts,
        "note": ("Verdict is evidence-based: secondary dose calculation, "
                 "deliverability, room control state and per-fraction log "
                 "verification. Plan-complexity metrics are shown for "
                 "description only and are not used to predict QA outcome. "
                 "A layer that has not been evaluated blocks clearance rather "
                 "than counting as a pass."),
    }


@router.get("/api/plans/{plan_id}/fractions/{fraction}/spots")
def fraction_spots(plan_id: int, fraction: int, beam: Optional[str] = None):
    """Per-spot delivered-vs-prescribed detail for one beam of one fraction.

    Reconstruction is re-run on demand rather than persisted: the per-spot
    arrays are ~3000 rows per beam per fraction, which is a lot of storage for
    something opened deliberately and rarely. A few seconds per beam is an
    acceptable cost for a detail view.

    Only summary statistics are stored per fraction (spot_stats_fx*.json), so
    the spatial structure of the deviation -- uniform translation vs radial
    scaling vs edge-weighted nonlinearity vs random scatter -- is not
    recoverable from stored data. Those all produce the same mean radial error
    and mean completely different things about the machine, which is the
    reason this endpoint exists.
    """
    cx = _db()
    try:
        plan = cx.execute(
            "SELECT dicom_store_path, rtplan_uid, plan_label FROM plans "
            "WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise HTTPException(status_code=404, detail=f"plan {plan_id}")
        frow = cx.execute(
            "SELECT rtrecord_path, machine, delivery_date FROM fractions "
            "WHERE plan_id=? AND fraction_number=?",
            (plan_id, fraction)).fetchone()
    finally:
        cx.close()

    if not frow or not frow["rtrecord_path"]:
        raise HTTPException(status_code=404,
                            detail=f"no RT Ion Record for fx {fraction}")

    plan_path = evidence._find_rtplan(plan["dicom_store_path"],
                                      plan["rtplan_uid"])
    if not plan_path:
        raise HTTPException(status_code=404, detail="RT Ion Plan not found")

    try:
        import numpy as np
        import pydicom
        from services.log_reconstruction import (
            reconstruct_from_record, _extract_prescribed_spots,
            _normalize_beam_name,
        )
        result = reconstruct_from_record(plan_path, frow["rtrecord_path"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500,
                            detail=f"reconstruction failed: {exc}") from exc

    beams = result.beams
    if beam:
        beams = [b for b in beams if b.beam_name == beam]
    if not beams:
        raise HTTPException(status_code=404,
                            detail=f"beam '{beam}' not in this record")
    b = beams[0]

    # Planned positions and energies are not carried on BeamReconstruction, so
    # re-extract them and truncate to the matched spot count.
    plan_ds = pydicom.dcmread(plan_path, force=True)
    idx = {_normalize_beam_name(x.BeamName): i
           for i, x in enumerate(plan_ds.IonBeamSequence)}
    if b.beam_name not in idx:
        raise HTTPException(status_code=404, detail="beam not in plan")
    rx = _extract_prescribed_spots(plan_ds, idx[b.beam_name])

    n = int(len(b.spot_x_offsets_mm))
    rx = rx[:n]

    def arr(a, nd=3):
        return [round(float(v), nd) for v in np.asarray(a)[:n]]

    rx_mu = np.asarray(b.spot_prescribed_mu)[:n]
    mu_err = np.asarray(b.spot_mu_errors)[:n]
    with np.errstate(divide="ignore", invalid="ignore"):
        mu_err_pct = np.where(rx_mu > 0, mu_err / rx_mu * 100.0, 0.0)

    size_rx = np.asarray(b.spot_size_rx_mm)
    size_dv = np.asarray(b.spot_size_dv_mm)
    has_size = size_rx.shape[0] >= n and size_dv.shape[0] >= n

    return {
        "plan_id": plan_id,
        "plan_label": plan["plan_label"],
        "fraction": fraction,
        "beam": b.beam_name,
        "machine": result.machine_name or frow["machine"],
        "treatment_date": result.treatment_date or frow["delivery_date"],
        "n_spots": n,
        # Planned position of each spot -- the plotting coordinates.
        "x": arr(rx[:, 4]), "y": arr(rx[:, 5]),
        "energy": arr(rx[:, 0], 2),
        "layer": [int(v) for v in rx[:, 6]],
        # Delivered minus prescribed.
        "dx": arr(b.spot_x_offsets_mm, 4),
        "dy": arr(b.spot_y_offsets_mm, 4),
        "rx_mu": arr(rx_mu, 5),
        # Delivered MU per spot, so the map can show the absolute value
        # alongside the relative error. Near the machine minimum a large
        # percentage can be a very small absolute quantity, and only the
        # absolute value distinguishes a delivery fault from quantisation.
        "dv_mu": arr(rx_mu + mu_err, 5),
        "mu_err": arr(mu_err, 5),
        "mu_err_pct": arr(mu_err_pct, 2),
        "size_rx_x": arr(size_rx[:, 0]) if has_size else [],
        "size_rx_y": arr(size_rx[:, 1]) if has_size else [],
        "size_dv_x": arr(size_dv[:, 0]) if has_size else [],
        "size_dv_y": arr(size_dv[:, 1]) if has_size else [],
    }


@router.get("/api/plans/{plan_id}/fractions")
def plan_fraction_list(plan_id: int):
    """Fractions with a stored record, for the spot-detail picker."""
    cx = _db()
    try:
        rows = cx.execute(
            "SELECT fraction_number, delivery_date, machine, qa_status, is_interrupted, interruption_reason "
            "FROM fractions WHERE plan_id=? AND rtrecord_path IS NOT NULL "
            "ORDER BY fraction_number", (plan_id,)).fetchall()
        beams = cx.execute(
            "SELECT DISTINCT beam_name FROM beam_deliveries WHERE plan_id=? "
            "ORDER BY beam_name", (plan_id,)).fetchall()
    finally:
        cx.close()
    return {
        "fractions": [dict(r) for r in rows],
        "beams": [r["beam_name"] for r in beams],
    }


@router.post("/api/plans/{plan_id}/fractions/{fraction_number}/reupload-record")
async def reupload_fraction_record(
    plan_id: int,
    fraction_number: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Allows the user to re-upload a merged/fixed RT Record for an interrupted fraction.
    Overwrites the old record in dicom_store, re-runs interruption detection, and
    re-triggers log dose reconstruction.
    """
    import io
    from pathlib import Path
    from datetime import datetime
    import pydicom
    from models.plan import Plan
    from models.fraction import Fraction
    from services.record_matcher import _find_plan_dicom, record_delivery_type
    from services.interruption_detector import detect_record_interruption
    from services.log_reconstructor import reconstruct_dose_from_log

    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    try:
        dcm = pydicom.dcmread(io.BytesIO(content), stop_before_pixels=True, force=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid DICOM file: {exc}")

    modality = str(getattr(dcm, "Modality", "")).upper()
    if modality not in ("RTRECORD", "RTIBTR"):
        raise HTTPException(
            status_code=400,
            detail=f"Uploaded file modality is '{modality}', expected RTRECORD or RTIBTR"
        )

    deliv_type = record_delivery_type(dcm)
    sop_uid = str(dcm.get("SOPInstanceUID", "") or "")
    uid_suffix = sop_uid.replace(".", "_")[-12:] if sop_uid else "merged"
    dest_filename = f"RTRecord_fx{fraction_number}_{uid_suffix}.dcm" if fraction_number != 0 else f"RTRecord_verification_{uid_suffix}.dcm"
    dest = Path(plan.dicom_store_path) / dest_filename
    dest.write_bytes(content)

    frac = (
        db.query(Fraction)
        .filter_by(plan_id=plan_id, fraction_number=fraction_number)
        .order_by(Fraction.id.desc())
        .first()
    )
    if frac is None:
        frac = Fraction(plan_id=plan_id, fraction_number=fraction_number)
        db.add(frac)

    frac.rtrecord_uid = sop_uid
    frac.rtrecord_path = str(dest)
    frac.delivery_type = deliv_type

    raw_date = str(dcm.get("TreatmentDate", "") or dcm.get("SeriesDate", "") or "")
    if len(raw_date) == 8 and raw_date.isdigit():
        try:
            frac.delivery_date = datetime.strptime(raw_date, "%Y%m%d").date()
        except Exception:
            pass

    # Check interruption status on the replacement record
    plan_dcm = _find_plan_dicom(plan.dicom_store_path)
    interruption_info = detect_record_interruption(dcm, plan_dcm)
    frac.is_interrupted = interruption_info["is_interrupted"]
    frac.interruption_reason = interruption_info["interruption_reason"]
    frac.qa_status = "interrupted" if frac.is_interrupted else "running"
    db.commit()

    # Trigger log dose reconstruction
    try:
        reconstruct_dose_from_log(plan_id=plan_id, fraction_number=fraction_number, db=db)
    except Exception as exc:
        pass

    db.refresh(frac)

    return {
        "status": "success",
        "plan_id": plan_id,
        "fraction_number": fraction_number,
        "is_interrupted": frac.is_interrupted,
        "interruption_reason": frac.interruption_reason,
        "qa_status": frac.qa_status,
        "message": (
            f"Fraction {fraction_number} record successfully updated. "
            + ("Warning: Replacement record still has interruption indicators." if frac.is_interrupted else "Complete delivery verified.")
        ),
    }


@router.get("/api/monitoring/health")
def health():
    cx = _db()
    try:
        n = cx.execute("SELECT COUNT(*) FROM beam_deliveries").fetchone()[0]
        path = _db_path()
    finally:
        cx.close()
    return {"ok": True, "db": path, "beam_deliveries": n}
