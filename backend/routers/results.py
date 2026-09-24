"""
Results & dose-data endpoints -- Phase 3.

Serves gamma results, plan dose metadata, and raw 2D dose / gamma planes as
binary float32 buffers (little-endian) for the client-side Canvas renderer.
Array dimensions and scalars travel in response headers.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from database import get_db
from models.gamma_result import GammaResult
from models.plan import Plan
from schemas.dose import DoseSourceMeta, PlanDoseInfo
from schemas.dvh import CalculateDVHRequest, PlanDVHResponse
from schemas.gamma_result import GammaResultResponse
from services.gamma_analysis import (
    _comparison_specs,
    compute_gamma_plane,
    load_plan_doses,
)

router = APIRouter(prefix="/api/results", tags=["results"])

_SOURCE_LABELS = {"tps": "TPS", "mcSquare": "MCsquare", "log": "Log recon"}


@router.get("/plan/{plan_id}", response_model=list[GammaResultResponse])
def get_plan_results(plan_id: int, db: Session = Depends(get_db)):
    """Returns all stored gamma results for a plan, auto-generating composite if missing."""
    try:
        from services.gamma_analysis import ensure_composite_gamma
        ensure_composite_gamma(plan_id, db)
    except Exception:
        pass
    return (
        db.query(GammaResult)
        .filter_by(plan_id=plan_id)
        .order_by(GammaResult.id.desc())
        .all()
    )


@router.get("/plan/{plan_id}/doses", response_model=PlanDoseInfo)
async def get_plan_dose_info(plan_id: int, db: Session = Depends(get_db)):
    """Returns metadata for all available dose sources + available comparisons."""
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    doses = load_plan_doses(plan_id, db)
    sources = []
    for key, grid in doses.items():
        n_z, n_y, n_x = grid.shape
        sources.append(
            DoseSourceMeta(
                source=key,
                n_planes=n_z,
                rows=n_y,
                cols=n_x,
                spacing=list(grid.spacing),
                max_dose=grid.max_dose,
            )
        )

    available = [
        s.name for s in _comparison_specs()
        if s.reference in doses and s.evaluation in doses
    ]
    default_plane = doses["tps"].max_dose_plane_index() if "tps" in doses else 0

    return PlanDoseInfo(
        plan_id=plan_id,
        verdict=plan.qa_status,
        sources=sources,
        available_comparisons=available,
        default_plane=default_plane,
    )


@router.get("/plan/{plan_id}/dose/{source}/plane/{z}")
async def get_dose_plane(
    plan_id: int, source: str, z: int, db: Session = Depends(get_db)
):
    """Returns a 2D dose plane as a little-endian float32 binary buffer (Gy)."""
    doses = load_plan_doses(plan_id, db)
    if source not in doses:
        raise HTTPException(
            status_code=404,
            detail=f"Dose source '{source}' not available. Have: {list(doses)}",
        )
    grid = doses[source]
    plane = grid.plane(z).astype("<f4")
    return Response(
        content=plane.tobytes(),
        media_type="application/octet-stream",
        headers={
            "X-Rows": str(plane.shape[0]),
            "X-Cols": str(plane.shape[1]),
            "X-Max-Dose": str(grid.max_dose),
            "X-Plane-Index": str(max(0, min(z, grid.shape[0] - 1))),
            "Access-Control-Expose-Headers": "X-Rows,X-Cols,X-Max-Dose,X-Plane-Index",
        },
    )


@router.get("/plan/{plan_id}/ct/plane/{z}")
async def get_ct_plane(plan_id: int, z: int, db: Session = Depends(get_db)):
    """
    Planning-CT plane resampled onto the TPS dose grid, as float32 HU.
    Same grid/indexing as the dose planes, so the frontend can composite the
    dose colorwash directly over anatomy. 404 when no CT series is in the
    plan's DICOM store.
    """
    from services.ct_backdrop import get_ct_on_grid

    doses = load_plan_doses(plan_id, db)
    if "tps" not in doses:
        raise HTTPException(status_code=404, detail="TPS dose grid unavailable")
    ct = get_ct_on_grid(plan_id, doses["tps"], db)
    if ct is None:
        raise HTTPException(status_code=404, detail="No CT series in DICOM store")

    plane = ct.plane(max(0, min(z, ct.shape[0] - 1))).astype("<f4")
    return Response(
        content=plane.tobytes(),
        media_type="application/octet-stream",
        headers={
            "X-Rows": str(plane.shape[0]),
            "X-Cols": str(plane.shape[1]),
            "X-Plane-Index": str(max(0, min(z, ct.shape[0] - 1))),
            "Access-Control-Expose-Headers": "X-Rows,X-Cols,X-Plane-Index",
        },
    )


@router.get("/plan/{plan_id}/spot-stats")
async def get_spot_stats(plan_id: int, db: Session = Depends(get_db)):
    """
    Delivered spot statistics per analysed fraction, from the JSON files the
    log reconstructor writes (spot_stats_fx{N}.json). Returns a list ordered
    by fraction number; empty list when no fractions are analysed yet.
    """
    import json as _json
    from pathlib import Path as _Path
    from config import settings as _settings

    out_dir = _Path(_settings.RESULTS_PATH) / f"plan_{plan_id}" / "log_output"
    payloads = []
    if out_dir.exists():
        for f in sorted(out_dir.glob("spot_stats_fx*.json")):
            try:
                payloads.append(_json.loads(f.read_text()))
            except Exception:
                continue
    payloads.sort(key=lambda p: p.get("fraction", 0))
    return payloads


@router.get("/plan/{plan_id}/gamma/{comparison}/plane/{z}")
def get_gamma_plane(
    plan_id: int,
    comparison: str,
    z: int,
    beam: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """
    Live gamma map for a single plane as float32 binary (NaN where excluded).
    Passing rate is returned in the X-Passing-Rate header.
    When beam is specified, evaluates that beam's doses.
    """
    try:
        gamma_map, passing_rate = compute_gamma_plane(
            plan_id, comparison, z, db, beam_number=beam
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    buf = gamma_map.astype("<f4")
    return Response(
        content=buf.tobytes(),
        media_type="application/octet-stream",
        headers={
            "X-Rows": str(buf.shape[0]),
            "X-Cols": str(buf.shape[1]),
            "X-Passing-Rate": str(round(float(passing_rate), 2)),
            "Access-Control-Expose-Headers": "X-Rows,X-Cols,X-Passing-Rate",
        },
    )


@router.get("/plan/{plan_id}/fraction-logs")
async def get_plan_fraction_logs(plan_id: int, db: Session = Depends(get_db)):
    """Returns summary metadata for all analyzed fractions of a plan."""
    from services.fraction_log_analysis import list_plan_fraction_logs
    return list_plan_fraction_logs(plan_id, db)


@router.get("/plan/{plan_id}/fraction-log/{fraction_number}")
async def get_plan_fraction_log(
    plan_id: int, fraction_number: int, db: Session = Depends(get_db)
):
    """
    Returns full clinical fraction delivery log and layer-by-layer spot QA report
    for the specified fraction, matching the standard vendor/clinical report.
    """
    from services.fraction_log_analysis import analyze_fraction_log
    res = analyze_fraction_log(plan_id, fraction_number, db)
    if res is None:
        raise HTTPException(
            status_code=404,
            detail=f"No delivery log found for plan {plan_id} fraction {fraction_number}",
        )
    return res


@router.get("/plan/{plan_id}/couch-trends")
async def get_plan_couch_trends(plan_id: int, db: Session = Depends(get_db)):
    """
    Returns 6-DoF couch position and angle tracking across all delivered fractions.
    Supports beam-by-beam selection, inter-fraction drift (delta from Fx 1), and tolerance thresholds.
    """
    from services.fraction_log_analysis import get_plan_couch_trends
    return get_plan_couch_trends(plan_id, db)


@router.get("/plan/{plan_id}/dvh", response_model=PlanDVHResponse)
async def get_plan_dvh(
    plan_id: int,
    setup_uncertainty_mm: float = 3.0,
    range_uncertainty_pct: float = 3.0,
    num_scenarios: int = 9,
    db: Session = Depends(get_db),
):
    """
    Returns Dose-Volume Histogram (DVH) predictions and robustness analysis
    for openMCsquare secondary verification. Loads cached result or computes on-demand.
    """
    from services.dvh_service import calculate_plan_dvh_and_robustness
    try:
        return calculate_plan_dvh_and_robustness(
            plan_id,
            db,
            setup_uncertainty_mm=setup_uncertainty_mm,
            range_uncertainty_pct=range_uncertainty_pct,
            num_scenarios=num_scenarios,
            force_recompute=False,
        )
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to calculate DVH and robustness: {exc}")


@router.post("/plan/{plan_id}/dvh/calculate", response_model=PlanDVHResponse)
async def calculate_plan_dvh(
    plan_id: int,
    req: CalculateDVHRequest,
    db: Session = Depends(get_db),
):
    """
    Recompute DVH and robustness uncertainty scenarios with custom setup and range uncertainty parameters.
    """
    from services.dvh_service import calculate_plan_dvh_and_robustness
    try:
        return calculate_plan_dvh_and_robustness(
            plan_id,
            db,
            setup_uncertainty_mm=req.setup_uncertainty_mm,
            range_uncertainty_pct=req.range_uncertainty_pct,
            num_scenarios=req.num_scenarios,
            prescription_dose_override=req.prescription_dose_gy,
            force_recompute=True,
        )
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to recalculate DVH and robustness: {exc}")



