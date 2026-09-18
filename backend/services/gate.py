"""
services/gate.py -- evidence-based delivery gate.

Replaces the retired complexity/ML verdict (services.ml_predictor) as the
single source of truth for "may this plan be treated, and does it still need a
pre-treatment dry run / physical measurement?".

DESIGN
------
Pure logic. This module performs NO database access and has no dependency on
SQLAlchemy, FastAPI or the rest of the application. Callers extract evidence
and hand it in; the module decides. That keeps the thresholds in exactly one
place, importable by both routers/monitoring.py (display) and services/
pipeline.py (pipeline verdict), which previously would have duplicated them.

FOUR EVIDENCE LAYERS
--------------------
  1. secondary_dose  -- MCsquare vs TPS gamma (pre-treatment, computational)
  2. deliverability  -- planned spot list vs commissioned machine envelope
                        (pre-treatment, deterministic rule check)
  3. machine_state   -- room-level SPC in control on the treatment day
  4. log_verification-- per-fraction log_vs_Rx gamma + per-plan-beam SPC
                        (in vivo, accrues during the course)

Layers 1-3 are the PRE-TREATMENT gate: together they decide whether a dry run
may be skipped. Layer 4 is the in-vivo confirmation that replaces the dry run's
evidentiary role, and can escalate a plan mid-course.

FAIL-CLOSED
-----------
Absence of evidence is never clearance. A layer that has not been evaluated is
UNAVAILABLE, and an unavailable pre-treatment layer blocks CLEARED -- the gate
reports INCOMPLETE and names the missing layers. This matters clinically: the
gate must never emit a permissive verdict because a check failed to run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Sequence


# ---------------------------------------------------------------------------
# Thresholds -- the ONLY place these are defined.
# ---------------------------------------------------------------------------

#: The two comparisons use DIFFERENT criteria, deliberately.
#:
#: SECONDARY DOSE (MCsquare vs TPS) is computed on the patient CT, so it
#: carries CT-to-stopping-power conversion uncertainty and heterogeneity
#: modelling differences between two independent dose engines. At 2%/2mm that
#: comparison largely measures engine disagreement rather than a delivery
#: problem, so 3%/3mm is the appropriate criterion there.
#:
#: LOG VERIFICATION (log vs Rx) compares delivered spots against prescribed
#: spots reconstructed by the same engine on the same grid. There is no
#: patient geometry and no second dose algorithm in that comparison, so the
#: tighter 2%/2mm criterion is meaningful and is retained.
#:
#: A passing rate is only interpretable AT the criterion it was computed
#: under: 96% at 3%/3mm is a substantially weaker result than 96% at 2%/2mm.
#: Results carrying any other criterion are refused rather than scored.
GAMMA_CRITERION = "3%/3mm"          # secondary dose calculation
GAMMA_DD_PERCENT = 3.0
GAMMA_DTA_MM = 3.0

#: Log verification criterion, unchanged.
LOG_GAMMA_CRITERION = "2%/2mm"
LOG_GAMMA_DD_PERCENT = 2.0
LOG_GAMMA_DTA_MM = 2.0

#: Passing-rate action level (%). At or above -> acceptable.
GAMMA_ACTION_LEVEL = 90.0

#: Passing rates in [ACTION_LEVEL, MARGINAL_LEVEL) are acceptable but thin.
#: Matches the "marginal <93%" band already used in the log heatmap UI.
GAMMA_MARGINAL_LEVEL = 93.0

#: Fractions needed before a per-plan-beam control chart is considered
#: established (baseline forming below this).
SPC_BASELINE_FRACTIONS = 8


class LayerStatus(str, Enum):
    PASS = "pass"
    MARGINAL = "marginal"
    FAIL = "fail"
    UNAVAILABLE = "unavailable"


class GateStatus(str, Enum):
    #: A pre-treatment layer failed. Physical measurement / dry run required.
    MEASURE = "measure"
    #: Pre-treatment layers all passed, no delivery logs yet.
    #: Eligible to treat WITHOUT a dry run; per-fraction verification required.
    CLEARED = "cleared"
    #: Delivery logs confirm the course is being delivered as prescribed.
    VERIFIED = "verified"
    #: Thin margin or an isolated signal. Review before continuing.
    INVESTIGATE = "investigate"
    #: Log gamma failure or control-chart signal. Escalate to physics.
    ESCALATE = "escalate"
    #: One or more layers not evaluated. No clearance can be given.
    INCOMPLETE = "incomplete"


@dataclass
class LayerResult:
    """Outcome of a single evidence layer.

    detail  -- the full clinical sentence, including per-field enumeration.
               Shown on the plan page and in tooltips.
    summary -- GATE_SUMMARY_V1: one short clause for dense surfaces (worklist
               rows, the report table). Falls back to detail when unset, so an
               evaluator that supplies none still reads sensibly.
    """
    name: str
    status: LayerStatus
    detail: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    @property
    def blocking(self) -> bool:
        return self.status in (LayerStatus.FAIL, LayerStatus.UNAVAILABLE)

    @property
    def short(self) -> str:
        return self.summary or self.detail


@dataclass
class GateDecision:
    status: GateStatus
    reason: str
    layers: list[LayerResult]
    #: True only when every pre-treatment layer (1-3) passed.
    pretreatment_cleared: bool
    #: True when the plan may be treated without a pre-treatment dry run.
    dry_run_waived: bool
    #: Names of layers that could not be evaluated.
    missing_layers: list[str] = field(default_factory=list)
    #: GATE_SUMMARY_V1 -- one short clause for dense surfaces. `reason` stays
    #: the full text; a worklist row that renders `reason` verbatim is 200+
    #: characters wide and crowds out everything beside it. Declared last:
    #: defaulted fields must follow the required ones in a dataclass.
    short_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "short_reason": self.short_reason or self.reason,
            "pretreatment_cleared": self.pretreatment_cleared,
            "dry_run_waived": self.dry_run_waived,
            "missing_layers": list(self.missing_layers),
            "criterion": GAMMA_CRITERION,
            "action_level": GAMMA_ACTION_LEVEL,
            "layers": [
                {
                    "name": l.name,
                    "status": l.status.value,
                    "detail": l.detail,
                    "summary": l.short,
                    "metrics": l.metrics,
                }
                for l in self.layers
            ],
        }


# ---------------------------------------------------------------------------
# Layer 1 -- MCsquare secondary dose calculation (pre-treatment)
# ---------------------------------------------------------------------------

def evaluate_secondary_dose(
    passing_rates: Optional[Sequence[float]],
    fields_expected: Optional[int] = None,
    off_criterion: Optional[Sequence[str]] = None,
) -> LayerResult:
    """Independent Monte Carlo dose check, per field, vs the TPS dose.

    passing_rates -- one gamma passing rate (%) per field, computed AT
                     GAMMA_CRITERION. Results at any other criterion must be
                     excluded by the caller and named in off_criterion.
    fields_expected -- if known, guards against scoring a partial calculation
                     as a pass.
    off_criterion -- fields whose stored result used a different criterion.
                     These block rather than being silently accepted: scoring
                     a 3%/3mm result against a 2%/2mm action level would
                     overstate agreement.
    """
    name = "secondary_dose"
    if off_criterion:
        return LayerResult(
            name, LayerStatus.UNAVAILABLE,
            f"{len(off_criterion)} field(s) were calculated at a different "
            f"gamma criterion than {GAMMA_CRITERION} and cannot be scored "
            f"against the {GAMMA_ACTION_LEVEL:g}% action level "
            f"({', '.join(off_criterion[:4])}). Recalculate at "
            f"{GAMMA_CRITERION}.",
            {"off_criterion": list(off_criterion)},
            summary=(f"{len(off_criterion)} field(s) scored off-criterion; "
                     f"recalculate at {GAMMA_CRITERION}."),
        )
    if not passing_rates:
        return LayerResult(name, LayerStatus.UNAVAILABLE,
                           "MCsquare secondary dose calculation has not run.",
                           summary="Secondary dose not calculated.")

    rates = [float(r) for r in passing_rates]
    if fields_expected is not None and len(rates) < fields_expected:
        return LayerResult(
            name, LayerStatus.UNAVAILABLE,
            f"Only {len(rates)} of {fields_expected} fields calculated.",
            {"fields_calculated": len(rates), "fields_expected": fields_expected},
            summary=f"Only {len(rates)} of {fields_expected} fields calculated.",
        )

    worst = min(rates)
    metrics = {
        "fields": len(rates),
        "min_passing_rate": round(worst, 2),
        "mean_passing_rate": round(sum(rates) / len(rates), 2),
        "failing_fields": sum(1 for r in rates if r < GAMMA_ACTION_LEVEL),
    }
    if worst < GAMMA_ACTION_LEVEL:
        return LayerResult(
            name, LayerStatus.FAIL,
            f"{metrics['failing_fields']} of {len(rates)} field(s) below "
            f"{GAMMA_ACTION_LEVEL:g}% at {GAMMA_CRITERION} "
            f"(worst {worst:.1f}%).",
            metrics,
            summary=(f"{metrics['failing_fields']} field(s) below "
                     f"{GAMMA_ACTION_LEVEL:g}% (worst {worst:.1f}%)."),
        )
    if worst < GAMMA_MARGINAL_LEVEL:
        return LayerResult(
            name, LayerStatus.MARGINAL,
            f"All fields pass, but the worst field ({worst:.1f}%) leaves thin "
            f"margin to the {GAMMA_ACTION_LEVEL:g}% action level.",
            metrics,
            summary=f"Thin margin: worst field {worst:.1f}%.",
        )
    return LayerResult(
        name, LayerStatus.PASS,
        f"All {len(rates)} field(s) at or above {GAMMA_ACTION_LEVEL:g}% "
        f"(worst {worst:.1f}%).",
        metrics,
        summary=f"{len(rates)} field(s) pass, worst {worst:.1f}%.",
    )


# ---------------------------------------------------------------------------
# Layer 2 -- deliverability against the commissioned machine envelope
# ---------------------------------------------------------------------------

def evaluate_deliverability(result: Optional[dict[str, Any]]) -> LayerResult:
    """Deterministic rule check of the planned spot list vs machine limits.

    Expects the payload produced by services.deliverability (not yet built):
        {"checked": True, "violations": [{"rule": str, "detail": str}, ...]}

    Until that module exists this returns UNAVAILABLE, which -- by the
    fail-closed rule -- prevents the gate from reaching CLEARED. That is
    deliberate: this layer is what a dry run proves and a dose calculation
    cannot, so waiving the dry run without it is not defensible.
    """
    name = "deliverability"
    if not result:
        return LayerResult(
            name, LayerStatus.UNAVAILABLE,
            "Deliverability check did not run (module unavailable, or the RT "
            "Ion Plan could not be located in the plan's DICOM store).",
        )

    unconfigured = result.get("unconfigured") or []
    if result.get("error"):
        return LayerResult(
            name, LayerStatus.UNAVAILABLE,
            f"Deliverability check failed: {result['error']}",
            {"error": result["error"]},
            summary="Deliverability check failed to run.",
        )

    if not result.get("checked"):
        # The check ran; some limits are simply not filled in yet. Naming them
        # matters -- "not performed" and "performed, 11 limits unset" call for
        # completely different actions.
        shown = ", ".join(unconfigured[:6])
        more = f" (+{len(unconfigured) - 6} more)" if len(unconfigured) > 6 else ""
        return LayerResult(
            name, LayerStatus.UNAVAILABLE,
            f"{len(unconfigured)} commissioned limit(s) not configured: "
            f"{shown}{more}",
            {"unconfigured": unconfigured,
             "rules_checked": result.get("rules_checked")},
            summary=f"{len(unconfigured)} commissioned limit(s) not configured.",
        )

    violations = result.get("violations") or []
    metrics = {
        "violations": len(violations),
        "rules_checked": result.get("rules_checked"),
    }
    if violations:
        first = violations[0]
        extra = f" (+{len(violations) - 1} more)" if len(violations) > 1 else ""
        return LayerResult(
            name, LayerStatus.FAIL,
            f"{len(violations)} deliverability violation(s): "
            f"{first.get('rule', 'rule')} -- {first.get('detail', '')}{extra}",
            metrics,
            summary=(f"{len(violations)} violation(s), first: "
                     f"{first.get('rule', 'rule')}."),
        )
    return LayerResult(name, LayerStatus.PASS,
                       "Planned spot list within commissioned machine limits.",
                       metrics,
                       summary="Within commissioned limits.")


# ---------------------------------------------------------------------------
# Layer 3 -- room / machine state
# ---------------------------------------------------------------------------

def evaluate_machine_state(state: Optional[dict[str, Any]]) -> LayerResult:
    """Room-level SPC: is the treatment room currently in control?

    Expects the payload produced by the room-level SPC query (not yet built):
        {"machine": str, "in_control": bool, "signals": [str, ...],
         "n_fractions": int}

    This is the layer that speaks to transient machine excursions -- the
    failure mode that plan-level evidence structurally cannot anticipate.
    Per-plan-beam charts cannot substitute: a new plan establishes its baseline
    inside an ongoing excursion and reports itself in control.
    """
    name = "machine_state"
    if not state or state.get("in_control") is None:
        return LayerResult(
            name, LayerStatus.UNAVAILABLE,
            "Room-level control-chart state is not available.",
            summary="Room control state unavailable.",
        )

    machine = state.get("machine") or "room"
    signals = state.get("signals") or []
    metrics = {
        "machine": machine,
        "signals": list(signals),
        "n_fractions": state.get("n_fractions"),
    }
    if not state.get("in_control"):
        detail = "; ".join(str(s) for s in signals) or "out-of-control signal"
        # MARGINAL, not FAIL, and deliberately so.
        #
        # A room-level control-chart signal is a finding about particular
        # DELIVERIES on that machine -- the reason string names the offending
        # plan-beam-fraction. It is not evidence that THIS plan needs a phantom
        # measurement, and the deliveries that raised it may belong to another
        # patient entirely, including one whose plan has since been deleted and
        # whose rows are retained only as machine history.
        #
        # Forcing MEASURE on that basis would make one patient's historical
        # fraction block an unrelated patient's treatment. The signal is
        # surfaced for physics review and the plan is flagged, but clearance is
        # not withdrawn: the per-fraction log verification still checks every
        # actual delivery, which is what would catch a problem in THIS plan.
        return LayerResult(name, LayerStatus.MARGINAL,
                           f"{machine} has a control-chart signal for review: "
                           f"{detail}. Flagged for physics; does not by itself "
                           f"require measurement of this plan.", metrics,
                           summary=f"{machine}: control-chart signal, "
                                   f"flagged for physics.")
    return LayerResult(name, LayerStatus.PASS,
                       f"{machine} in control on the most recent deliveries.",
                       metrics,
                       summary=f"{machine} in control.")


# ---------------------------------------------------------------------------
# Layer 4 -- per-fraction in-vivo log verification
# ---------------------------------------------------------------------------

def evaluate_log_verification(
    fraction_rates: Optional[Sequence[float]],
    control_signal: bool = False,
    signal_detail: str = "",
) -> LayerResult:
    """log_vs_Rx gamma across delivered fractions, plus per-plan-beam SPC.

    fraction_rates -- passing rate (%) per analysed beam-delivery. Empty means
                      nothing has been delivered/reconstructed yet, which is
                      the normal pre-treatment state (not a failure).
    control_signal -- True if any per-plan-beam control chart flagged, even
                      where gamma still passed. This is deliberately allowed to
                      escalate on its own: on plan 17 the position chart
                      flagged beam LP while its gamma was still 96.6-97.4%.
    """
    name = "log_verification"
    if not fraction_rates:
        return LayerResult(name, LayerStatus.UNAVAILABLE,
                           "No delivered fractions analysed yet.",
                           summary="No fractions delivered yet.")

    rates = [float(r) for r in fraction_rates]
    below = [r for r in rates if r < GAMMA_ACTION_LEVEL]
    metrics = {
        "analysed": len(rates),
        "below_action": len(below),
        "min_passing_rate": round(min(rates), 2),
        "mean_passing_rate": round(sum(rates) / len(rates), 2),
        "control_signal": bool(control_signal),
        "baseline_established": len(rates) >= SPC_BASELINE_FRACTIONS,
    }

    if below:
        return LayerResult(
            name, LayerStatus.FAIL,
            f"{len(below)} of {len(rates)} analysed delivery(ies) below "
            f"{GAMMA_ACTION_LEVEL:g}% at {LOG_GAMMA_CRITERION} "
            f"(min {min(rates):.1f}%).",
            metrics,
            summary=(f"{len(below)} of {len(rates)} delivery(ies) below "
                     f"{GAMMA_ACTION_LEVEL:g}% (min {min(rates):.1f}%)."),
        )
    if control_signal:
        return LayerResult(
            name, LayerStatus.MARGINAL,
            signal_detail or "Control-chart signal with gamma still passing.",
            metrics,
            summary="Control-chart signal; gamma still passing.",
        )
    if min(rates) < GAMMA_MARGINAL_LEVEL:
        return LayerResult(
            name, LayerStatus.MARGINAL,
            f"All deliveries pass, but the worst ({min(rates):.1f}%) leaves "
            f"thin margin.",
            metrics,
            summary=f"Thin margin: worst delivery {min(rates):.1f}%.",
        )
    return LayerResult(
        name, LayerStatus.PASS,
        f"{len(rates)} delivery(ies) analysed, all at or above "
        f"{GAMMA_ACTION_LEVEL:g}% at {LOG_GAMMA_CRITERION} "
        f"(min {min(rates):.1f}%).",
        metrics,
        summary=(f"{len(rates)} delivery(ies) verified, "
                 f"min {min(rates):.1f}%."),
    )


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

#: Layers 1-3 constitute the pre-treatment gate.
PRETREATMENT_LAYERS = ("secondary_dose", "deliverability", "machine_state")


def decide(
    secondary_dose: LayerResult,
    deliverability: LayerResult,
    machine_state: LayerResult,
    log_verification: LayerResult,
) -> GateDecision:
    """Combine the four layers into a single gate decision.

    Precedence:
      1. In-vivo failure or control signal beats everything -- a plan already
         under treatment that starts failing is escalated regardless of how
         clean its pre-treatment evidence was.
      2. A failed pre-treatment layer -> MEASURE.
      3. An unavailable pre-treatment layer -> INCOMPLETE (fail-closed).
      4. All pre-treatment layers pass, logs confirm -> VERIFIED.
      5. All pre-treatment layers pass, no logs yet -> CLEARED (dry run waived).

    GATE_MARGINAL_V1 -- a marginal pre-treatment layer is NOT erased by a
    passing log. Previously step 4 returned VERIFIED on log PASS before the
    pre-treatment marginal check ran, so a room control-chart signal vanished
    from the plan's status the moment one fraction passed gamma: P_SV read
    "investigate" on its machine_state signal, then flipped to "verified" with
    the signal nowhere in the verdict.

    A room signal is advisory and does not force measurement, so the plan stays
    cleared to treat (dry_run_waived remains True) -- but it surfaces as
    INVESTIGATE rather than disappearing, which is what "advisory" means: shown
    to a physicist, not acted on automatically.
    """
    layers = [secondary_dose, deliverability, machine_state, log_verification]

    # 1. In-vivo evidence overrides.
    if log_verification.status is LayerStatus.FAIL:
        return GateDecision(
            GateStatus.ESCALATE, log_verification.detail, layers,
            pretreatment_cleared=False, dry_run_waived=False,
            short_reason=log_verification.short,
        )

    pre = [secondary_dose, deliverability, machine_state]
    failed = [l for l in pre if l.status is LayerStatus.FAIL]
    missing = [l for l in pre if l.status is LayerStatus.UNAVAILABLE]

    # 2. Pre-treatment failure.
    if failed:
        return GateDecision(
            GateStatus.MEASURE,
            "Physical measurement required: "
            + " ".join(l.detail for l in failed),
            layers,
            pretreatment_cleared=False, dry_run_waived=False,
            short_reason=" ".join(l.short for l in failed),
        )

    # 3. Fail closed on missing evidence.
    if missing:
        names = [l.name for l in missing]
        return GateDecision(
            GateStatus.INCOMPLETE,
            "Cannot clear this plan: "
            + " ".join(l.detail for l in missing),
            layers,
            pretreatment_cleared=False, dry_run_waived=False,
            missing_layers=names,
            short_reason=" ".join(l.short for l in missing),
        )

    # Pre-treatment gate is satisfied from here on.
    marginal = [l for l in pre if l.status is LayerStatus.MARGINAL]

    # 4. Under treatment.
    if log_verification.status is LayerStatus.MARGINAL:
        return GateDecision(
            GateStatus.INVESTIGATE, log_verification.detail, layers,
            pretreatment_cleared=True, dry_run_waived=True,
            short_reason=log_verification.short,
        )
    if log_verification.status is LayerStatus.PASS:
        # GATE_MARGINAL_V1 -- logs confirm delivery, but do not overwrite a
        # standing pre-treatment signal.
        if marginal:
            return GateDecision(
                GateStatus.INVESTIGATE,
                log_verification.detail
                + " Pre-treatment signal still standing: "
                + " ".join(l.detail for l in marginal),
                layers,
                pretreatment_cleared=True, dry_run_waived=True,
                short_reason=(log_verification.short + " "
                              + " ".join(l.short for l in marginal)),
            )
        return GateDecision(
            GateStatus.VERIFIED, log_verification.detail, layers,
            pretreatment_cleared=True, dry_run_waived=True,
            short_reason=log_verification.short,
        )

    # 5. Pre-treatment only, no logs yet.
    if marginal:
        return GateDecision(
            GateStatus.INVESTIGATE,
            "Pre-treatment checks pass with thin margin: "
            + " ".join(l.detail for l in marginal),
            layers,
            pretreatment_cleared=True, dry_run_waived=True,
            short_reason=" ".join(l.short for l in marginal),
        )
    return GateDecision(
        GateStatus.CLEARED,
        "Secondary dose calculation, deliverability and machine state all "
        "pass. Eligible to treat without a dry run; every fraction is "
        "verified from the delivery log.",
        layers,
        pretreatment_cleared=True, dry_run_waived=True,
        short_reason="Pre-treatment checks pass. No dry run required.",
    )


def evaluate(
    *,
    mc_passing_rates: Optional[Sequence[float]] = None,
    fields_expected: Optional[int] = None,
    mc_off_criterion: Optional[Sequence[str]] = None,
    deliverability_result: Optional[dict[str, Any]] = None,
    machine_state: Optional[dict[str, Any]] = None,
    log_passing_rates: Optional[Sequence[float]] = None,
    log_control_signal: bool = False,
    log_signal_detail: str = "",
) -> GateDecision:
    """Convenience wrapper: raw evidence in, decision out."""
    return decide(
        evaluate_secondary_dose(mc_passing_rates, fields_expected,
                                mc_off_criterion),
        evaluate_deliverability(deliverability_result),
        evaluate_machine_state(machine_state),
        evaluate_log_verification(log_passing_rates, log_control_signal,
                                  log_signal_detail),
    )
