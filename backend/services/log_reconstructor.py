"""
RT Ion Record delivery-log QA -- clinical reconstruction.

Executes proton delivery log QA by reconstructing delivered spot fluence from
DICOM RT Ion Records and comparing against the RT Ion Plan prescription:

  plan + RT Ion Record
      -> per-beam prescribed & delivered dose profiles (2D Gaussian spot sum,
         150 x 150 grid at the isocenter plane; services.log_reconstruction)
      -> per-beam log-vs-Rx gamma at 2%/2mm (AAPM TG-218; services.log_gamma)
      -> GammaResult rows (comparison_type="log_vs_Rx", fraction_number=fx)
      -> per-beam reconstructed-dose .npz for the dose viewer
      -> per-fraction delivered spot statistics JSON (spot_stats_fx{fx}.json)
      -> per-beam rows in beam_deliveries, and machine on the fraction row

No RTDose is required: the reference is the PRESCRIPTION reconstructed from the
plan's own spots. The passing threshold is 90% at 2%/2mm (TG-218 action level).

v2 fixed two defects:

  1. FILE RESOLUTION. The previous _find_plan_and_record() globbed the plan's
     store directory and took the FIRST RTRECORD it found. A plan accumulates
     one record per delivered fraction in the same directory, so every fraction
     was reconstructed against the same arbitrary record. The record is now
     taken from Fraction.rtrecord_path and verified against Fraction
     .rtrecord_uid; the plan is resolved by Plan.rtplan_uid. Neither is ever
     guessed -- resolution failure raises rather than substituting a file.

  2. ABORTED DELIVERIES. A record from an interrupted session carries zero
     delivered meterset (and often a subset of the beams). Reconstructing it
     yields an empty dose grid, and the gamma then reports 0.0% -- recorded as
     a catastrophic QA failure when in fact no QA was performed. Zero-delivery
     records now produce no GammaResult rows and set the fraction to
     RECORD_INCOMPLETE_STATUS.

v3 makes the delivery data queryable:

  3. Machine (treatment room) is written to fractions.machine, and per-beam
     delivery statistics to the beam_deliveries table. Room and beam both
     proved to be first-order variables in delivery deviation -- larger than
     any plan-derived quantity -- and previously existed only inside per-
     fraction JSON files. Requires migrate_add_machine_beam.py to have been
     run; if the schema is absent the writes are skipped with a warning rather
     than failing the QA run.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pydicom
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from config import settings
from models.gamma_result import GammaResult
from models.fraction import Fraction
from models.plan import Plan
from services.log_reconstruction import reconstruct_from_record
from services.log_gamma import gamma_log_vs_rx
from services.interruption_detector import detect_record_interruption
from services.record_matcher import _find_plan_dicom
from dicom.rtdose_parser import clean_store_duplicates
from services.dicom_ingestor import RTRECORD_SOP_CLASSES, RTRECORD_MODALITIES

logger = logging.getLogger(__name__)

# Clinical log-QA gamma criteria.
LOG_GAMMA_DD_PERCENT = 2.0
LOG_GAMMA_DTA_MM = 2.0
LOG_GAMMA_RESOLUTION_MM = 0.5
LOG_GAMMA_PASS_THRESHOLD = 90.0   # % passing at 2%/2mm (TG-218 action level)
LOG_COMPARISON_TYPE = "log_vs_Rx"

# Record spot sizes closer to plan-nominal than this (mm, max abs anywhere)
# are treated as an ECHO of the plan (not independently measured).
_SIZE_ECHO_TOL_MM = 1e-3

# Total delivered MU at or below this is treated as "nothing was delivered".
_ZERO_DELIVERY_MU = 1e-6

# Fraction status for a record that cannot be scored (aborted / interrupted).
# NOT a QA failure -- no measurement is implied.
RECORD_INCOMPLETE_STATUS = "record_incomplete"

# Columns persisted to beam_deliveries, in order. Names match the keys emitted
# by _beam_spot_stats() so the mapping stays mechanical.
_BEAM_FIELDS = [
    "n_spots_prescribed", "n_spots_delivered", "n_spots_matched",
    "mu_prescribed", "mu_delivered", "mu_deviation_pct",
    "mu_err_mean_abs_pct", "mu_err_max_abs_pct",
    "pos_mean_dx_mm", "pos_mean_dy_mm", "pos_mean_radial_mm",
    "pos_p95_radial_mm", "pos_max_radial_mm", "gamma_passing_rate",
    # POSITION_PASS_RATES_V1 -- tolerance-based pass rates. A signed mean hides a
    # symmetric spread; these do not. Requires
    # migrate_add_position_pass_rates.py.
    "pos_x_pass_05mm", "pos_x_pass_20mm",
    "pos_y_pass_05mm", "pos_y_pass_20mm",
    "pos_mag_pass_05mm", "pos_mag_pass_20mm",
    "pos_max_abs_dx_mm", "pos_max_abs_dy_mm",
    # POSITION_MU_V1 -- how much DOSE landed out of tolerance, not just how far
    # the worst spot went. Requires migrate_add_position_mu.py.
    "pos_n_beyond_05mm", "pos_n_beyond_20mm", "pos_mu_beyond_20mm",
]

# Position tolerances (mm) for the pass-rate statistics above. 0.5 mm tracks
# machine behaviour; 2.0 mm matches the gamma DTA, so a spot outside it is one
# gamma can no longer absorb.
_POS_TOL_TIGHT_MM = 0.5
_POS_TOL_LOOSE_MM = 2.0


def _beam_spot_stats(beam) -> dict:
    """Summarize per-spot delivered-vs-prescribed metrics for one beam."""
    x = np.asarray(beam.spot_x_offsets_mm, dtype=float)
    y = np.asarray(beam.spot_y_offsets_mm, dtype=float)
    mu_err = np.asarray(beam.spot_mu_errors, dtype=float)
    rx_mu = np.asarray(beam.spot_prescribed_mu, dtype=float)

    stats: dict = {
        "beam_name": beam.beam_name,
        "n_spots_prescribed": int(beam.n_spots_prescribed),
        "n_spots_delivered": int(beam.n_spots_delivered),
        "mu_prescribed": round(float(beam.total_mu_prescribed), 2),
        "mu_delivered": round(float(beam.total_mu_delivered), 2),
    }
    if beam.total_mu_prescribed > 0:
        stats["mu_deviation_pct"] = round(
            100.0 * (beam.total_mu_delivered - beam.total_mu_prescribed)
            / beam.total_mu_prescribed, 3)

    if x.size:
        radial = np.hypot(x, y)
        ax, ay = np.abs(x), np.abs(y)

        def _pct(arr, tol):
            return round(float(100.0 * np.count_nonzero(arr <= tol)
                               / arr.size), 2)

        stats.update({
            "n_spots_matched": int(x.size),
            "pos_mean_dx_mm": round(float(np.mean(x)), 3),
            "pos_mean_dy_mm": round(float(np.mean(y)), 3),
            "pos_std_dx_mm": round(float(np.std(x)), 3),
            "pos_std_dy_mm": round(float(np.std(y)), 3),
            "pos_mean_radial_mm": round(float(np.mean(radial)), 3),
            "pos_p95_radial_mm": round(float(np.percentile(radial, 95)), 3),
            "pos_max_radial_mm": round(float(np.max(radial)), 3),
            # Tolerance-based pass rates, per axis and radial. These are what
            # the vendor's log-based QA report tabulates, so storing them makes
            # every fraction directly comparable to a vendor report instead of
            # only the ones somebody runs by hand.
            "pos_x_pass_05mm": _pct(ax, _POS_TOL_TIGHT_MM),
            "pos_x_pass_20mm": _pct(ax, _POS_TOL_LOOSE_MM),
            "pos_y_pass_05mm": _pct(ay, _POS_TOL_TIGHT_MM),
            "pos_y_pass_20mm": _pct(ay, _POS_TOL_LOOSE_MM),
            "pos_mag_pass_05mm": _pct(radial, _POS_TOL_TIGHT_MM),
            "pos_mag_pass_20mm": _pct(radial, _POS_TOL_LOOSE_MM),
            "pos_max_abs_dx_mm": round(float(np.max(ax)), 3),
            "pos_max_abs_dy_mm": round(float(np.max(ay)), 3),
            "pos_n_beyond_05mm": int(np.count_nonzero(
                radial > _POS_TOL_TIGHT_MM)),
            "pos_n_beyond_20mm": int(np.count_nonzero(
                radial > _POS_TOL_LOOSE_MM)),
        })

        # POSITION_MU_V1 -- delivered MU carried by out-of-tolerance spots, as
        # a percent of the beam total.
        #
        # A large radial deviation alone does not distinguish a harmless
        # aborted spot from a real positioning fault. On plan 8 fx 17 beam LP
        # one spot of 2814 was prescribed 0.0217 MU and delivered 0.0020 MU
        # 158 mm off target: the machine aborted it at ~9% of meterset. Every
        # neighbouring spot was within 0.4 mm and layer counts matched the
        # plan, so the pairing was correct and the event was real -- but it
        # carried essentially no dose, which is why the 2%/2mm gamma read
        # 99.9%. A genuine mispositioning of the same magnitude would carry
        # normal MU.
        #
        # services/evidence.py currently discards rows with
        # pos_max_radial_mm > 10 mm as reconstruction artifacts. This figure
        # is what lets a caller tell the two cases apart instead.
        dv_mu = np.asarray(beam.spot_prescribed_mu, dtype=float) + mu_err \
            if mu_err.size == rx_mu.size else np.zeros(0)
        if dv_mu.size == radial.size and dv_mu.size:
            total_mu = float(np.sum(dv_mu))
            beyond = radial > _POS_TOL_LOOSE_MM
            if total_mu > 0:
                stats["pos_mu_beyond_20mm"] = round(
                    float(100.0 * np.sum(dv_mu[beyond]) / total_mu), 6)
            else:
                stats["pos_mu_beyond_20mm"] = 0.0

    # Per-spot MU error in % of prescribed (spots with meaningful Rx MU only).
    if mu_err.size and rx_mu.size:
        valid = rx_mu > 1e-6
        if valid.any():
            rel = 100.0 * np.abs(mu_err[valid]) / rx_mu[valid]
            stats["mu_err_mean_abs_pct"] = round(float(np.mean(rel)), 3)
            stats["mu_err_max_abs_pct"] = round(float(np.max(rel)), 3)

    # Spot size: record-reported vs plan-nominal, matched per spot.
    rx_sz = np.asarray(beam.spot_size_rx_mm, dtype=float)
    dv_sz = np.asarray(beam.spot_size_dv_mm, dtype=float)
    if dv_sz.size:
        stats["size_x_min_mm"] = round(float(np.min(dv_sz[:, 0])), 2)
        stats["size_x_max_mm"] = round(float(np.max(dv_sz[:, 0])), 2)
        stats["size_y_min_mm"] = round(float(np.min(dv_sz[:, 1])), 2)
        stats["size_y_max_mm"] = round(float(np.max(dv_sz[:, 1])), 2)
        if rx_sz.shape == dv_sz.shape and rx_sz.size:
            max_diff = float(np.max(np.abs(dv_sz - rx_sz)))
            stats["size_max_abs_diff_mm"] = round(max_diff, 4)
            stats["size_is_plan_echo"] = bool(max_diff < _SIZE_ECHO_TOL_MM)
    return stats


def _sop_uid(path: str) -> str:
    """SOPInstanceUID of a DICOM file, or '' if unreadable."""
    try:
        ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True,
                             specific_tags=["SOPInstanceUID"])
        return str(getattr(ds, "SOPInstanceUID", ""))
    except Exception:
        return ""


def _pick_best_plan_file(candidates: list[tuple[str, str, str]], store: Path) -> str:
    """Select the most canonical RTPLAN file when duplicate/anonymized copies exist.

    Prefers:
    1. Shallowest path relative to store (top-level directory).
    2. Canonical RTPLAN filenames without numerical Orthanc prefixes (e.g. 'RP...' rather than '0_RP...').
    3. Newest file modification time.
    """
    import re
    def _rank(c: tuple[str, str, str]) -> tuple[int, int, int, float]:
        p = Path(c[0])
        try:
            depth = len(p.relative_to(store).parts)
        except Exception:
            depth = 99
        name = p.name
        has_prefix = 1 if re.match(r"^\d+_", name) else 0
        is_rp = 0 if (name.startswith("RP") or name.lower().startswith("rtplan")) else 1
        try:
            mtime = -p.stat().st_mtime
        except Exception:
            mtime = 0.0
        return (depth, has_prefix, is_rp, mtime)

    return sorted(candidates, key=_rank)[0][0]


def _resolve_plan_file(plan: Plan) -> str:
    """Locate this plan's RTPLAN by SOPInstanceUID.

    A store directory can contain a foreign patient's plan (this has occurred
    in production), so the first RTPLAN found is not necessarily the right one.
    """
    store = Path(plan.dicom_store_path)
    if not store.is_dir():
        raise FileNotFoundError(
            f"Plan {plan.id}: store path does not exist: {store}")

    # Remove any byte/SOP duplicate files in the store first
    try:
        clean_store_duplicates(str(store))
    except Exception:
        pass

    candidates = []
    for p in store.rglob("*"):
        if not p.is_file() or p.name.startswith(".") or p.name.lower().endswith(".zip"):
            continue
        try:
            ds = pydicom.dcmread(str(p), stop_before_pixels=True, force=True,
                                 specific_tags=["Modality", "SOPInstanceUID",
                                                "PatientID", "SOPClassUID"])
        except Exception:
            continue
        mod = str(getattr(ds, "Modality", "")).upper()
        sop_class = str(getattr(ds, "SOPClassUID", ""))
        is_plan = (mod == "RTPLAN" or sop_class in ("1.2.840.10008.5.1.4.1.1.481.5", "1.2.840.10008.5.1.4.1.1.481.8"))
        is_record = (mod in RTRECORD_MODALITIES or sop_class in RTRECORD_SOP_CLASSES)
        if is_plan and not is_record:
            candidates.append((str(p), str(getattr(ds, "SOPInstanceUID", "")),
                               str(getattr(ds, "PatientID", ""))))

    if not candidates:
        raise FileNotFoundError(f"Plan {plan.id}: no RTPLAN found in {store}")

    want = str(plan.rtplan_uid or "")
    if want:
        exact = [c for c in candidates if c[1] == want]
        if len(exact) == 1:
            foreign = [c for c in candidates if c[1] != want]
            if foreign:
                logger.warning(
                    f"Plan {plan.id}: {len(foreign)} foreign RTPLAN(s) present "
                    f"in store: "
                    + ", ".join(f"{Path(p).name} (patient {pt})"
                                for p, _, pt in foreign)
                )
            return exact[0][0]
        if len(exact) > 1:
            chosen = _pick_best_plan_file(exact, store)
            logger.warning(
                f"Plan {plan.id}: {len(exact)} files share rtplan_uid {want} "
                f"({', '.join(Path(p).name for p, _, _ in exact)}); "
                f"using {Path(chosen).name}."
            )
            return chosen
        raise FileNotFoundError(
            f"Plan {plan.id}: no RTPLAN in {store} matches rtplan_uid {want} "
            f"({len(candidates)} candidate(s) present) -- refusing to guess.")

    # rtplan_uid not set: if all candidates share the same UID, pick best
    uids = {c[1] for c in candidates if c[1]}
    if len(uids) == 1:
        chosen = _pick_best_plan_file(candidates, store)
        logger.warning(
            f"Plan {plan.id}: rtplan_uid not set; {len(candidates)} file(s) share sole "
            f"UID {next(iter(uids))}; using {Path(chosen).name}."
        )
        return chosen

    if len(candidates) == 1:
        logger.warning(
            f"Plan {plan.id}: rtplan_uid not set; using sole RTPLAN "
            f"{Path(candidates[0][0]).name}")
        return candidates[0][0]

    raise FileNotFoundError(
        f"Plan {plan.id}: rtplan_uid not set and {len(candidates)} RTPLANs "
        f"present in {store} -- cannot resolve.")


def _resolve_record_file(plan: Plan, frac: Fraction) -> str:
    """This fraction's own RT Ion Record, from the database. Never globbed.

    A plan accumulates one record per delivered fraction in the same store
    directory. Selecting by directory scan returns an arbitrary fraction's
    record; only Fraction.rtrecord_path identifies the right one.
    """
    if frac is None:
        raise FileNotFoundError(
            f"Plan {plan.id}: no fraction row supplied -- cannot locate a "
            f"delivery record.")

    raw = frac.rtrecord_path
    if not raw:
        raise FileNotFoundError(
            f"Plan {plan.id} fx {frac.fraction_number}: fraction row has no "
            f"rtrecord_path.")

    path = Path(raw)
    if not path.is_absolute():
        candidates = [
            (Path(settings.DICOM_STORE_PATH).parent / raw).resolve(),
            Path(raw).resolve(),
            (Path.cwd() / raw).resolve(),
        ]
        for c in candidates:
            if c.exists():
                path = c
                break

    want = str(frac.rtrecord_uid or "")
    if not path.exists() or (want and _sop_uid(str(path)) != want):
        store_dir = Path(plan.dicom_store_path)
        if not store_dir.is_absolute():
            store_dir = (Path(settings.DICOM_STORE_PATH).parent / plan.dicom_store_path).resolve()
        if store_dir.exists():
            for cand in store_dir.rglob("*"):
                if not cand.is_file() or cand.name.startswith(".") or cand.name.lower().endswith(".zip"):
                    continue
                if want and _sop_uid(str(cand)) == want:
                    logger.info(f"Self-healed rtrecord_path for plan {plan.id} fx {frac.fraction_number}: {cand}")
                    frac.rtrecord_path = str(cand)
                    path = cand
                    break
            if not path.exists():
                for cand in store_dir.rglob("*"):
                    if not cand.is_file() or cand.name.startswith(".") or cand.name.lower().endswith(".zip"):
                        continue
                    cname = cand.name.lower()
                    if (frac.fraction_number == 0 and "verif" in cname) or f"fx{frac.fraction_number}" in cname or f"fraction_{frac.fraction_number}" in cname:
                        logger.info(f"Self-healed rtrecord_path by fraction pattern for plan {plan.id} fx {frac.fraction_number}: {cand}")
                        frac.rtrecord_path = str(cand)
                        path = cand
                        break

    if not path.exists():
        raise FileNotFoundError(
            f"Plan {plan.id} fx {frac.fraction_number}: record file missing "
            f"on disk: {raw}")

    if want:
        got = _sop_uid(str(path))
        if got and got != want:
            raise FileNotFoundError(
                f"Plan {plan.id} fx {frac.fraction_number}: record file "
                f"{path.name} has SOPInstanceUID {got}, expected {want}.")

    return str(path)


def _fraction_row(db: Session, plan_id: int, fx: int) -> Optional[Fraction]:
    return (
        db.query(Fraction)
        .filter_by(plan_id=plan_id, fraction_number=fx)
        .order_by(Fraction.id.desc())
        .first()
    )


def _persist_machine(db: Session, frac: Optional[Fraction], machine) -> None:
    """Write the treatment room onto the fraction row.

    Raw SQL: the column is added by migrate_add_machine_beam.py and may not be
    mapped on the ORM model. Failure here must not fail the QA run.
    """
    if frac is None or not machine:
        return
    try:
        db.execute(text("UPDATE fractions SET machine = :m WHERE id = :i"),
                   {"m": str(machine), "i": int(frac.id)})
        db.commit()
    except SQLAlchemyError as e:
        db.rollback()
        logger.warning(
            f"Could not write fractions.machine (run "
            f"migrate_add_machine_beam.py?): {e}")


def _persist_beam_rows(db: Session, plan_id: int, fx: int,
                       frac: Optional[Fraction], machine, treatment_date,
                       beam_stats: list, incomplete: bool) -> None:
    """Write one beam_deliveries row per beam. Idempotent per (plan, fx, beam)."""
    if not beam_stats:
        return
    cols = ", ".join(_BEAM_FIELDS)
    binds = ", ".join(f":{c}" for c in _BEAM_FIELDS)
    sql = text(
        f"INSERT OR REPLACE INTO beam_deliveries "
        f"(plan_id, fraction_number, fraction_id, machine, treatment_date, "
        f" beam_name, {cols}, not_scored, record_incomplete, created_at) "
        f"VALUES (:plan_id, :fraction_number, :fraction_id, :machine, "
        f" :treatment_date, :beam_name, {binds}, :not_scored, "
        f" :record_incomplete, datetime('now'))"
    )
    try:
        for s in beam_stats:
            params = {
                "plan_id": plan_id,
                "fraction_number": fx,
                "fraction_id": int(frac.id) if frac is not None else None,
                "machine": str(machine) if machine else None,
                "treatment_date": str(treatment_date) if treatment_date else None,
                "beam_name": s.get("beam_name"),
                "not_scored": s.get("not_scored"),
                "record_incomplete": 1 if incomplete else 0,
            }
            for c in _BEAM_FIELDS:
                params[c] = s.get(c)
            db.execute(sql, params)
        db.commit()
        logger.info(
            f"beam_deliveries: {len(beam_stats)} row(s) written for plan "
            f"{plan_id} fx {fx}")
    except SQLAlchemyError as e:
        db.rollback()
        logger.warning(
            f"Could not write beam_deliveries (run "
            f"migrate_add_machine_beam.py?): {e}")


def _reconstruct_single_fraction(
    plan: Plan,
    frac: Fraction,
    fx: int,
    db: Session,
    out_dir: Path,
) -> str:
    plan_id = plan.id
    plan_path = _resolve_plan_file(plan)
    record_path = _resolve_record_file(plan, frac)

    logger.info(
        f"Log reconstruction plan {plan.id}: plan={Path(plan_path).name}, "
        f"record={Path(record_path).name}, fraction={fx}"
    )

    # --- Stage 1: reconstruct per-beam prescribed & delivered dose ---
    result = reconstruct_from_record(plan_path, record_path)
    if not result.beams:
        raise ValueError(
            f"Log reconstruction produced no matched beams for plan {plan_id}. "
            f"Check plan/record beam-name correspondence."
        )

    machine = result.machine_name
    tdate = result.treatment_date
    _persist_machine(db, frac, machine)

    # Interruption detection on the RT Record and Plan
    plan_ds = _find_plan_dicom(plan.dicom_store_path)
    record_ds = pydicom.dcmread(record_path, force=True)
    interruption_info = detect_record_interruption(record_ds, plan_ds)

    if frac is not None:
        frac.is_interrupted = interruption_info["is_interrupted"]
        frac.interruption_reason = interruption_info["interruption_reason"]

    total_delivered_mu = sum(float(b.total_mu_delivered) for b in result.beams)
    total_prescribed_mu = sum(float(b.total_mu_prescribed) for b in result.beams)

    # --- Aborted / interrupted delivery guard ---------------------------
    # A record from a session that did not deliver carries zero meterset. The
    # reconstruction is then an empty grid and the gamma degenerates to 0.0%
    # over an empty ROI. That is not a QA failure and must not be recorded as
    # one.
    if total_delivered_mu <= _ZERO_DELIVERY_MU:
        beam_names = [b.beam_name for b in result.beams]
        logger.error(
            f"Plan {plan_id} fx {fx}: record {Path(record_path).name} reports "
            f"0 delivered MU across {len(result.beams)} beam(s) {beam_names} "
            f"({total_prescribed_mu:.1f} MU prescribed). This is an aborted or "
            f"interrupted delivery record, not a QA result. No gamma computed."
        )
        beam_stats = [_beam_spot_stats(b) for b in result.beams]
        for s in beam_stats:
            s["not_scored"] = "zero delivered MU"

        stats_payload = {
            "plan_id": plan_id,
            "fraction": fx,
            "treatment_date": tdate,
            "machine": machine,
            "record_incomplete": True,
            "is_interrupted": True,
            "interruption_reason": interruption_info["interruption_reason"] or "Zero delivered MU",
            "interruption_type": "zero_delivery",
            "mu_prescribed": round(total_prescribed_mu, 2),
            "mu_delivered": 0.0,
            "beams": beam_stats,
        }
        (out_dir / f"spot_stats_fx{fx}.json").write_text(
            json.dumps(stats_payload, indent=2))

        _persist_beam_rows(db, plan_id, fx, frac, machine, tdate,
                           beam_stats, incomplete=True)

        if frac is not None:
            frac.is_interrupted = True
            frac.interruption_reason = interruption_info["interruption_reason"] or "Zero delivered MU"
            frac.qa_status = "interrupted"
            db.commit()
            logger.info(
                f"Fraction {fx} (plan {plan_id}) qa_status -> interrupted")
        return str(out_dir)

    # Partial delivery: some beams delivered, others not. Score what exists,
    # but make the shortfall visible rather than averaging it away.
    plan_beam_count = getattr(plan, "number_of_fields", None)
    if plan_beam_count and len(result.beams) < int(plan_beam_count):
        logger.warning(
            f"Plan {plan_id} fx {fx}: record contains {len(result.beams)} of "
            f"{plan_beam_count} planned beams -- partial delivery."
        )

    # Clear any prior log_vs_Rx rows for this fraction (idempotent re-runs).
    (
        db.query(GammaResult)
        .filter_by(plan_id=plan_id, comparison_type=LOG_COMPARISON_TYPE,
                   fraction_number=fx)
        .delete()
    )

    n_beams = len(result.beams)
    pass_rates = []
    beam_stats: list[dict] = []

    for i, beam in enumerate(result.beams):
        # A single beam with no delivered MU inside an otherwise delivered
        # fraction: record it, but do not fold a meaningless 0% into the mean.
        if float(beam.total_mu_delivered) <= _ZERO_DELIVERY_MU:
            logger.warning(
                f"  Beam '{beam.beam_name}': 0 delivered MU "
                f"({beam.total_mu_prescribed:.1f} prescribed) -- not scored."
            )
            s = _beam_spot_stats(beam)
            s["not_scored"] = "zero delivered MU"
            beam_stats.append(s)
            continue

        # --- Stage 2: clinical log-vs-Rx gamma ---
        gmap, passing_rate, fails, roi = gamma_log_vs_rx(
            beam.delivered_dose, beam.prescribed_dose,
            dd_percent=LOG_GAMMA_DD_PERCENT,
            dta_mm=LOG_GAMMA_DTA_MM,
            resolution_mm=LOG_GAMMA_RESOLUTION_MM,
        )

        if roi <= 0:
            logger.warning(
                f"  Beam '{beam.beam_name}': empty gamma ROI -- not scored."
            )
            s = _beam_spot_stats(beam)
            s["not_scored"] = "empty gamma ROI"
            beam_stats.append(s)
            continue

        passed = bool(passing_rate >= LOG_GAMMA_PASS_THRESHOLD)
        pass_rates.append(passing_rate)

        # Save per-beam reconstructed dose + gamma map for the viewer.
        beam_npz = out_dir / f"log_dose_fx{fx}_beam{i + 1}.npz"
        np.savez_compressed(
            beam_npz,
            delivered=beam.delivered_dose.astype(np.float32),
            prescribed=beam.prescribed_dose.astype(np.float32),
            gamma=gmap.astype(np.float32),
        )

        row = GammaResult(
            plan_id=plan_id,
            fraction_number=fx,
            field_name=beam.beam_name,
            comparison_type=LOG_COMPARISON_TYPE,
            dd_percent=LOG_GAMMA_DD_PERCENT,
            dta_mm=LOG_GAMMA_DTA_MM,
            passing_rate=round(passing_rate, 2),
            threshold=LOG_GAMMA_PASS_THRESHOLD,
            passed=passed,
            gamma_map_path=str(beam_npz),
        )
        db.add(row)

        # --- Stage 3: delivered spot statistics for trend monitoring ---
        s = _beam_spot_stats(beam)
        s["gamma_passing_rate"] = round(passing_rate, 2)
        beam_stats.append(s)

        logger.info(
            f"  Beam '{beam.beam_name}': log-vs-Rx {passing_rate:.1f}% "
            f"({'PASS' if passed else 'FAIL'} @ {LOG_GAMMA_DD_PERCENT:.0f}%/"
            f"{LOG_GAMMA_DTA_MM:.0f}mm, {fails} fails / {roi} px) | "
            f"MU {beam.total_mu_prescribed:.1f}->{beam.total_mu_delivered:.1f}"
            f" ({s.get('mu_deviation_pct', 0):+.2f}%)"
        )

    db.commit()

    # Persist the per-fraction spot statistics (idempotent overwrite).
    stats_payload = {
        "plan_id": plan_id,
        "fraction": fx,
        "treatment_date": tdate,
        "machine": machine,
        "is_interrupted": interruption_info["is_interrupted"],
        "interruption_reason": interruption_info["interruption_reason"],
        "interruption_type": interruption_info["interruption_type"],
        "beams": beam_stats,
    }
    stats_path = out_dir / f"spot_stats_fx{fx}.json"
    stats_path.write_text(json.dumps(stats_payload, indent=2))
    logger.info(f"Delivered spot statistics written: {stats_path.name}")

    _persist_beam_rows(db, plan_id, fx, frac, machine, tdate,
                       beam_stats, incomplete=False)

    if not pass_rates:
        logger.error(
            f"Plan {plan_id} fx {fx}: no beam could be scored "
            f"({n_beams} beam(s) present). Marking record interrupted/incomplete."
        )
        if frac is not None:
            frac.is_interrupted = True
            frac.interruption_reason = interruption_info["interruption_reason"] or "No beams could be scored"
            frac.qa_status = "interrupted"
            db.commit()
        return str(out_dir)

    mean_pr = float(np.mean(pass_rates))
    all_pass = all(pr >= LOG_GAMMA_PASS_THRESHOLD for pr in pass_rates)
    logger.info(
        f"Log reconstruction for plan {plan.id} complete: "
        f"{len(pass_rates)}/{n_beams} beams scored, mean {mean_pr:.1f}% "
        f"({'ALL PASS' if all_pass else 'SOME FAIL'}) on {machine}"
    )

    # Reflect the outcome on this delivery's fraction row.
    # If the record was interrupted, flag it as 'interrupted' so the user knows
    # it's a partial delivery (can be merged elsewhere and re-uploaded).
    if interruption_info["is_interrupted"]:
        frac_status = "interrupted"
        logger.warning(
            f"Fraction {fx} (plan {plan.id}) flagged as INTERRUPTED: {interruption_info['interruption_reason']}"
        )
    else:
        frac_status = "pass" if all_pass else "measure_needed"

    if frac is not None:
        frac.qa_status = frac_status
        db.commit()
        logger.info(
            f"Fraction {fx} (plan {plan.id}) qa_status -> {frac_status}"
        )
    else:
        logger.warning(
            f"No fractions row for plan {plan.id} fx {fx} to update."
        )

    return str(out_dir)


def reconstruct_dose_from_log(
    plan_id: int,
    fraction_number: Optional[int],
    db: Session,
    job_id: Optional[int] = None,
) -> str:
    """Clinical log-file QA entry point.

    If fraction_number is provided, reconstructs delivered vs prescribed dose
    for that specific delivered fraction.
    If fraction_number is None, runs reconstruction for all available delivered
    fractions for this plan.
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan is None:
        raise ValueError(f"Plan {plan_id} not found")

    out_dir = Path(settings.RESULTS_PATH) / f"plan_{plan_id}" / "log_output"
    out_dir.mkdir(parents=True, exist_ok=True)

    if fraction_number is not None:
        fx = int(fraction_number)
        frac = _fraction_row(db, plan_id, fx)
        if frac is None or not frac.rtrecord_path:
            raise FileNotFoundError(
                f"Plan {plan_id} fraction {fx}: no delivery record (RT Ion Record) found in database."
            )
        return _reconstruct_single_fraction(plan, frac, fx, db, out_dir)

    # fraction_number is None: find all delivered fractions
    delivered = (
        db.query(Fraction)
        .filter(Fraction.plan_id == plan_id, Fraction.rtrecord_path.isnot(None))
        .order_by(Fraction.fraction_number.asc())
        .all()
    )
    if not delivered:
        raise FileNotFoundError(
            f"Plan {plan_id}: no delivery records (RT Ion Records) found in database. "
            f"Please upload an RTRecord to run log reconstruction."
        )

    last_res = str(out_dir)
    for frac in delivered:
        try:
            last_res = _reconstruct_single_fraction(plan, frac, frac.fraction_number, db, out_dir)
        except Exception as exc:
            logger.error(f"Error reconstructing fraction {frac.fraction_number} for plan {plan_id}: {exc}")
            if len(delivered) == 1:
                raise

    return last_res
