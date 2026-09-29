"""
ML prediction engine API: per-plan predictions, outcome recording,
model retraining and performance/insights.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List, Optional


from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import get_db
from models.ml_prediction import MLPrediction
from models.plan import Plan
from schemas.ml import (
    OutcomeRequest,
    PredictionRecord,
    PredictionResponse,
    RetrainResponse,
    ModelPerformance,
)
from services import ml_predictor

router = APIRouter(prefix="/api", tags=["ml"])


def _to_record(row: MLPrediction) -> dict:
    try:
        evidence = json.loads(row.evidence_available)
    except Exception:
        evidence = []
    try:
        features = json.loads(row.feature_vector)
    except Exception:
        features = {}
    return {
        "id": row.id,
        "plan_id": row.plan_id,
        "model_version": row.model_version,
        "pass_probability": row.pass_probability,
        "confidence": row.confidence,
        "verdict": row.verdict,
        "evidence_available": evidence,
        "feature_vector": features,
        "created_at": row.created_at,
        "physical_outcome": row.physical_outcome,
        "physical_passing_rate": row.physical_passing_rate,
        "outcome_recorded_at": row.outcome_recorded_at,
    }


@router.post("/plans/{plan_id}/predict", response_model=PredictionResponse)
@router.post("/ml/plans/{plan_id}/predict", response_model=PredictionResponse)
def predict_plan(plan_id: int, db: Session = Depends(get_db)):
    """Run an on-demand prediction for a plan with all available evidence."""
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    return ml_predictor.predict(plan_id, db)


@router.get("/plans/{plan_id}/predictions", response_model=List[PredictionRecord])
@router.get("/ml/plans/{plan_id}/predictions", response_model=List[PredictionRecord])
def get_predictions(plan_id: int, db: Session = Depends(get_db)):
    """Prediction history for a plan, newest first."""
    rows = (
        db.query(MLPrediction)
        .filter_by(plan_id=plan_id)
        .order_by(MLPrediction.created_at.desc())
        .all()
    )
    return [_to_record(r) for r in rows]


@router.get("/plans/{plan_id}/prediction", response_model=PredictionRecord)
@router.get("/ml/plans/{plan_id}/prediction", response_model=PredictionRecord)
def get_latest_prediction(plan_id: int, db: Session = Depends(get_db)):
    """Latest prediction for a plan."""
    row = (
        db.query(MLPrediction)
        .filter_by(plan_id=plan_id)
        .order_by(MLPrediction.created_at.desc())
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="No prediction yet for this plan")
    return _to_record(row)


@router.post("/plans/{plan_id}/outcome", response_model=PredictionRecord)
@router.post("/ml/plans/{plan_id}/outcome", response_model=PredictionRecord)
def record_outcome(plan_id: int, body: OutcomeRequest, db: Session = Depends(get_db)):
    """
    Record the physical QA outcome for a plan. Attaches the ground truth to the
    latest prediction so it becomes a training example on the next retrain.
    """
    row = (
        db.query(MLPrediction)
        .filter_by(plan_id=plan_id)
        .order_by(MLPrediction.created_at.desc())
        .first()
    )
    if not row:
        # No prediction yet — generate one first so we have a feature vector.
        plan = db.query(Plan).filter_by(id=plan_id).first()
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")
        ml_predictor.predict(plan_id, db)
        row = (
            db.query(MLPrediction)
            .filter_by(plan_id=plan_id)
            .order_by(MLPrediction.created_at.desc())
            .first()
        )

    row.physical_outcome = body.physical_outcome
    row.physical_passing_rate = body.physical_passing_rate
    row.outcome_recorded_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return _to_record(row)


@router.post("/ml/retrain", response_model=RetrainResponse)
def retrain_model(
    target: str = Query("log", description="Target QA metric: 'log' for log_vs_Rx or 'mc' for mcSquare_vs_TPS"),
    force: bool = Query(False, description="Force retrain even if below sample threshold"),
    db: Session = Depends(get_db),
):
    """Retrain the model on all recorded outcomes (5-fold CV, auto model selection)."""
    return ml_predictor.retrain(db, target=target, force=force)


@router.get("/ml/performance", response_model=ModelPerformance)
def model_performance(
    target: str = Query("log", description="Target QA metric: 'log' or 'mc'"),
    db: Session = Depends(get_db),
):
    """Current model metadata, metrics and feature importances."""
    return ml_predictor.performance(db, target=target)


@router.get("/ml/history")
def model_history(db: Session = Depends(get_db)):
    """History of trained model versions."""
    return ml_predictor.history(db)
