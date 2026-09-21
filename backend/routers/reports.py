"""
QA report endpoints.

GET /api/reports/{plan_id}            -> clinical report (HTML, browser Save-as-PDF)
GET /api/reports/{plan_id}?format=pdf -> true PDF if WeasyPrint is installed,
                                          otherwise falls back to HTML
GET /api/reports/{plan_id}/fractional-trend -> per-fraction gamma trend (JSON)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models.gamma_result import GammaResult
from models.plan import Plan
from services.report_generator import (
    build_report_html,
    build_secondary_dose_report_html,
    build_synthetic_ct_report_html,
    render_pdf,
)

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/{plan_id}")
def generate_report(plan_id: int, format: str = "html", db: Session = Depends(get_db)):
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    html = build_report_html(plan_id, db)

    if format == "pdf":
        pdf = render_pdf(html)
        if pdf is not None:
            return Response(
                content=pdf,
                media_type="application/pdf",
                headers={"Content-Disposition": f'inline; filename="psqa_full_plan_{plan_id}.pdf"'},
            )
        # WeasyPrint not available — gracefully serve HTML instead.

    return HTMLResponse(content=html)


@router.get("/{plan_id}/secondary-dose")
def generate_secondary_dose_report(
    plan_id: int,
    format: str = "html",
    rois: Optional[str] = Query(None, description="Comma-separated ROI numbers to plot on the DVH (e.g. 1,2,5)"),
    db: Session = Depends(get_db),
):
    """
    Generate dedicated clinical QA report for Secondary Dose Calculations (MCsquare vs TPS)
    and 3D Gamma Analysis. Supports HTML (for interactive view/print) and PDF download.
    Allows plotting only selected ROIs separated into Targets and OARs, while keeping the full metrics table.
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    selected_roi_set = None
    if rois:
        try:
            selected_roi_set = {int(x.strip()) for x in rois.split(",") if x.strip()}
        except Exception:
            selected_roi_set = None

    html = build_secondary_dose_report_html(plan_id, db, selected_rois=selected_roi_set)

    if format == "pdf":
        pdf = render_pdf(html)
        if pdf is not None:
            return Response(
                content=pdf,
                media_type="application/pdf",
                headers={"Content-Disposition": f'inline; filename="secondary_dose_qa_plan_{plan_id}.pdf"'},
            )

    return HTMLResponse(content=html)


@router.get("/{plan_id}/synthetic-ct")
def generate_synthetic_ct_report(
    plan_id: int,
    fraction_number: Optional[int] = None,
    format: str = "html",
    db: Session = Depends(get_db),
):
    """
    Generate dedicated clinical QA report for SyntheticQACT Adaptive Dose & Setup Verification.
    Supports HTML (for interactive browser view/print) and PDF download.
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    html = build_synthetic_ct_report_html(plan_id, fraction_number, db)

    if format == "pdf":
        pdf = render_pdf(html)
        if pdf is not None:
            fname = f"synthetic_qact_plan_{plan_id}" + (f"_fx{fraction_number}" if fraction_number else "") + ".pdf"
            return Response(
                content=pdf,
                media_type="application/pdf",
                headers={"Content-Disposition": f'inline; filename="{fname}"'},
            )

    return HTMLResponse(content=html)


@router.get("/{plan_id}/fractional-trend")
def fractional_trend(plan_id: int, db: Session = Depends(get_db)):
    """
    Returns the gamma passing-rate trend across fractions for each comparison,
    plus tolerance/action thresholds, for the FractionalTracker page.
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    rows = (
        db.query(GammaResult)
        .filter_by(plan_id=plan_id)
        .order_by(GammaResult.created_at)
        .all()
    )

    series: dict[str, list] = {}
    thresholds: dict[str, float] = {}
    for r in rows:
        s = series.setdefault(r.comparison_type, [])
        idx = r.fraction_number if r.fraction_number else len(s) + 1
        s.append({
            "fraction": idx,
            "passing_rate": round(r.passing_rate, 2),
            "passed": r.passed,
            "field_name": r.field_name,
            "created_at": r.created_at.isoformat(),
        })
        thresholds[r.comparison_type] = r.threshold

    # Drift detection on the log series: flag if the last 3 points trend down
    # and the latest is within 2% of the action threshold or below it.
    log = series.get("log_vs_Rx", [])
    drift = False
    if len(log) >= 3:
        last3 = [p["passing_rate"] for p in log[-3:]]
        thr = thresholds.get("log_vs_Rx", settings.GAMMA_LOG_VS_TPS_THRESHOLD)
        decreasing = last3[0] > last3[1] > last3[2]
        near_limit = last3[-1] <= thr + 2.0
        drift = decreasing and near_limit

    return {
        "plan_id": plan_id,
        "series": series,
        "thresholds": thresholds,
        "drift_alert": drift,
    }


