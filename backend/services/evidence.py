"""
services/evidence.py -- assemble the four gate evidence layers from the DB.

services/gate.py is pure decision logic and touches no database. This module is
the adapter: it reads the store, calls the layer modules, and hands gate.py a
finished set of evidence.

Deliberately raw sqlite3 rather than the ORM, matching services/room_spc.py.
These are read-only analytical queries; going through SQLAlchemy would couple
the gate to model definitions without buying anything.

    from services.evidence import plan_gate
    decision = plan_gate(17)
    print(decision.status, decision.reason)

routers/monitoring.py should become a thin wrapper over this, so the gate
thresholds and the per-plan-beam chart construction exist in exactly one place.
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
from typing import Any, Optional

from services import gate, room_spc

try:
    from services import deliverability as _deliverability
except Exception:  # noqa: BLE001 - pydicom may be absent in some contexts
    _deliverability = None  # type: ignore[assignment]

DEFAULT_DB = "./data/psqa.db"

#: comparison_type values written by the pipeline.
MC_COMPARISON = "mcSquare_vs_TPS"
LOG_COMPARISON = "log_vs_Rx"

#: Plan-side TreatmentMachineName -> the record-side rooms that could deliver
#: it. The plan DICOM names a machine class, not a specific room, so before the
#: first delivery the gate cannot know which gantry will treat. Any class left
#: unmapped falls back to "every room seen in the delivery record", which is
#: conservative but never wrong.
#:
#: The fixed-beam room has no deliveries yet, so its record-side name is not
#: known; add it here once it appears.
PLAN_MACHINE_ROOMS: dict[str, list[str]] = {
    "ProNova SC360 GR": ["Gantry_1", "Gantry_2"],
}

#: pos_max_radial_mm above this indicates a spot-matching reconstruction
#: artifact (the 193 mm case), not a real delivery deviation. Such rows are
#: excluded from position statistics. Matches routers/monitoring.py.
MATCH_ARTIFACT_MM = 10.0


# ---------------------------------------------------------------------------
# Plan facts
# ---------------------------------------------------------------------------

def _plan_row(conn: sqlite3.Connection, plan_id: int) -> Optional[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT id, plan_label, number_of_fields, number_of_fractions, "
        "       dicom_store_path, rtplan_uid, qa_status "
        "FROM plans WHERE id = ?", (plan_id,)).fetchone()


def _plan_machine(conn: sqlite3.Connection, plan_id: int) -> Optional[str]:
    """Most recent machine this plan was delivered on."""
    row = conn.execute(
        "SELECT machine FROM beam_deliveries "
        "WHERE plan_id = ? AND machine IS NOT NULL "
        "ORDER BY treatment_date DESC, fraction_number DESC LIMIT 1",
        (plan_id,)).fetchone()
    if row and row[0]:
        return row[0]
    row = conn.execute(
        "SELECT machine FROM fractions "
        "WHERE plan_id = ? AND machine IS NOT NULL "
        "ORDER BY fraction_number DESC LIMIT 1", (plan_id,)).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Layer 1 -- MCsquare vs TPS
# ---------------------------------------------------------------------------

def mc_passing_rates(conn: sqlite3.Connection,
                     plan_id: int) -> tuple[list[float], list[str]]:
    """Latest MCsquare-vs-TPS passing rate per field, and off-criterion fields.

    A field recalculated more than once contributes only its most recent
    result, or a superseded failure would keep blocking the plan forever.

    Results stored at a criterion other than GAMMA_CRITERION are separated out
    rather than mixed in: historical rows in this database were computed at
    3%/3mm as well as 2%/2mm, and comparing those to the same action level
    would silently overstate agreement.
    """
    rows = conn.execute(
        "SELECT field_name, passing_rate, dd_percent, dta_mm "
        "FROM gamma_results g "
        "WHERE plan_id = ? AND comparison_type = ? "
        "  AND id = (SELECT MAX(id) FROM gamma_results "
        "            WHERE plan_id = g.plan_id "
        "              AND comparison_type = g.comparison_type "
        "              AND field_name = g.field_name) "
        "ORDER BY field_name", (plan_id, MC_COMPARISON)).fetchall()
    rates: list[float] = []
    off: list[str] = []
    for r in rows:
        if r[1] is None:
            continue
        dd, dta = r[2], r[3]
        if dd is not None and dta is not None and (
                abs(float(dd) - gate.GAMMA_DD_PERCENT) > 0.01
                or abs(float(dta) - gate.GAMMA_DTA_MM) > 0.01):
            off.append(f"{r[0]} at {float(dd):g}%/{float(dta):g}mm")
            continue
        rates.append(float(r[1]))
    return rates, off


# ---------------------------------------------------------------------------
# Layer 4 -- per-fraction log verification
# ---------------------------------------------------------------------------

def log_deliveries(conn: sqlite3.Connection, plan_id: int) -> list[dict]:
    """Scored beam deliveries for this plan, oldest first."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT fraction_number, beam_name, treatment_date, machine, "
        "       gamma_passing_rate, pos_mean_radial_mm, pos_max_radial_mm, "
        "       mu_deviation_pct "
        "FROM beam_deliveries "
        "WHERE plan_id = ? AND COALESCE(record_incomplete, 0) = 0 "
        "  AND not_scored IS NULL AND gamma_passing_rate IS NOT NULL "
        "ORDER BY treatment_date, fraction_number, id",
        (plan_id,)).fetchall()
    return [dict(r) for r in rows]


