"""
Schemas for openMCsquare Robustness Analysis and Dose-Volume Histogram (DVH) predictions.
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class MetricInterval(BaseModel):
    tps: Optional[float] = None
    mc_nominal: float
    mc_min: float
    mc_max: float
    delta_pct: Optional[float] = None


class ROIMetrics(BaseModel):
    d98: MetricInterval
    d95: MetricInterval
    d50: MetricInterval
    d2: MetricInterval
    d_mean: MetricInterval
    d_max: MetricInterval
    d_min: MetricInterval
    v100_pct: Optional[MetricInterval] = None


class DVHCurve(BaseModel):
    dose_bins_gy: list[float]
    tps_volume_pct: Optional[list[float]] = None
    mc_nominal_volume_pct: list[float]
    mc_min_volume_pct: list[float]
    mc_max_volume_pct: list[float]


class ROIDVHData(BaseModel):
    roi_number: int
    name: str
    type: str = "OTHER"  # TARGET | OAR | EXTERNAL | OTHER
    color: str = "#3b82f6"
    volume_cc: float
    is_target: bool = False
    robustness_pass: bool = True
    robustness_note: Optional[str] = None
    metrics: ROIMetrics
    dvh: DVHCurve


class PlanDVHResponse(BaseModel):
    plan_id: int
    calculated_at: str
    setup_uncertainty_mm: float
    range_uncertainty_pct: float
    num_scenarios: int
    scenario_names: list[str]
    prescription_dose_gy: Optional[float] = None
    has_mc_dose: bool
    has_tps_dose: bool
    rois: list[ROIDVHData]


class CalculateDVHRequest(BaseModel):
    setup_uncertainty_mm: float = Field(3.0, ge=0.5, le=10.0, description="Setup uncertainty in mm (e.g. 3.0 mm)")
    range_uncertainty_pct: float = Field(3.0, ge=0.5, le=10.0, description="Range / stopping power uncertainty in % (e.g. 3.0%)")
    num_scenarios: int = Field(9, ge=1, le=21, description="Number of uncertainty scenarios (e.g. 9 or 21)")
    prescription_dose_gy: Optional[float] = Field(None, description="Optional prescription dose override in Gy")
