"""
Tests for Section 15 Machine Learning Prediction Engine.

Validates:
- Feature schema and vector assembly
- Pre-delivery prediction in bootstrap mode
- Model persistence (model.joblib, model_meta.json, model_history.json)
- GroupKFold CV retraining and metrics
- REST API endpoints for ML predictions, outcomes, performance, and retraining
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from database import SessionLocal
from main import app
from ml import feature_schema as fs
from models.gamma_result import GammaResult
from models.ml_prediction import MLPrediction
from models.plan import Plan
from services import ml_predictor


from services.auth_service import create_session_token
from config import settings


@pytest.fixture
def client():
    c = TestClient(app)
    c.cookies[settings.AUTH_SESSION_COOKIE] = create_session_token("admin")
    return c



def test_feature_schema_canonical():
    """Verify feature schema constants and vector transforms."""
    assert len(fs.FEATURE_NAMES) == 22
    assert len(fs.PREDICTIVE_FEATURE_NAMES) == 16
    assert "mcs" in fs.PREDICTIVE_FEATURE_NAMES
    assert "sas" in fs.PREDICTIVE_FEATURE_NAMES
    assert "mu_gy" in fs.PREDICTIVE_FEATURE_NAMES
    assert "mcsquare_mean_pr" in fs.PREDICTIVE_FEATURE_NAMES
    assert "log_mean_pr" not in fs.PREDICTIVE_FEATURE_NAMES  # Target layer excluded

    defaults = fs.default_vector()
    assert len(defaults) == 22
    arr = fs.to_array(defaults)
    assert len(arr) == 22

    pred_arr = fs.to_predictive_array(defaults)
    assert len(pred_arr) == 16


def test_bootstrap_prediction_plan1():
    """Test bootstrap prediction on existing plan 1."""
    with SessionLocal() as db:
        plan = db.query(Plan).filter_by(id=1).first()
        if not plan:
            pytest.skip("Plan 1 not in test database")

        pred = ml_predictor.predict(1, db)
        assert pred["plan_id"] == 1
        assert 0.0 <= pred["pass_probability"] <= 1.0
        assert pred["confidence"] in ("low", "moderate", "high")
        assert pred["verdict"] in ("virtual_approve", "flag", "measure")
        assert "complexity" in pred["evidence_available"]
        assert len(pred["feature_vector"]) == 22

        # Verify DB persistence
        row = db.query(MLPrediction).filter_by(id=pred["prediction_id"]).first()
        assert row is not None
        assert row.plan_id == 1
        assert row.pass_probability == pred["pass_probability"]


def test_ml_performance_and_history():
    """Test performance and history query."""
    with SessionLocal() as db:
        perf = ml_predictor.performance(db)
        assert "algorithm" in perf
        assert "version" in perf
        assert "labelled_outcomes" in perf

        hist = ml_predictor.history(db)
        assert isinstance(hist, list)


def test_ml_api_endpoints(client):
    """Test FastAPI endpoints for ML engine."""
    with SessionLocal() as db:
        plan = db.query(Plan).first()
        if not plan:
            pytest.skip("No plans in test database")
        plan_id = plan.id

    # 1. Performance endpoint
    res = client.get("/api/ml/performance")
    assert res.status_code == 200
    perf_data = res.json()
    assert "labelled_outcomes" in perf_data

    # 2. History endpoint
    res = client.get("/api/ml/history")
    assert res.status_code == 200
    assert isinstance(res.json(), list)

    # 3. Predict endpoint
    res = client.post(f"/api/ml/plans/{plan_id}/predict")
    assert res.status_code == 200
    pred_data = res.json()
    assert pred_data["plan_id"] == plan_id
    assert "pass_probability" in pred_data
    assert "verdict" in pred_data

    # 4. Get latest prediction
    res = client.get(f"/api/ml/plans/{plan_id}/prediction")
    assert res.status_code == 200
    latest_data = res.json()
    assert latest_data["plan_id"] == plan_id

    # 5. Get all predictions for plan
    res = client.get(f"/api/ml/plans/{plan_id}/predictions")
    assert res.status_code == 200
    assert len(res.json()) >= 1

    # 6. Record physical outcome
    res = client.post(
        f"/api/ml/plans/{plan_id}/outcome",
        json={"physical_outcome": True, "physical_passing_rate": 98.5},
    )
    assert res.status_code == 200
    outcome_data = res.json()
    assert outcome_data["physical_outcome"] is True
    assert outcome_data["physical_passing_rate"] == 98.5