def plan_beam_charts(deliveries: list[dict]) -> dict[str, Any]:
    """Per-plan-beam position control charts.

    Within a single plan-beam the geometry is fixed, so raw
    pos_mean_radial_mm is chartable directly -- no residual transform is
    needed here (unlike the room-level charts, where pooling across plans
    would otherwise mix genuinely different geometric baselines).

    Reuses room_spc's robust estimators and practical floor so a plan-level
    signal and a room-level signal mean the same thing.
    """
    by_beam: dict[str, list[dict]] = {}
    for d in deliveries:
        by_beam.setdefault(d["beam_name"], []).append(d)

    charts: dict[str, Any] = {}
    signal_details: list[str] = []
    floor = room_spc.PRACTICAL_FLOOR.get("position_residual_mm", 0.0)

    for beam, rows in sorted(by_beam.items()):
        vals = [r["pos_mean_radial_mm"] for r in rows
                if r["pos_mean_radial_mm"] is not None]
        labels = [f"fx{r['fraction_number']}" for r in rows
                  if r["pos_mean_radial_mm"] is not None]
        dates = [r["treatment_date"] for r in rows
                 if r["pos_mean_radial_mm"] is not None]
        chart = room_spc._build_chart(
            f"beam {beam} mean radial position", "mm", vals, labels, dates,
            floor)
        charts[beam] = chart.to_dict()
        for s in chart.signals:
            signal_details.append(f"beam {beam}: {s}")

    return {"charts": charts, "signals": signal_details}


# ---------------------------------------------------------------------------
# Layer 2 -- deliverability
# ---------------------------------------------------------------------------

def _find_rtplan(store_path: str, rtplan_uid: str) -> Optional[str]:
    """Locate the RT Ion Plan by SOPInstanceUID, not by glob order.

    Resolving by UID rather than 'first .dcm found' is the same correctness
    requirement that applies to record resolution: a store directory can hold
    more than one plan, and picking the wrong one silently checks the wrong
    spot list.
    """
    if not store_path or not os.path.isdir(store_path):
        return None
    try:
        import pydicom
    except ImportError:
        return None
    for path in sorted(glob.glob(os.path.join(store_path, "**", "*.dcm"),
                                 recursive=True)):
        try:
            ds = pydicom.dcmread(path, force=True, stop_before_pixels=True,
                                 specific_tags=["SOPInstanceUID"])
            if str(getattr(ds, "SOPInstanceUID", "")) == str(rtplan_uid):
                return path
        except Exception:  # noqa: BLE001
            continue
    return None


