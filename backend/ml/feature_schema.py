"""
Canonical feature vector definition for the Virtual PSQA ML prediction engine.

The schema is the single source of truth for:
  - which features the model consumes, and in what order
  - sensible default / neutral values when a feature is not yet available
    (e.g. log-file evidence before the first fraction is delivered)
  - which "evidence layer" each feature belongs to, so the UI can show
    evidence completeness

Keeping this list stable lets us retrain and upgrade the model
(logistic regression -> random forest -> XGBoost) without touching any other
component. New features are appended at the end.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

# Evidence layers -- used for UI completeness display and feature grouping.
LAYER_COMPLEXITY = "complexity"
LAYER_MCSQUARE = "mcSquare"
LAYER_LOG = "log"
LAYER_PHYSICAL = "physical"

ALL_LAYERS = [LAYER_COMPLEXITY, LAYER_MCSQUARE, LAYER_LOG, LAYER_PHYSICAL]


@dataclass(frozen=True)
class Feature:
    name: str
    layer: str
    default: float  # neutral value used when the layer's evidence is unavailable


# Order matters: this is the vector order fed to the model.
FEATURES: List[Feature] = [
    # ---- Plan-level (always available from RTIonPlan) ----
    Feature("n_fields", LAYER_COMPLEXITY, 0.0),
    Feature("n_fractions", LAYER_COMPLEXITY, 0.0),
    Feature("total_spots", LAYER_COMPLEXITY, 0.0),
    Feature("total_layers", LAYER_COMPLEXITY, 0.0),
    Feature("total_mu", LAYER_COMPLEXITY, 0.0),
    # ---- Complexity metrics ----
    Feature("mcs", LAYER_COMPLEXITY, 0.5),
    Feature("sas", LAYER_COMPLEXITY, 0.0),
    Feature("mu_gy", LAYER_COMPLEXITY, 0.0),
    # ---- Field aggregates ----
    Feature("mean_spots_per_layer", LAYER_COMPLEXITY, 0.0),
    Feature("max_spots_per_layer", LAYER_COMPLEXITY, 0.0),
    Feature("mean_energy_range_mev", LAYER_COMPLEXITY, 0.0),
    Feature("mean_mu_per_spot", LAYER_COMPLEXITY, 0.0),
    Feature("mean_field_size_cm2", LAYER_COMPLEXITY, 0.0),
    # ---- MCsquare evidence (vs TPS) ----
    Feature("mcsquare_mean_pr", LAYER_MCSQUARE, 100.0),
    Feature("mcsquare_min_pr", LAYER_MCSQUARE, 100.0),
    Feature("mcsquare_all_pass", LAYER_MCSQUARE, 1.0),
    # ---- Log reconstruction evidence (vs TPS) ----
    Feature("log_mean_pr", LAYER_LOG, 100.0),
    Feature("log_min_pr", LAYER_LOG, 100.0),
    Feature("log_all_pass", LAYER_LOG, 1.0),
    Feature("log_n_fractions", LAYER_LOG, 0.0),
    # ---- Physical measurement evidence ----
    Feature("physical_passing_rate", LAYER_PHYSICAL, 100.0),
    Feature("physical_measured", LAYER_PHYSICAL, 0.0),
]

FEATURE_NAMES: List[str] = [f.name for f in FEATURES]


def default_vector() -> Dict[str, float]:
    """Return a feature dict populated with neutral defaults."""
    return {f.name: f.default for f in FEATURES}


def to_array(features: Dict[str, float]) -> List[float]:
    """Convert a feature dict to an ordered list matching FEATURE_NAMES."""
    defaults = default_vector()
    return [float(features.get(name, defaults[name])) for name in FEATURE_NAMES]


def layers_for_features() -> Dict[str, str]:
    """Map feature name -> evidence layer."""
    return {f.name: f.layer for f in FEATURES}


# ===========================================================================
# Predictive feature set (pre-delivery prediction model)
# ---------------------------------------------------------------------------
# For predicting whether log_vs_Rx will PASS, the model may only use inputs
# knowable BEFORE delivery. That means complexity + MCsquare, and explicitly
# NOT the log layer (that is the prediction target -- you cannot use the answer
# to predict itself) nor the physical layer (a downstream action, unavailable
# at predict time).
# ===========================================================================

# Layers a pre-delivery predictor is allowed to see.
PREDICTIVE_LAYERS = [LAYER_COMPLEXITY, LAYER_MCSQUARE]

PREDICTIVE_FEATURES: List[Feature] = [
    f for f in FEATURES if f.layer in PREDICTIVE_LAYERS
]
PREDICTIVE_FEATURE_NAMES: List[str] = [f.name for f in PREDICTIVE_FEATURES]


def predictive_default_vector() -> Dict[str, float]:
    """Neutral defaults for the predictive (pre-delivery) feature set."""
    return {f.name: f.default for f in PREDICTIVE_FEATURES}


def to_predictive_array(features: Dict[str, float]) -> List[float]:
    """Ordered list matching PREDICTIVE_FEATURE_NAMES (complexity + MCsquare)."""
    defaults = predictive_default_vector()
    return [float(features.get(name, defaults[name]))
            for name in PREDICTIVE_FEATURE_NAMES]
