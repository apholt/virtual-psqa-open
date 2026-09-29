"""
ML prediction engine for Virtual PSQA.

Predicts, before delivery, whether a plan's LOG-FILE QA (log_vs_Rx) will PASS,
so routine pre-treatment phantom QA can be skipped when confidence is high.

Design (see MANUAL.md §15):
  - TARGET   = log_vs_Rx pass/fail, per beam (rolled up to a plan verdict by the
               worst beam). Automatic labels -- every delivery produces them.
  - FEATURES = pre-delivery-knowable only: plan complexity + MCsquare-vs-TPS
               gamma. NOT the log layer (that is the target) and NOT physical
               (a downstream action). See ml.feature_schema.PREDICTIVE_*.
  - GATES    = predicted-log-QA (learned) AND MCsquare (deterministic physics).
               An MCsquare failure always forces a physical measurement.

Model progression is automatic by training-set size:
  < 200 cases  -> logistic regression  (bootstrap)
  < 500 cases  -> random forest
  >= 500 cases -> gradient boosting

Until enough labelled outcomes accumulate across ENOUGH DISTINCT PLANS the
engine runs in bootstrap mode: it still computes an evidence-based heuristic
probability for display but always returns verdict='measure', confidence='low'.
Distinct plans -- not fraction count -- drive generalization, so training is
gated on plan diversity, not just row count. This is the correct conservative
cold-start behaviour.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from typing import Any, Dict, List, Optional, Tuple

import joblib
import numpy as np
from sqlalchemy.orm import Session

from config import settings
from ml import feature_schema as fs
from models.gamma_result import GammaResult
from models.ml_prediction import MLPrediction
from models.plan import Plan

logger = logging.getLogger(__name__)

_MODEL_FILE = "model.joblib"
_META_FILE = "model_meta.json"
_HISTORY_FILE = "model_history.json"

# Training requires signal spread across at least this many DISTINCT plans.
# Row count alone is misleading (25 fractions of one plan teach one plan).
_MIN_DISTINCT_PLANS = 5

# In-process cache of the loaded model bundle.
_model_cache: Optional[dict] = None


# ---------------------------------------------------------------------------
# Model persistence
# ---------------------------------------------------------------------------

def _model_dir() -> Path:
    d = Path(settings.ML_MODEL_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_model() -> Optional[dict]:
    """Load the model bundle from disk (cached). Returns None if untrained."""
    global _model_cache
    if _model_cache is not None:
        return _model_cache
    path = _model_dir() / _MODEL_FILE
    if not path.exists():
        return None
    try:
        _model_cache = joblib.load(path)
        return _model_cache
    except Exception as exc:
        logger.error(f"Failed to load ML model: {exc}")
        return None


def _save_model(bundle: dict) -> None:
    global _model_cache
    joblib.dump(bundle, _model_dir() / _MODEL_FILE)
    meta = {k: v for k, v in bundle.items() if k not in ("model",)}
    (_model_dir() / _META_FILE).write_text(json.dumps(meta, indent=2, default=str))
    _model_cache = bundle


def _append_history(meta: dict) -> None:
    path = _model_dir() / _HISTORY_FILE
    history: List[dict] = []
    if path.exists():
        try:
            history = json.loads(path.read_text())
        except Exception:
            history = []
    history.append(meta)
    path.write_text(json.dumps(history, indent=2, default=str))


# ---------------------------------------------------------------------------
# Feature assembly
# ---------------------------------------------------------------------------

def assemble_features(plan_id: int, db: Session) -> Tuple[Dict[str, float], List[str]]:
    """
    Build the canonical (full-evidence) feature dict for a plan from all
    available evidence. Used for DISPLAY, the bootstrap heuristic, and stored
    on the prediction row. The trained MODEL consumes only the predictive
    subset (complexity + MCsquare); see _predictive_array_for.
    Returns (features, evidence_available_layers).
    """
    from services.complexity_extractor import extract_features

    features = fs.default_vector()
    evidence: List[str] = []

    # ---- Complexity layer (always available once ingested) ----
    try:
        complexity = extract_features(plan_id, db)
        features.update(complexity)
        evidence.append(fs.LAYER_COMPLEXITY)
    except Exception as exc:
        logger.warning(f"Complexity extraction failed for plan {plan_id}: {exc}")

    # ---- MCsquare vs TPS gamma ----
    mc_rows = (
        db.query(GammaResult)
        .filter_by(plan_id=plan_id, comparison_type="mcSquare_vs_TPS")
        .all()
    )
    if mc_rows:
        prs = [r.passing_rate for r in mc_rows]
        features["mcsquare_mean_pr"] = float(np.mean(prs))
        features["mcsquare_min_pr"] = float(np.min(prs))
        features["mcsquare_all_pass"] = 1.0 if all(r.passed for r in mc_rows) else 0.0
        evidence.append(fs.LAYER_MCSQUARE)

    # ---- Log reconstruction vs Rx gamma (evidence/display only; NOT a model
    #      feature -- it is the prediction target) ----
    log_rows = (
        db.query(GammaResult)
        .filter(
            GammaResult.plan_id == plan_id,
            GammaResult.comparison_type.in_(["log_vs_Rx", "log_vs_TPS"])
        )
        .all()
    )
    if log_rows:
        prs = [r.passing_rate for r in log_rows]
        fracs = {r.fraction_number for r in log_rows if r.fraction_number is not None}
        features["log_mean_pr"] = float(np.mean(prs))
        features["log_min_pr"] = float(np.min(prs))
        features["log_all_pass"] = 1.0 if all(r.passed for r in log_rows) else 0.0
        features["log_n_fractions"] = float(len(fracs)) if fracs else float(len(log_rows))
        evidence.append(fs.LAYER_LOG)

    # ---- Physical measurement (recorded outcome; downstream action, not a
    #      model feature and no longer the training label) ----
    outcome = (
        db.query(MLPrediction)
        .filter(MLPrediction.plan_id == plan_id, MLPrediction.physical_outcome.isnot(None))
        .order_by(MLPrediction.outcome_recorded_at.desc())
        .first()
    )
    if outcome is not None:
        features["physical_measured"] = 1.0
        if outcome.physical_passing_rate is not None:
            features["physical_passing_rate"] = float(outcome.physical_passing_rate)
        evidence.append(fs.LAYER_PHYSICAL)

    return features, evidence


def _norm_beam(name: str) -> str:
    """Normalize a field/beam name for matching ('LA:TX' -> 'LA')."""
    return str(name or "").split(":")[0].strip().upper()


def _mc_beam_map(plan_id: int, db: Session) -> Dict[str, Tuple[float, bool]]:
    """Map normalized beam name -> (MCsquare passing_rate, passed) for a plan."""
    rows = (
        db.query(GammaResult)
        .filter_by(plan_id=plan_id, comparison_type="mcSquare_vs_TPS")
        .all()
    )
    return {_norm_beam(r.field_name): (float(r.passing_rate), bool(r.passed))
            for r in rows}


def _predictive_feature_dict(
    source_feats: Dict[str, float],
    mc: Optional[Tuple[float, bool]],
) -> Dict[str, float]:
    """Build a predictive (complexity + one beam's MCsquare) feature dict.

    Complexity values are taken from source_feats (a complexity dict or a full
    feature dict); MCsquare fields are overridden with this beam's gamma.
    """
    feats = fs.predictive_default_vector()
    for k in feats:
        if k in source_feats:
            feats[k] = source_feats[k]
    if mc is not None:
        pr, passed = mc
        feats["mcsquare_mean_pr"] = pr
        feats["mcsquare_min_pr"] = pr
        feats["mcsquare_all_pass"] = 1.0 if passed else 0.0
    return feats


def _heuristic_probability(features: Dict[str, float], evidence: List[str]) -> float:
    """
    Evidence-based heuristic pass probability for bootstrap mode (no trained
    model yet). Combines available gamma passing rates with a complexity penalty.
    Display/bootstrap only.
    """
    factors: List[float] = []
    if fs.LAYER_MCSQUARE in evidence:
        factors.append(np.clip(features["mcsquare_min_pr"] / 100.0, 0.0, 1.0))
    if fs.LAYER_LOG in evidence:
        factors.append(np.clip(features["log_min_pr"] / 100.0, 0.0, 1.0))
    if fs.LAYER_PHYSICAL in evidence:
        factors.append(np.clip(features["physical_passing_rate"] / 100.0, 0.0, 1.0))

    base = float(np.mean(factors)) if factors else 0.5

    # Complexity modifiers: high SAS and low MCS increase failure risk.
    sas = features.get("sas", 0.0)
    mcs = features.get("mcs", 0.5)
    penalty = 0.15 * min(sas / 0.1, 1.0) + 0.10 * max(0.0, (0.3 - mcs) / 0.3)
    return float(np.clip(base - penalty, 0.01, 0.99))


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------

def _verdict_for(prob: float, confidence: str, mc_all_pass: bool) -> str:
    """Cascade verdict: predicted-log gate combined with the MC dose gate.

    - MCsquare dose FAIL -> 'measure' (MatriXX; no log check verifies dose)
    - MC pass + confident predicted-log PASS -> 'virtual_approve'
          (skip pre-treatment log QA; verify with the real post-Tx log)
    - MC pass otherwise -> 'flag' (run the log-file QA BEFORE treatment)

    With MC passing a prediction never jumps to MatriXX -- the worst
    pre-treatment action is running the log QA first. MatriXX comes only
    from an actual MC dose failure or an actual log-QA failure.
    """
    if not mc_all_pass:
        return "measure"
    if prob >= settings.ML_APPROVE_PROBABILITY and confidence == "high":
        return "virtual_approve"
    return "flag"


def predict(plan_id: int, db: Session) -> dict:
    """
    Score a plan with currently available PRE-DELIVERY evidence (complexity +
    MCsquare), predicting whether its log-file QA will pass. Persists an
    MLPrediction row and returns the prediction payload.
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan is None:
        raise ValueError(f"Plan {plan_id} not found")

    features, evidence = assemble_features(plan_id, db)
    evidence_missing = [l for l in fs.ALL_LAYERS if l not in evidence]
    bundle = _load_model()

    if bundle is None:
        # Bootstrap: heuristic probability, conservative verdict via the
        # same cascade (low confidence -> never virtual_approve). MC dose
        # gate still applies: MC fail or not-yet-run -> 'measure';
        # MC pass -> 'flag' (run the log QA before treatment).
        prob = _heuristic_probability(features, evidence)
        confidence = "low"
        mc_cleared = (fs.LAYER_MCSQUARE in evidence
                      and features.get("mcsquare_all_pass", 0.0) == 1.0)
        verdict = _verdict_for(prob, confidence, mc_cleared)
        model_version = "bootstrap-0"
        return _persist_prediction(
            db, plan_id, model_version, prob, confidence, verdict,
            features, evidence, evidence_missing,
        )

    # Trained model: predict per beam from predictive features, roll up worst.
    model = bundle["model"]
    mc_map = _mc_beam_map(plan_id, db)
    model_version = bundle.get("version", "unknown")

    if not mc_map:
        # No per-beam MCsquare evidence to predict on -- fall back to heuristic.
        prob = _heuristic_probability(features, evidence)
        return _persist_prediction(
            db, plan_id, model_version, prob, "low", "measure",
            features, evidence, evidence_missing,
        )

    try:
        beam_probs = []
        for beam_name, mc_tuple in mc_map.items():
            b_feats = _predictive_feature_dict(features, mc_tuple)
            x_vec = np.asarray([fs.to_predictive_array(b_feats)], dtype=np.float64)
            p = float(model.predict_proba(x_vec)[0, 1])
            beam_probs.append(p)
        prob = float(min(beam_probs))  # worst beam governs the plan
    except Exception as exc:
        logger.error(f"Model inference failed, falling back to heuristic: {exc}")
        prob = _heuristic_probability(features, evidence)
        return _persist_prediction(
            db, plan_id, model_version, prob, "low", "measure",
            features, evidence, evidence_missing,
        )

    mc_all_pass = all(passed for (_pr, passed) in mc_map.values())
    confidence = _confidence(prob, bundle, evidence)
    verdict = _verdict_for(prob, confidence, mc_all_pass)

    return _persist_prediction(
        db, plan_id, model_version, prob, confidence, verdict,
        features, evidence, evidence_missing,
    )


def _confidence(prob: float, bundle: dict, evidence: List[str]) -> str:
    """Confidence from model maturity, decision margin, and evidence completeness."""
    n_plans = bundle.get("n_plans", 0)
    auc = bundle.get("metrics", {}).get("auc", 0.5)
    margin = abs(prob - 0.5)
    n_evidence = len(evidence)

    if n_plans >= 20 and auc >= 0.80 and margin >= 0.30 and n_evidence >= 2:
        return "high"
    if n_plans >= _MIN_DISTINCT_PLANS and auc >= 0.70 and n_evidence >= 1:
        return "moderate"
    return "low"


def _persist_prediction(
    db: Session,
    plan_id: int,
    model_version: str,
    pass_prob: float,
    confidence: str,
    verdict: str,
    features: Dict[str, float],
    evidence: List[str],
    evidence_missing: List[str],
) -> dict:
    row = MLPrediction(
        plan_id=plan_id,
        model_version=model_version,
        pass_probability=round(pass_prob, 4),
        confidence=confidence,
        verdict=verdict,
        feature_vector=json.dumps(features),
        evidence_available=json.dumps(evidence),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    return {
        "prediction_id": row.id,
        "plan_id": plan_id,
        "pass_probability": row.pass_probability,
        "confidence": confidence,
        "verdict": verdict,
        "evidence_available": evidence,
        "evidence_missing": evidence_missing,
        "model_version": model_version,
        "feature_vector": features,
    }


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def _select_estimator(n: int):
    from sklearn.linear_model import LogisticRegression
    if n < 200:
        return LogisticRegression(max_iter=1000, class_weight="balanced"), "logistic_regression"
    if n < 500:
        from sklearn.ensemble import RandomForestClassifier
        return RandomForestClassifier(n_estimators=300, class_weight="balanced", random_state=42), "random_forest"
    from sklearn.ensemble import GradientBoostingClassifier
    return GradientBoostingClassifier(random_state=42), "gradient_boosting"


def _training_data(db: Session, target: str = "log") -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build (X, y, groups) for the ML QA model.

    Target options:
      - 'log' (default): One row per (plan, fraction, beam) log_vs_Rx result:
          X      = plan complexity + that beam's MCsquare gamma (predictive set)
          y      = 1 if that beam's log_vs_Rx passed else 0
          groups = plan_id  (for plan-grouped CV -- fractions of one plan must
                             never straddle train/test)
      - 'mc': One row per (plan, beam) mcSquare_vs_TPS result:
          X      = plan complexity + default MCsquare fields
          y      = 1 if that beam's mcSquare_vs_TPS passed else 0
          groups = plan_id
    """
    from services.complexity_extractor import extract_features

    if target == "mc":
        gamma_rows = (
            db.query(GammaResult)
            .filter_by(comparison_type="mcSquare_vs_TPS")
            .all()
        )
    else:
        gamma_rows = (
            db.query(GammaResult)
            .filter(GammaResult.comparison_type.in_(["log_vs_Rx", "log_vs_TPS"]))
            .all()
        )

    comp_cache: Dict[int, Dict[str, float]] = {}
    mc_cache: Dict[int, Dict[str, Tuple[float, bool]]] = {}

    def complexity_for(plan_id: int) -> Dict[str, float]:
        if plan_id not in comp_cache:
            try:
                comp_cache[plan_id] = extract_features(plan_id, db)
            except Exception as exc:
                logger.warning(f"complexity extract failed plan {plan_id}: {exc}")
                comp_cache[plan_id] = {}
        return comp_cache[plan_id]

    def mc_for(plan_id: int) -> Dict[str, Tuple[float, bool]]:
        if plan_id not in mc_cache:
            mc_cache[plan_id] = _mc_beam_map(plan_id, db)
        return mc_cache[plan_id]

    X, y, groups = [], [], []
    for r in gamma_rows:
        comp = complexity_for(r.plan_id)
        mc = mc_for(r.plan_id).get(_norm_beam(r.field_name))
        feats = _predictive_feature_dict(comp, mc)
        X.append(fs.to_predictive_array(feats))
        y.append(1 if r.passed else 0)
        groups.append(r.plan_id)

    return (np.asarray(X, dtype=np.float64),
            np.asarray(y, dtype=np.int64),
            np.asarray(groups, dtype=np.int64))


def retrain(db: Session, target: str = "log", force: bool = False) -> dict:
    """
    Train on all delivered log_vs_Rx beam results. Uses plan-grouped CV for an
    honest AUC estimate, fits on the full set, and saves the model if data is
    sufficient (enough distinct plans AND both classes present).
    """
    from sklearn.model_selection import cross_val_predict, GroupKFold, StratifiedGroupKFold
    from sklearn.metrics import (
        roc_auc_score, confusion_matrix, accuracy_score,
    )
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    X, y, groups = _training_data(db, target=target)
    n = len(y)
    n_plans = int(len(np.unique(groups))) if n else 0

    min_cases = settings.ML_RETRAIN_MIN_CASES if not force else 5
    min_plans = _MIN_DISTINCT_PLANS if not force else 2

    if n < min_cases or n_plans < min_plans:
        return {
            "status": "insufficient_data",
            "n_samples": n,
            "n_plans": n_plans,
            "required_samples": min_cases,
            "required_plans": min_plans,
            "target": target,
            "message": (
                f"Need >= {min_cases} labelled beam-results "
                f"across >= {min_plans} distinct plans; have {n} across "
                f"{n_plans}. Running in bootstrap mode."
            ),
        }

    unique_classes = np.unique(y)
    if len(unique_classes) < 2:
        class_label = "PASS" if unique_classes[0] == 1 else "FAIL"
        return {
            "status": "single_class",
            "n_samples": n,
            "n_plans": n_plans,
            "target": target,
            "message": (
                f"All {n} labelled cases have the same outcome ({class_label}). "
                "A predictive classifier requires both passing and failing examples "
                "to learn decision boundaries."
            ),
        }

    estimator, algo = _select_estimator(n)
    pipe = Pipeline([("scaler", StandardScaler()), ("clf", estimator)])

    # Plan-grouped CV so no plan appears in both train and test folds.
    n_splits = min(5, n_plans)
    n_splits = max(2, n_splits)
    try:
        cv = StratifiedGroupKFold(n_splits=n_splits)
        proba = cross_val_predict(
            pipe, X, y, groups=groups, cv=cv, method="predict_proba"
        )[:, 1]
    except Exception:
        cv = GroupKFold(n_splits=n_splits)
        proba = cross_val_predict(
            pipe, X, y, groups=groups, cv=cv, method="predict_proba"
        )[:, 1]

    preds = (proba >= 0.5).astype(int)

    auc = float(roc_auc_score(y, proba))
    acc = float(accuracy_score(y, preds))
    tn, fp, fn, tp = confusion_matrix(y, preds, labels=[0, 1]).ravel()
    sensitivity = float(tp / (tp + fn)) if (tp + fn) else 0.0
    specificity = float(tn / (tn + fp)) if (tn + fp) else 0.0
    ppv = float(tp / (tp + fp)) if (tp + fp) else 0.0
    npv = float(tn / (tn + fn)) if (tn + fn) else 0.0

    # Fit final model on all data.
    pipe.fit(X, y)

    version = f"{algo}-{n}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    metrics = {
        "auc": round(auc, 4),
        "accuracy": round(acc, 4),
        "sensitivity": round(sensitivity, 4),
        "specificity": round(specificity, 4),
        "ppv": round(ppv, 4),
        "npv": round(npv, 4),
        "confusion_matrix": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
    }
    importances = _feature_importances(pipe)

    bundle = {
        "model": pipe,
        "algorithm": algo,
        "version": version,
        "n_samples": n,
        "n_plans": n_plans,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
        "feature_importances": importances,
        "feature_names": fs.PREDICTIVE_FEATURE_NAMES,
        "target": f"{target}_pass",
    }
    _save_model(bundle)
    meta = {k: v for k, v in bundle.items() if k != "model"}
    _append_history(meta)

    logger.info(
        f"Retrained model {version}: AUC={auc:.3f} on {n} beam-results "
        f"across {n_plans} plans"
    )
    return {"status": "trained", **meta}


def _feature_importances(pipe) -> Dict[str, float]:
    """Extract feature importances / coefficients keyed by predictive feature name."""
    clf = pipe.named_steps.get("clf")
    out: Dict[str, float] = {}
    try:
        if hasattr(clf, "coef_"):
            vals = np.abs(clf.coef_[0])
        elif hasattr(clf, "feature_importances_"):
            vals = clf.feature_importances_
        else:
            return out
        total = float(np.sum(vals)) or 1.0
        for name, v in zip(fs.PREDICTIVE_FEATURE_NAMES, vals):
            out[name] = round(float(v) / total, 4)
    except Exception:
        pass
    return out


def performance(db: Session, target: str = "log") -> dict:
    """Return current model metadata + training-data summary for ModelInsights."""
    bundle = _load_model()
    X, y, groups = _training_data(db, target=target)
    labelled = int(len(y))
    n_plans = int(len(np.unique(groups))) if labelled else 0
    if bundle is None:
        return {
            "trained": False,
            "algorithm": "bootstrap",
            "version": "bootstrap-0",
            "n_samples": 0,
            "labelled_outcomes": labelled,
            "n_plans": n_plans,
            "required_samples": settings.ML_RETRAIN_MIN_CASES,
            "required_to_train": settings.ML_RETRAIN_MIN_CASES,
            "required_plans": _MIN_DISTINCT_PLANS,
            "metrics": None,
            "feature_importances": None,
            "target": target,
            "message": "No trained model yet -- running in conservative bootstrap mode.",
        }
    meta = {k: v for k, v in bundle.items() if k != "model"}
    meta["trained"] = True
    meta["labelled_outcomes"] = labelled
    meta["n_plans"] = n_plans
    meta["required_samples"] = settings.ML_RETRAIN_MIN_CASES
    meta["required_to_train"] = settings.ML_RETRAIN_MIN_CASES
    meta["required_plans"] = _MIN_DISTINCT_PLANS
    return meta


def history(db: Session) -> List[dict]:
    path = _model_dir() / _HISTORY_FILE
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


# ---------------------------------------------------------------------------
# CLI Runner
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Virtual PSQA ML Prediction Engine CLI")
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # train
    train_p = subparsers.add_parser("train", help="Train / retrain the ML model")
    train_p.add_argument("--target", default="log", choices=["log", "mc"], help="Target metric (log or mc)")
    train_p.add_argument("--force", action="store_true", help="Force train even if below default sample threshold")

    # status
    subparsers.add_parser("status", help="Check ML model status and performance")

    # predict
    pred_p = subparsers.add_parser("predict", help="Predict pass probability for a plan")
    pred_p.add_argument("plan_id", type=int, help="Plan ID to predict")

    # history
    subparsers.add_parser("history", help="Show training history")

    args = parser.parse_args()

    from database import SessionLocal
    db = SessionLocal()
    try:
        if args.command == "train":
            print(f"[*] Retraining ML model (target={args.target}, force={args.force})...")
            res = retrain(db, target=args.target, force=args.force)
            print(json.dumps(res, indent=2, default=str))
        elif args.command == "status" or args.command is None:
            perf = performance(db)
            print("[*] Model Performance & Status:")
            print(json.dumps(perf, indent=2, default=str))
        elif args.command == "predict":
            print(f"[*] Predicting for plan_id={args.plan_id}...")
            res = predict(args.plan_id, db)
            print(json.dumps(res, indent=2, default=str))
        elif args.command == "history":
            hist = history(db)
            print(f"[*] Training history ({len(hist)} runs):")
            print(json.dumps(hist, indent=2, default=str))
    finally:
        db.close()


if __name__ == "__main__":
    main()