def deliverability_result(store_path: str, rtplan_uid: str) -> Optional[dict]:
    if _deliverability is None:
        return None
    path = _find_rtplan(store_path, rtplan_uid)
    if not path:
        return None
    return _deliverability.check_plan(path)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _machine_state(all_deliveries, delivered_machine: Optional[str],
                   plan_machine: Optional[str],
                   window: int) -> Optional[dict[str, Any]]:
    """Resolve the machine-state layer for a plan.

    Once a plan has been delivered, the room is known and is charted directly.
    Before that, the plan names only a machine class, so every room that could
    deliver it must be in control -- see room_spc.combine_states.
    """
    if delivered_machine:
        return room_spc.room_state(all_deliveries, delivered_machine, window)

    known = sorted({d.machine for d in all_deliveries if d.machine})
    if not known:
        return None
    candidates = PLAN_MACHINE_ROOMS.get(plan_machine or "", None)
    if candidates:
        candidates = [m for m in candidates if m in known] or known
    else:
        candidates = known
    states = [room_spc.room_state(all_deliveries, m, window) for m in candidates]
    label = plan_machine or "all rooms"
    if len(states) == 1:
        return states[0]
    return room_spc.combine_states(states, label)


def plan_evidence(plan_id: int, db_path: str = DEFAULT_DB,
                  window: int = room_spc.DEFAULT_WINDOW) -> dict[str, Any]:
    """Gather every layer's raw evidence for one plan."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        plan = _plan_row(conn, plan_id)
        if plan is None:
            raise ValueError(f"No plan with id {plan_id}")

        mc_rates, mc_off = mc_passing_rates(conn, plan_id)
        deliveries = log_deliveries(conn, plan_id)
        machine = _plan_machine(conn, plan_id)

        beam_info = plan_beam_charts(deliveries)

    finally:
        conn.close()

    deliv = deliverability_result(plan["dicom_store_path"], plan["rtplan_uid"])

    all_deliveries = room_spc.load_deliveries(db_path)
    room = _machine_state(all_deliveries, machine,
                          (deliv or {}).get("machine"), window)

    return {
        "plan_id": plan_id,
        "plan_label": plan["plan_label"],
        "machine": machine,
        "fields_expected": plan["number_of_fields"],
        "plan_fractions": plan["number_of_fractions"],
        "mc_passing_rates": mc_rates,
        "mc_off_criterion": mc_off,
        "deliverability": deliv,
        "room": room,
        "log_passing_rates": [d["gamma_passing_rate"] for d in deliveries],
        "log_deliveries": deliveries,
        "beam_charts": beam_info["charts"],
        "beam_signals": beam_info["signals"],
    }


def pretreatment_layers(plan_id: int, db_path: str = DEFAULT_DB,
                        window: int = room_spc.DEFAULT_WINDOW
                        ) -> tuple[gate.LayerResult, gate.LayerResult,
                                   gate.LayerResult, dict[str, Any]]:
    """Evaluate layers 1-3 only, for callers that already hold log evidence.

    routers/monitoring.py owns its own per-plan-beam chart construction (MAD
    limits, Western Electric rules, the 193 mm artifact filter) which has been
    validated against plan 17. It supplies its own layer-4 result rather than
    having a second implementation here compute a slightly different one.
    """
    ev = plan_evidence(plan_id, db_path, window)
    return (
        gate.evaluate_secondary_dose(ev["mc_passing_rates"],
                                     ev["fields_expected"],
                                     ev.get("mc_off_criterion")),
        gate.evaluate_deliverability(ev["deliverability"]),
        gate.evaluate_machine_state(ev["room"]),
        ev,
    )


def plan_gate(plan_id: int, db_path: str = DEFAULT_DB,
              window: int = room_spc.DEFAULT_WINDOW
              ) -> tuple[gate.GateDecision, dict[str, Any]]:
    """Evaluate the delivery gate for one plan.

    Returns (decision, evidence) so callers can render the supporting detail
    without querying twice.
    """
    ev = plan_evidence(plan_id, db_path, window)
    decision = gate.evaluate(
        mc_passing_rates=ev["mc_passing_rates"],
        fields_expected=ev["fields_expected"],
        mc_off_criterion=ev.get("mc_off_criterion"),
        deliverability_result=ev["deliverability"],
        machine_state=ev["room"],
        log_passing_rates=ev["log_passing_rates"],
        log_control_signal=bool(ev["beam_signals"]),
        log_signal_detail="; ".join(ev["beam_signals"]),
    )
    return decision, ev


# ---------------------------------------------------------------------------
# Batch evaluation (dashboard)
# ---------------------------------------------------------------------------

#: Deliverability results are cached because the dashboard polls every few
#: seconds and the check reads DICOM off disk. A plan's spot list is immutable
#: once ingested, so the only thing that can change a result is the limits
#: file -- hence its mtime is part of the key.
_DELIV_CACHE: dict[tuple, Optional[dict]] = {}


def _cached_deliverability(store_path: str, rtplan_uid: str) -> Optional[dict]:
    try:
        limits_mtime = os.path.getmtime(_deliverability._limits_path()) \
            if _deliverability else 0.0
    except OSError:
        limits_mtime = 0.0
    key = (store_path, rtplan_uid, limits_mtime)
    if key not in _DELIV_CACHE:
        _DELIV_CACHE[key] = deliverability_result(store_path, rtplan_uid)
    return _DELIV_CACHE[key]


def batch_gates(db_path: str = DEFAULT_DB,
                window: int = room_spc.DEFAULT_WINDOW
                ) -> dict[int, dict[str, Any]]:
    """Evaluate the gate for every plan in one pass.

    Doing this per plan would reload the full delivery table and recompute
    every room chart once per plan, on every dashboard poll. Here the
    deliveries load once, each room is charted once, and deliverability is
    cached, so cost is roughly constant in the number of plans.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        plans = conn.execute(
            "SELECT id, plan_label, number_of_fields, number_of_fractions, "
            "       dicom_store_path, rtplan_uid FROM plans").fetchall()

        mc_rows = conn.execute(
            "SELECT plan_id, field_name, passing_rate, dd_percent, dta_mm "
            "FROM gamma_results g "
            "WHERE comparison_type = ? "
            "  AND id = (SELECT MAX(id) FROM gamma_results "
            "            WHERE plan_id = g.plan_id "
            "              AND comparison_type = g.comparison_type "
            "              AND field_name = g.field_name)",
            (MC_COMPARISON,)).fetchall()
        mc_by_plan: dict[int, list[float]] = {}
        mc_off_by_plan: dict[int, list[str]] = {}
        for r in mc_rows:
            if r["passing_rate"] is None:
                continue
            dd, dta = r["dd_percent"], r["dta_mm"]
            if dd is not None and dta is not None and (
                    abs(float(dd) - gate.GAMMA_DD_PERCENT) > 0.01
                    or abs(float(dta) - gate.GAMMA_DTA_MM) > 0.01):
                mc_off_by_plan.setdefault(r["plan_id"], []).append(
                    f"{r['field_name']} at {float(dd):g}%/{float(dta):g}mm")
                continue
            mc_by_plan.setdefault(r["plan_id"], []).append(
                float(r["passing_rate"]))

        # plan_id < 0 marks deliveries retained as machine history after
        # their plan was deleted; they belong to no plan and are excluded here.
        # room_spc still counts them, which is why they were kept.
        del_rows = conn.execute(
            "SELECT plan_id, beam_name, fraction_number, treatment_date, "
            "       machine, gamma_passing_rate, pos_mean_radial_mm, "
            "       pos_max_radial_mm "
            "FROM beam_deliveries "
            "WHERE COALESCE(record_incomplete, 0) = 0 AND not_scored IS NULL "
            "  AND plan_id > 0 "
            "ORDER BY treatment_date, fraction_number, id").fetchall()
    finally:
        conn.close()

    by_plan: dict[int, list[sqlite3.Row]] = {}
    machine_of: dict[int, str] = {}
    for r in del_rows:
        by_plan.setdefault(r["plan_id"], []).append(r)
        if r["machine"]:
            machine_of[r["plan_id"]] = r["machine"]

    all_deliveries = room_spc.load_deliveries(db_path)
    machines = sorted({d.machine for d in all_deliveries if d.machine})
    room_states = {m: room_spc.room_state(all_deliveries, m, window)
                   for m in machines}

    out: dict[int, dict[str, Any]] = {}
    for p in plans:
        pid = p["id"]
        rows = by_plan.get(pid, [])
        rates = [float(r["gamma_passing_rate"]) for r in rows
                 if r["gamma_passing_rate"] is not None]

        # Per-plan-beam signal, artifact rows excluded.
        signals: list[str] = []
        beams: dict[str, list[float]] = {}
        beam_dates: dict[str, list[Optional[str]]] = {}
        for r in rows:
            if r["pos_mean_radial_mm"] is None:
                continue
            if r["pos_max_radial_mm"] is not None and \
               r["pos_max_radial_mm"] > MATCH_ARTIFACT_MM:
                continue
            beams.setdefault(r["beam_name"], []).append(
                float(r["pos_mean_radial_mm"]))
            beam_dates.setdefault(r["beam_name"], []).append(
                r["treatment_date"])
        floor = room_spc.PRACTICAL_FLOOR.get("position_residual_mm", 0.0)
        for beam, vals in beams.items():
            labels = [f"fx{i+1}" for i in range(len(vals))]
            ch = room_spc._build_chart(f"beam {beam}", "mm", vals, labels,
                                       beam_dates[beam], floor)
            signals.extend(f"beam {beam}: {s}" for s in ch.signals)

        machine = machine_of.get(pid)
        deliv = _cached_deliverability(p["dicom_store_path"], p["rtplan_uid"])
        if machine:
            m_state = room_states.get(machine)
        else:
            m_state = _machine_state(all_deliveries, None,
                                     (deliv or {}).get("machine"), window)
        decision = gate.evaluate(
            mc_passing_rates=mc_by_plan.get(pid),
            fields_expected=p["number_of_fields"],
            mc_off_criterion=mc_off_by_plan.get(pid),
            deliverability_result=deliv,
            machine_state=m_state,
            log_passing_rates=rates,
            log_control_signal=bool(signals),
            log_signal_detail="; ".join(signals),
        )
        out[pid] = {
            "decision": decision,
            "machine": machine,
            "n_deliveries": len(rates),
        }
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate the delivery gate.")
    ap.add_argument("plan_id", nargs="?", type=int, default=None)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--window", type=int, default=room_spc.DEFAULT_WINDOW)
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    if args.plan_id is None:
        ids = [r[0] for r in conn.execute("SELECT id FROM plans ORDER BY id")]
    else:
        ids = [args.plan_id]
    conn.close()

    for pid in ids:
        try:
            decision, ev = plan_gate(pid, args.db, args.window)
        except Exception as exc:  # noqa: BLE001
            print(f"plan {pid}: ERROR {exc}")
            continue
        print(f"=== plan {pid} ({ev['plan_label']}) on "
              f"{ev['machine'] or 'unknown machine'} ===")
        print(f"  status : {decision.status.value.upper()}  "
              f"dry_run_waived={decision.dry_run_waived}")
        print(f"  reason : {decision.reason[:150]}")
        for l in decision.layers:
            print(f"    [{l.status.value:11s}] {l.name:18s} {l.detail[:88]}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
