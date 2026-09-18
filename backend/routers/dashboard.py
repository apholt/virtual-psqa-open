"""
Dashboard aggregation endpoint -- the daily QA overview.

Returns everything the Dashboard page needs in one call:
  - KPI counts (measure required / flagged / cleared / MCsquare running)
  - the action list (one row per plan with its evidence-gate status)
  - evidence coverage across all plans (per gate layer)
  - a recent pipeline-activity feed

The verdict shown here comes from services/gate.py, the same decision logic
routers/monitoring.py uses for the plan detail page, so the two cannot
disagree. It previously came from the retired complexity/ML model, whose
"virtual_approve" was unreachable in practice: it required confidence == high,
which required at least 20 scored plans AND AUC >= 0.80 from a model whose
leave-one-plan-out skill measured at or below zero.

Gate evaluation is batched (services.evidence.batch_gates) because this
endpoint is polled every few seconds by the dashboard page.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import desc
from sqlalchemy.orm import Session

from database import get_db
from models.plan import Plan
from models.qa_job import QAJob
from services import evidence

router = APIRouter(prefix="/api", tags=["dashboard"])

#: Gate layers, in the order they are shown in the coverage card.
GATE_LAYERS = ("secondary_dose", "deliverability", "machine_state",
               "log_verification")

#: Gate status -> KPI bucket. 'incomplete' is counted as flagged: the plan
#: needs attention before it can be cleared, which is what that tile means.
KPI_BUCKET = {
    "measure": "measure_required",
    "escalate": "measure_required",
    "investigate": "flagged",
    "incomplete": "flagged",
    "cleared": "approved",
    "verified": "approved",
}

#: Ordering for the action list: things needing action first.
STATUS_PRIORITY = {
    "escalate": 0,
    "measure": 1,
    "investigate": 2,
    "incomplete": 3,
    "cleared": 4,
    "verified": 5,
}


def _db_path() -> str:
    """Resolve the SQLite file the app uses, matching routers/monitoring.py."""
    import os
    try:
        from database import engine
        url = str(engine.url)
        if url.startswith("sqlite"):
            p = url.split("///", 1)[-1]
            if os.path.exists(p):
                return p
    except Exception:  # noqa: BLE001
        pass
    for c in (os.environ.get("PSQA_DB", ""), os.path.join("data", "psqa.db")):
        if c and os.path.exists(c):
            return c
    return "./data/psqa.db"


@router.get("/dashboard")
def get_dashboard(db: Session = Depends(get_db)):
    plans = db.query(Plan).order_by(desc(Plan.created_at)).all()

    try:
        gates = evidence.batch_gates(_db_path())
    except Exception:  # noqa: BLE001
        # A dashboard that renders without verdicts is better than one that
        # 500s; the plan detail page will still show the gate.
        gates = {}

    rows = []
    kpis = {"measure_required": 0, "flagged": 0, "approved": 0,
            "mcsquare_running": 0, "incomplete": 0}
    coverage = {layer: 0 for layer in GATE_LAYERS}

    for plan in plans:
        g = gates.get(plan.id)
        decision = g["decision"] if g else None
        status = decision.status.value if decision else None

        layers = {l.name: l.status.value for l in decision.layers} \
            if decision else {}
        # A layer counts as covered when it has actually been evaluated --
        # 'unavailable' means the check did not run, which is precisely what
        # the coverage card should surface.
        evaluated = [name for name in GATE_LAYERS
                     if layers.get(name) not in (None, "unavailable")]
        for name in evaluated:
            coverage[name] += 1

        rows.append({
            "plan_id": plan.id,
            "patient_id": plan.patient.patient_id if plan.patient else "",
            "patient_name": plan.patient.patient_name if plan.patient else "",
            "site": plan.treatment_site,
            "plan_label": plan.plan_label,
            "qa_status": plan.qa_status,
            "number_of_fields": plan.number_of_fields,
            "verdict": status,
            "reason": decision.reason if decision else None,
            # GATE_SUMMARY_V1 -- one short clause for the worklist row; the
            # full reason goes in the tooltip.
            "short_reason": (decision.short_reason or decision.reason)
                            if decision else None,
            "dry_run_waived": bool(decision.dry_run_waived) if decision
                              else False,
            "machine": g["machine"] if g else None,
            "fractions_analysed": g["n_deliveries"] if g else 0,
            "evidence_available": evaluated,
            "layers": layers,
            "created_at": plan.created_at,
        })

        bucket = KPI_BUCKET.get(status)
        if bucket:
            kpis[bucket] += 1
        if status == "incomplete":
            kpis["incomplete"] += 1

    kpis["mcsquare_running"] = (
        db.query(QAJob)
        .filter(QAJob.job_type == "mcSquare", QAJob.status == "running")
        .count()
    )

    n_plans = len(rows) or 1
    evidence_coverage = {
        layer: {
            "count": coverage[layer],
            "total": len(rows),
            "pct": round(100 * coverage[layer] / n_plans, 1),
        }
        for layer in GATE_LAYERS
    }

    recent_jobs = db.query(QAJob).order_by(desc(QAJob.id)).limit(15).all()
    activity = []
    for j in recent_jobs:
        plan = db.query(Plan).filter_by(id=j.plan_id).first()
        activity.append({
            "job_id": j.id,
            "plan_id": j.plan_id,
            "plan_label": plan.plan_label if plan else "",
            "job_type": j.job_type,
            "status": j.status,
            "progress": j.progress,
            "timestamp": j.completed_at or j.started_at,
        })

    rows.sort(key=lambda r: (STATUS_PRIORITY.get(r["verdict"], 9),
                             (r["patient_id"] or "")))

    return {
        "kpis": kpis,
        "action_list": rows,
        "evidence_coverage": evidence_coverage,
        "activity": activity,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
