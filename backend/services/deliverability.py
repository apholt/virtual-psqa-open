"""
services/deliverability.py -- planned spot list vs commissioned machine limits.

This is evidence layer 2 of the delivery gate (services/gate.py). It answers a
question neither the TPS nor the Monte Carlo secondary dose calculation can:
is this plan physically deliverable on this machine, within the envelope the
machine was commissioned over?

This is precisely what a pre-treatment dry run demonstrates and a dose
calculation cannot -- a dose distribution can be perfectly correct and still
require spots the machine cannot deliver. Waiving the dry run without this
check leaves that failure mode uncovered, which is why gate.py refuses to
clear a plan while this layer is unavailable.

DETERMINISTIC, NOT STATISTICAL
------------------------------
Every rule here is a hard comparison against a commissioned limit. There is no
model, no threshold fitted to historical data, and no prediction. A plan either
sits inside the envelope or it does not.

FAIL-CLOSED ON UNCONFIGURED RULES
---------------------------------
A rule whose limits are null in deliverability_limits.json is reported as
UNCONFIGURED and forces checked=False, so the gate treats the whole layer as
unavailable. An unconfigured rule NEVER counts as a pass.

USAGE
-----
    from services.deliverability import check_plan
    result = check_plan("path/to/RTPLAN.dcm")
    # -> {"checked": bool, "violations": [...], "unconfigured": [...], ...}

    # Survey observed values across existing plans to cross-check commissioning:
    python -m services.deliverability survey P:/PSQA
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

_LIMITS_FILENAME = "deliverability_limits.json"

#: Number of distinct limit keys the checker knows about.
N_RULES = 13


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

def _limits_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        _LIMITS_FILENAME)


def load_limits(path: Optional[str] = None) -> dict[str, Any]:
    """Load the commissioned-envelope config. Missing file is fatal-but-soft:
    it yields an all-null config, so every rule reports UNCONFIGURED."""
    path = path or _limits_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        logger.error("Deliverability limits file not found at %s; "
                     "every rule will report as unconfigured.", path)
        return {"defaults": {}, "rooms": {}, "tolerance": {}}
    except (OSError, ValueError) as exc:
        logger.error("Could not read deliverability limits (%s); "
                     "every rule will report as unconfigured.", exc)
        return {"defaults": {}, "rooms": {}, "tolerance": {}}


def limits_for_room(limits: dict[str, Any], machine: Optional[str]) -> dict[str, Any]:
    """Room-specific limits layered over defaults."""
    merged = dict(limits.get("defaults") or {})
    rooms = limits.get("rooms") or {}
    if machine and machine in rooms:
        for key, value in (rooms[machine] or {}).items():
            if not key.startswith("_"):
                merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Plan extraction
# ---------------------------------------------------------------------------

@dataclass
class BeamSpots:
    """Flattened planned delivery for one ion beam."""
    beam_name: str
    machine: Optional[str]
    gantry_angle: Optional[float]
    snout_position: Optional[float]
    range_shifter_ids: list[str] = field(default_factory=list)
    air_gaps_mm: list[float] = field(default_factory=list)
    #: one entry per energy layer: (energy_mev, [(x_mm, y_mm, mu), ...])
    layers: list[tuple[float, list[tuple[float, float, float]]]] = \
        field(default_factory=list)

    @property
    def n_layers(self) -> int:
        return len(self.layers)

    def iter_spots(self) -> Iterable[tuple[float, float, float, float]]:
        """Yield (energy_mev, x_mm, y_mm, mu) for every planned spot."""
        for energy, spots in self.layers:
            for x, y, mu in spots:
                yield energy, x, y, mu


def _beam_meterset(ds, beam_number) -> Optional[float]:
    """Total MU for a beam, from the fraction group's referenced beam."""
    for fg in getattr(ds, "FractionGroupSequence", []) or []:
        for rb in getattr(fg, "ReferencedBeamSequence", []) or []:
            if getattr(rb, "ReferencedBeamNumber", None) == beam_number:
                bm = getattr(rb, "BeamMeterset", None)
                return float(bm) if bm is not None else None
    return None


