from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict


class PredictionResponse(BaseModel):
    prediction_id: int
    plan_id: int
    pass_probability: float
    confidence: str            # high | moderate | low
    verdict: str               # virtual_approve | flag | measure
    evidence_available: List[str]
    evidence_missing: List[str]
    model_version: str
    feature_vector: Dict[str, float]


class PredictionRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    plan_id: int
    model_version: str
    pass_probability: float
    confidence: str
    verdict: str
    evidence_available: List[str]
    feature_vector: Dict[str, float] = {}
    created_at: datetime
    physical_outcome: Optional[bool] = None
    physical_passing_rate: Optional[float] = None
    outcome_recorded_at: Optional[datetime] = None


class OutcomeRequest(BaseModel):
    physical_outcome: bool                       # did it pass physical QA?
    physical_passing_rate: Optional[float] = None


class ConfusionMatrix(BaseModel):
    tn: int
    fp: int
    fn: int
    tp: int


class ModelMetrics(BaseModel):
    auc: float
    accuracy: float
    sensitivity: float
    specificity: float
    ppv: float
    npv: float
    confusion_matrix: ConfusionMatrix


class ModelPerformance(BaseModel):
    trained: bool
    algorithm: str
    version: str
    n_samples: int
    labelled_outcomes: int
    required_to_train: int = 50
    required_samples: int = 50
    n_plans: Optional[int] = None
    required_plans: Optional[int] = None
    metrics: Optional[ModelMetrics] = None
    feature_importances: Optional[Dict[str, float]] = None
    trained_at: Optional[str] = None
    message: Optional[str] = None


class RetrainResponse(BaseModel):
    status: str
    n_samples: Optional[int] = None
    required: Optional[int] = None
    required_samples: Optional[int] = None
    n_plans: Optional[int] = None
    required_plans: Optional[int] = None
    message: Optional[str] = None
    version: Optional[str] = None
    algorithm: Optional[str] = None
    metrics: Optional[ModelMetrics] = None
    feature_importances: Optional[Dict[str, float]] = None
    target: Optional[str] = None