class ChartCheckCreatePayload(BaseModel):
    fraction_numbers: list[int]
    reviewer_name: str = "Medical Physicist"
    notes: str = ""
    checklist: Optional[list[dict]] = None


@router.get("/{plan_id}/chart-checks")
def list_chart_checks(plan_id: int, db: Session = Depends(get_db)):
    """
    Retrieve all recorded physicist chart checks for a plan, the running tally,
    and the next due check recommendation.
    """
    try:
        from services.chart_check_service import get_plan_chart_checks
        return get_plan_chart_checks(plan_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load chart checks: {exc}")


@router.post("/{plan_id}/chart-checks")
def create_chart_check(
    plan_id: int,
    payload: ChartCheckCreatePayload,
    db: Session = Depends(get_db),
):
    """
    Record a completed weekly/continuing physics chart check in the platform's running tally.
    """
    try:
        from services.chart_check_service import record_chart_check
        check = record_chart_check(
            plan_id=plan_id,
            fraction_numbers=payload.fraction_numbers,
            reviewer_name=payload.reviewer_name,
            notes=payload.notes,
            checklist=payload.checklist,
            db=db,
        )
        return {
            "success": True,
            "check_id": check.id,
            "check_number": check.check_number,
            "fractions_covered": check.fractions_covered,
            "report_url": f"/api/reports/{plan_id}/chart-check-report?check_id={check.id}",
            "pdf_url": f"/api/reports/{plan_id}/chart-check-report?check_id={check.id}&format=pdf",
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to record chart check: {exc}")


@router.delete("/{plan_id}/chart-checks/{check_id}")
def remove_chart_check(plan_id: int, check_id: int, db: Session = Depends(get_db)):
    """Delete a chart check and update sequence numbers."""
    from services.chart_check_service import delete_chart_check
    success = delete_chart_check(plan_id, check_id, db)
    if not success:
        raise HTTPException(status_code=404, detail="Chart check not found")
    return {"success": True}


@router.get("/{plan_id}/chart-check-report")
def generate_chart_check_report(
    plan_id: int,
    check_id: Optional[int] = None,
    fractions: Optional[str] = None,
    reviewer_name: Optional[str] = None,
    notes: Optional[str] = None,
    format: str = "html",
    db: Session = Depends(get_db),
):
    """
    Generate print-ready HTML / PDF Physics Chart Check Report for a given set of fractions.
    Can be generated for a previously recorded check (?check_id=...) or on-the-fly (?fractions=1,2,3).
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    from services.chart_check_service import build_chart_check_report_html
    from models.chart_check import ChartCheck
    import json

    fx_nums = []
    check_num = None
    checklist_data = None

    if check_id is not None:
        c = db.query(ChartCheck).filter_by(id=check_id, plan_id=plan_id).first()
        if not c:
            raise HTTPException(status_code=404, detail="Chart check record not found")
        check_num = c.check_number
        reviewer_name = reviewer_name or c.reviewer_name
        notes = notes if notes is not None else (c.notes or "")
        try:
            checklist_data = json.loads(c.checklist_json) if c.checklist_json else None
        except Exception:
            checklist_data = None
        fx_nums = list(range(c.start_fraction, c.end_fraction + 1))
    elif fractions:
        try:
            fx_nums = [int(x.strip()) for x in fractions.split(",") if x.strip()]
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid fractions parameter. Expected comma-separated integers.")
    else:
        # Default to initial check (fractions 1-3)
        fx_nums = [1, 2, 3]

    html = build_chart_check_report_html(
        plan_id=plan_id,
        fraction_numbers=fx_nums,
        db=db,
        check_number=check_num,
        reviewer_name=reviewer_name,
        notes=notes,
        checklist=checklist_data,
    )

    if format == "pdf":
        pdf = render_pdf(html)
        if pdf is not None:
            c_tag = f"_check_{check_num}" if check_num else f"_fx_{fx_nums[0]}-{fx_nums[-1]}"
            return Response(
                content=pdf,
                media_type="application/pdf",
                headers={"Content-Disposition": f'inline; filename="chart_check_plan_{plan_id}{c_tag}.pdf"'},
            )

    return HTMLResponse(content=html)