def extract_beams(ds) -> list[BeamSpots]:
    """Pull the planned spot list out of an RT Ion Plan dataset.

    ScanSpotMetersetWeights are WEIGHTS, not MU. Actual spot MU is
        weight / FinalCumulativeMetersetWeight * BeamMeterset
    so the beam meterset must be resolved from the fraction group. Without it,
    spot MU cannot be computed and MU rules are skipped rather than guessed.
    """
    beams: list[BeamSpots] = []
    for beam in getattr(ds, "IonBeamSequence", []) or []:
        beam_number = getattr(beam, "BeamNumber", None)
        total_mu = _beam_meterset(ds, beam_number)
        final_weight = getattr(beam, "FinalCumulativeMetersetWeight", None)
        scale: Optional[float] = None
        if total_mu is not None and final_weight:
            try:
                scale = float(total_mu) / float(final_weight)
            except (TypeError, ZeroDivisionError):
                scale = None

        bs = BeamSpots(
            beam_name=str(getattr(beam, "BeamName", "") or
                          f"beam {beam_number}"),
            machine=(str(getattr(beam, "TreatmentMachineName", "")) or None),
            gantry_angle=None,
            snout_position=None,
        )

        for rs in getattr(beam, "RangeShifterSequence", []) or []:
            rsid = getattr(rs, "RangeShifterID", None)
            if rsid:
                bs.range_shifter_ids.append(str(rsid))

        for idx, cp in enumerate(getattr(beam, "IonControlPointSequence", []) or []):
            if idx == 0:
                ga = getattr(cp, "GantryAngle", None)
                if ga is not None:
                    bs.gantry_angle = float(ga)
                sp = getattr(cp, "SnoutPosition", None)
                if sp is not None:
                    bs.snout_position = float(sp)

            for rss in getattr(cp, "RangeShifterSettingsSequence", []) or []:
                gap = getattr(rss, "IsocenterToRangeShifterDistance", None)
                if gap is not None:
                    bs.air_gaps_mm.append(float(gap))

            weights = getattr(cp, "ScanSpotMetersetWeights", None)
            posmap = getattr(cp, "ScanSpotPositionMap", None)
            if weights is None or posmap is None:
                continue
            if not isinstance(weights, (list, tuple)):
                weights = [weights]
            # A control point with all-zero weights is the paired "end" point
            # of a layer, not a delivery. Skip it.
            weights = [float(w) for w in weights]
            if not any(w > 0 for w in weights):
                continue

            energy = getattr(cp, "NominalBeamEnergy", None)
            if energy is None:
                continue
            energy = float(energy)

            coords = [float(v) for v in posmap]
            spots: list[tuple[float, float, float]] = []
            for i, w in enumerate(weights):
                if w <= 0:
                    continue
                try:
                    x = coords[2 * i]
                    y = coords[2 * i + 1]
                except IndexError:
                    break
                mu = w * scale if scale is not None else float("nan")
                spots.append((x, y, mu))
            if spots:
                bs.layers.append((energy, spots))

        beams.append(bs)
    return beams


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

#: Explicit marker for "this machine imposes no such limit".
#:
#: Needed because null means "not yet determined" and blocks clearance. Some
#: constraints genuinely do not exist on a given machine -- if the control
#: system caps neither spots per layer nor layers per field, leaving those
#: null would block every plan forever while leaving them at an invented
#: number would be a fabricated limit. Setting "unlimited" records a
#: deliberate decision that the limit does not apply, which is auditable in a
#: way that a guessed number is not.
UNLIMITED = "unlimited"


def _unlimited(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() == UNLIMITED


def _configured(value: Any) -> bool:
    """True only if this limit has been decided.

    A bare None is unconfigured. So is a dict whose bounds are all None, e.g.
    {"min": null, "max": null} -- that shape reads as "configured" to a naive
    check but permits every value, which would be a silent pass. Silent passes
    are the one failure mode this module exists to prevent.

    UNLIMITED counts as configured: it records an explicit decision that the
    machine imposes no such limit.
    """
    if _unlimited(value):
        return True
    if value is None:
        return False
    if isinstance(value, dict):
        bounds = [v for k, v in value.items() if not k.startswith("_")]
        return any(b is not None for b in bounds)
    if isinstance(value, (list, tuple)):
        return len(value) > 0
    return True


def _violation(rule: str, detail: str, **extra: Any) -> dict[str, Any]:
    v = {"rule": rule, "detail": detail}
    v.update(extra)
    return v


def _check_beam(bs: BeamSpots, lim: dict[str, Any],
                tol: dict[str, Any]) -> tuple[list[dict], set[str]]:
    """Run every rule against one beam. Returns (violations, unconfigured)."""
    violations: list[dict] = []
    unconfigured: set[str] = set()

    tol_e = float(tol.get("energy_mev", 0.0) or 0.0)
    tol_p = float(tol.get("position_mm", 0.0) or 0.0)
    tol_mu = float(tol.get("mu", 0.0) or 0.0)
    where = f"beam {bs.beam_name}"

    # --- spot MU ---------------------------------------------------------
    mu_min, mu_max = lim.get("spot_mu_min"), lim.get("spot_mu_max")
    mus = [mu for _, _, _, mu in bs.iter_spots() if mu == mu]  # drop NaN
    have_mu = len(mus) > 0
    if not have_mu:
        unconfigured.add("spot_mu (beam meterset unresolved; MU not computable)")
    else:
        if not _configured(mu_min):
            unconfigured.add("spot_mu_min")
        elif not _unlimited(mu_min):
            low = [m for m in mus if m < float(mu_min) - tol_mu]
            if low:
                violations.append(_violation(
                    "spot_mu_min",
                    f"{where}: {len(low)} spot(s) below minimum deliverable "
                    f"{mu_min} MU (min {min(low):.6g}).", count=len(low)))
        if not _configured(mu_max):
            unconfigured.add("spot_mu_max")
        elif not _unlimited(mu_max):
            high = [m for m in mus if m > float(mu_max) + tol_mu]
            if high:
                violations.append(_violation(
                    "spot_mu_max",
                    f"{where}: {len(high)} spot(s) above maximum "
                    f"{mu_max} MU (max {max(high):.6g}).", count=len(high)))

    # --- field extent ----------------------------------------------------
    ext_x, ext_y = lim.get("field_extent_x_mm"), lim.get("field_extent_y_mm")
    if not _configured(ext_x):
        unconfigured.add("field_extent_x_mm")
        ext_x = None
    elif _unlimited(ext_x):
        ext_x = None
    if not _configured(ext_y):
        unconfigured.add("field_extent_y_mm")
        ext_y = None
    elif _unlimited(ext_y):
        ext_y = None
    if ext_x is not None or ext_y is not None:
        ox = oy = 0
        mx = my = 0.0
        for _, x, y, _ in bs.iter_spots():
            if ext_x is not None and abs(x) > float(ext_x) + tol_p:
                ox += 1
                mx = max(mx, abs(x))
            if ext_y is not None and abs(y) > float(ext_y) + tol_p:
                oy += 1
                my = max(my, abs(y))
        if ox:
            violations.append(_violation(
                "field_extent_x", f"{where}: {ox} spot(s) beyond +/-{ext_x} mm "
                f"in X (max |x| {mx:.1f} mm).", count=ox))
        if oy:
            violations.append(_violation(
                "field_extent_y", f"{where}: {oy} spot(s) beyond +/-{ext_y} mm "
                f"in Y (max |y| {my:.1f} mm).", count=oy))

    # --- energy range ----------------------------------------------------
    e_min, e_max = lim.get("energy_min_mev"), lim.get("energy_max_mev")
    energies = [e for e, _ in bs.layers]
    if not _configured(e_min):
        unconfigured.add("energy_min_mev")
    elif energies and not _unlimited(e_min):
        low = [e for e in energies if e < float(e_min) - tol_e]
        if low:
            violations.append(_violation(
                "energy_min", f"{where}: {len(low)} layer(s) below commissioned "
                f"minimum {e_min} MeV (min {min(low):.2f}).", count=len(low)))
    if not _configured(e_max):
        unconfigured.add("energy_max_mev")
    elif energies and not _unlimited(e_max):
        high = [e for e in energies if e > float(e_max) + tol_e]
        if high:
            violations.append(_violation(
                "energy_max", f"{where}: {len(high)} layer(s) above commissioned "
                f"maximum {e_max} MeV (max {max(high):.2f}).", count=len(high)))

    # --- minimum energy-layer meterset -----------------------------------
    # A layer whose total MU falls below the machine minimum cannot be
    # delivered, even when every individual spot in it is deliverable. This is
    # a distinct failure mode from the per-spot minimum and is exactly the
    # kind of thing a dry run surfaces and a dose calculation does not.
    layer_mu_min = lim.get("layer_mu_min")
    if not _configured(layer_mu_min):
        unconfigured.add("layer_mu_min")
    elif not _unlimited(layer_mu_min) and have_mu:
        low_layers = []
        for energy, spots in bs.layers:
            total = sum(mu for _x, _y, mu in spots if mu == mu)
            if total < float(layer_mu_min) - tol_mu:
                low_layers.append((energy, total))
        if low_layers:
            worst = min(low_layers, key=lambda t: t[1])
            violations.append(_violation(
                "layer_mu_min",
                f"{where}: {len(low_layers)} energy layer(s) below the "
                f"minimum deliverable layer meterset {layer_mu_min} MU "
                f"(worst {worst[1]:.4g} MU at {worst[0]:.2f} MeV).",
                count=len(low_layers)))

    # --- spots per layer -------------------------------------------------
    spl_max = lim.get("spots_per_layer_max")
    if not _configured(spl_max):
        unconfigured.add("spots_per_layer_max")
    elif not _unlimited(spl_max):
        over = [(e, len(s)) for e, s in bs.layers if len(s) > int(spl_max)]
        if over:
            worst = max(over, key=lambda t: t[1])
            violations.append(_violation(
                "spots_per_layer_max",
                f"{where}: {len(over)} layer(s) exceed {spl_max} spots "
                f"(worst {worst[1]} at {worst[0]:.2f} MeV).", count=len(over)))

    # --- energy layers per field ----------------------------------------
    lay_max = lim.get("energy_layers_per_field_max")
    if not _configured(lay_max):
        unconfigured.add("energy_layers_per_field_max")
    elif not _unlimited(lay_max) and bs.n_layers > int(lay_max):
        violations.append(_violation(
            "energy_layers_per_field_max",
            f"{where}: {bs.n_layers} energy layers exceeds maximum {lay_max}."))

    # --- gantry angle ----------------------------------------------------
    allowed_ga = lim.get("gantry_angles_allowed")
    if not _configured(allowed_ga):
        unconfigured.add("gantry_angles_allowed")
    elif bs.gantry_angle is not None and not _unlimited(allowed_ga):
        ok = _angle_allowed(bs.gantry_angle, allowed_ga)
        if not ok:
            violations.append(_violation(
                "gantry_angle",
                f"{where}: gantry angle {bs.gantry_angle:g} deg is not a "
                f"permitted setting."))

    # --- snout -----------------------------------------------------------
    allowed_snout = lim.get("snout_positions_allowed")
    if not _configured(allowed_snout):
        unconfigured.add("snout_positions_allowed")
    elif bs.snout_position is not None and not _unlimited(allowed_snout):
        if not _value_allowed(bs.snout_position, allowed_snout):
            violations.append(_violation(
                "snout_position",
                f"{where}: snout position {bs.snout_position:g} mm is not a "
                f"permitted setting."))

    # --- range shifter ---------------------------------------------------
    allowed_rs = lim.get("range_shifter_ids_allowed")
    if not _configured(allowed_rs):
        unconfigured.add("range_shifter_ids_allowed")
    elif bs.range_shifter_ids and not _unlimited(allowed_rs):
        bad = [r for r in bs.range_shifter_ids if r not in allowed_rs]
        if bad:
            violations.append(_violation(
                "range_shifter_id",
                f"{where}: range shifter(s) {', '.join(bad)} not in the "
                f"commissioned set."))

    # --- air gap ---------------------------------------------------------
    gap_min, gap_max = lim.get("air_gap_min_mm"), lim.get("air_gap_max_mm")
    if not _configured(gap_min):
        unconfigured.add("air_gap_min_mm")
        gap_min = None
    elif _unlimited(gap_min):
        gap_min = None
    if not _configured(gap_max):
        unconfigured.add("air_gap_max_mm")
        gap_max = None
    elif _unlimited(gap_max):
        gap_max = None
    if bs.air_gaps_mm:
        if gap_min is not None:
            low = [g for g in bs.air_gaps_mm if g < float(gap_min)]
            if low:
                violations.append(_violation(
                    "air_gap_min",
                    f"{where}: air gap {min(low):.1f} mm below minimum "
                    f"{gap_min} mm."))
        if gap_max is not None:
            high = [g for g in bs.air_gaps_mm if g > float(gap_max)]
            if high:
                violations.append(_violation(
                    "air_gap_max",
                    f"{where}: air gap {max(high):.1f} mm above maximum "
                    f"{gap_max} mm."))

    return violations, unconfigured


def _value_allowed(value: float, allowed: Any, tol: float = 0.01) -> bool:
    if isinstance(allowed, dict):
        lo, hi = allowed.get("min"), allowed.get("max")
        if lo is not None and value < float(lo) - tol:
            return False
        if hi is not None and value > float(hi) + tol:
            return False
        return True
    if isinstance(allowed, (list, tuple)):
        return any(abs(value - float(a)) <= tol for a in allowed)
    return True


def _angle_allowed(angle: float, allowed: Any, tol: float = 0.01) -> bool:
    if isinstance(allowed, dict) and allowed.get("continuous"):
        return True
    return _value_allowed(angle % 360.0, allowed, tol)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def check_plan(rtplan_path: str,
               limits_path: Optional[str] = None) -> dict[str, Any]:
    """Check an RT Ion Plan against the commissioned envelope.

    Returns the payload services/gate.py expects:
        {"checked": bool, "violations": [...], "unconfigured": [...],
         "rules_checked": int, "beams": int, "machine": str|None}

    checked is True only when every rule ran with real limits. Any
    unconfigured rule -> checked False -> gate reports the layer unavailable.
    """
    try:
        import pydicom
    except ImportError:
        return {"checked": False, "violations": [],
                "unconfigured": ["pydicom not available"],
                "error": "pydicom is required to read the RT Ion Plan."}

    try:
        ds = pydicom.dcmread(rtplan_path, force=True)
    except Exception as exc:  # noqa: BLE001
        return {"checked": False, "violations": [], "unconfigured": [],
                "error": f"Could not read RT Ion Plan: {exc}"}

    limits = load_limits(limits_path)
    tol = limits.get("tolerance") or {}

    beams = extract_beams(ds)
    if not beams:
        return {"checked": False, "violations": [], "unconfigured": [],
                "error": "No IonBeamSequence found; not an RT Ion Plan?"}

    all_violations: list[dict] = []
    all_unconfigured: set[str] = set()
    machines: set[str] = set()

    for bs in beams:
        if bs.machine:
            machines.add(bs.machine)
        lim = limits_for_room(limits, bs.machine)
        v, u = _check_beam(bs, lim, tol)
        all_violations.extend(v)
        all_unconfigured.update(u)

    return {
        "checked": len(all_unconfigured) == 0,
        "violations": all_violations,
        "unconfigured": sorted(all_unconfigured),
        "rules_checked": max(0, N_RULES - len(all_unconfigured)),
        "beams": len(beams),
        "machine": ", ".join(sorted(machines)) if machines else None,
    }


# ---------------------------------------------------------------------------
# Survey mode -- observed values across existing plans
# ---------------------------------------------------------------------------

def survey(root: str) -> dict[str, Any]:
    """Walk a directory of RT Ion Plans and report observed value ranges.

    This exists to cross-check commissioning numbers, NOT to derive them.
    Observed ranges describe what your plans happen to use; the commissioned
    envelope is necessarily wider and must come from commissioning data.
    """
    try:
        import pydicom
    except ImportError:
        print("pydicom is required for survey mode.")
        return {}

    obs: dict[str, dict[str, Any]] = {}
    n_plans = 0

    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.lower().endswith(".dcm"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                ds = pydicom.dcmread(path, force=True, stop_before_pixels=True)
            except Exception:  # noqa: BLE001
                continue
            if not hasattr(ds, "IonBeamSequence"):
                continue
            n_plans += 1
            for bs in extract_beams(ds):
                key = bs.machine or "(unnamed machine)"
                o = obs.setdefault(key, {
                    "beams": 0, "energy_min": None, "energy_max": None,
                    "spot_mu_min": None, "spot_mu_max": None,
                    "abs_x_max": 0.0, "abs_y_max": 0.0,
                    "spots_per_layer_max": 0, "layers_max": 0,
                    "gantry_angles": set(), "snouts": set(),
                    "range_shifters": set(), "air_gap_min": None,
                    "air_gap_max": None,
                })
                o["beams"] += 1
                o["layers_max"] = max(o["layers_max"], bs.n_layers)
                for energy, spots in bs.layers:
                    o["energy_min"] = energy if o["energy_min"] is None \
                        else min(o["energy_min"], energy)
                    o["energy_max"] = energy if o["energy_max"] is None \
                        else max(o["energy_max"], energy)
                    o["spots_per_layer_max"] = max(o["spots_per_layer_max"],
                                                   len(spots))
                for _e, x, y, mu in bs.iter_spots():
                    o["abs_x_max"] = max(o["abs_x_max"], abs(x))
                    o["abs_y_max"] = max(o["abs_y_max"], abs(y))
                    if mu == mu:  # not NaN
                        o["spot_mu_min"] = mu if o["spot_mu_min"] is None \
                            else min(o["spot_mu_min"], mu)
                        o["spot_mu_max"] = mu if o["spot_mu_max"] is None \
                            else max(o["spot_mu_max"], mu)
                if bs.gantry_angle is not None:
                    o["gantry_angles"].add(round(bs.gantry_angle, 1))
                if bs.snout_position is not None:
                    o["snouts"].add(round(bs.snout_position, 1))
                for r in bs.range_shifter_ids:
                    o["range_shifters"].add(r)
                for g in bs.air_gaps_mm:
                    o["air_gap_min"] = g if o["air_gap_min"] is None \
                        else min(o["air_gap_min"], g)
                    o["air_gap_max"] = g if o["air_gap_max"] is None \
                        else max(o["air_gap_max"], g)

    print(f"\nSurveyed {n_plans} RT Ion Plan(s) under {root}\n")
    for machine, o in sorted(obs.items()):
        print(f"=== {machine} ===  ({o['beams']} beam(s))")
        print(f"  energy observed      : {o['energy_min']} - {o['energy_max']} MeV")
        print(f"  spot MU observed     : {o['spot_mu_min']} - {o['spot_mu_max']}")
        print(f"  max |x|, |y|         : {o['abs_x_max']:.1f}, {o['abs_y_max']:.1f} mm")
        print(f"  max spots per layer  : {o['spots_per_layer_max']}")
        print(f"  max layers per field : {o['layers_max']}")
        print(f"  gantry angles        : {sorted(o['gantry_angles'])}")
        print(f"  snout positions      : {sorted(o['snouts'])}")
        print(f"  range shifters       : {sorted(o['range_shifters'])}")
        print(f"  air gap observed     : {o['air_gap_min']} - {o['air_gap_max']} mm")
        print()
    print("NOTE: observed ranges are what these plans use, not commissioned")
    print("limits. Use them only to sanity-check commissioning values.\n")
    return obs


def _main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "survey":
        survey(argv[1])
        return 0
    if len(argv) >= 1 and argv[0] not in ("survey",):
        result = check_plan(argv[0])
        print(json.dumps(result, indent=2))
        return 0
    print(__doc__)
    print("Usage:")
    print("  python -m services.deliverability <RTPLAN.dcm>")
    print("  python -m services.deliverability survey <directory>")
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
