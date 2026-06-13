"""Adaptive sizing layer: an interpretable volatility-regime classifier that can
ONLY make the book equal-or-more conservative than the base rules.

Design constraints (hard):
  * Predicts REGIME / volatility -- never returns or "optimal profit".
  * Outputs a size multiplier in [0, 1] (never > 1.0) and a short-leg delta in a
    fixed allowed band [0.20, 0.30] (never closer to the money than 0.30).
  * clamp_to_base() guarantees the result is <= the base rule on every axis, so
    the layer is monotonically de-risking by construction.
  * The 3%/20% hard caps are enforced AFTER this layer (in guardrails) and
    override it. Nothing here can loosen them.

"Training" = estimating VIX / realized-vol QUANTILE THRESHOLDS on a past window.
Those thresholds are the only fitted parameters; they are applied to strictly
future data in walk-forward. The regime->policy map is a FIXED defensive table,
not optimized against P/L.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

# Allowed short-leg delta band (never closer than 0.30, never further than 0.20)
BASE_DELTA = 0.30
MIN_DELTA = 0.20

# Regime labels, ordered from calm -> stress.
CALM, NORMAL, ELEVATED, STRESS = "CALM", "NORMAL", "ELEVATED", "STRESS"
REGIME_ORDER = [CALM, NORMAL, ELEVATED, STRESS]

# FIXED defensive policy: regime -> (size multiplier, short-leg target delta).
# These are pre-registered risk choices, NOT fit to returns. All multipliers
# <= 1.0; all deltas within [MIN_DELTA, BASE_DELTA].
POLICY: dict[str, tuple[float, float]] = {
    CALM:     (1.00, 0.30),
    NORMAL:   (0.75, 0.30),
    ELEVATED: (0.50, 0.25),
    STRESS:   (0.00, 0.20),   # multiplier 0 => skip new entries
}


def percentile(xs: list[float], p: float) -> float:
    """Linear-interpolation percentile (p in [0,100]); no numpy dependency."""
    s = sorted(x for x in xs if x is not None)
    if not s:
        return float("nan")
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


@dataclass
class RegimeFeatures:
    """All computed from data available AT the decision date (no lookahead)."""
    vix: float
    vix3m: Optional[float]
    rv10: Optional[float]      # trailing 10d annualized realized vol
    rv21: Optional[float]      # trailing 21d annualized realized vol

    @property
    def term_ratio(self) -> Optional[float]:
        # VIX / VIX3M ; > 1.0 == backwardation == stress.
        if self.vix3m and self.vix3m > 0:
            return self.vix / self.vix3m
        return None

    @property
    def rv_spike(self) -> Optional[float]:
        if self.rv10 and self.rv21 and self.rv21 > 0:
            return self.rv10 / self.rv21
        return None


@dataclass
class RegimeThresholds:
    """Fitted parameters: VIX & realized-vol quantiles from the TRAIN window."""
    vix_q40: float
    vix_q70: float
    vix_q90: float
    rv21_q90: float
    fit_date: date            # last date of the training window (for staleness)
    n_train: int

    @classmethod
    def fit(cls, vix_levels: list[float], rv21_levels: list[float], fit_date: date) -> "RegimeThresholds":
        rv = [x for x in rv21_levels if x is not None]
        return cls(
            vix_q40=percentile(vix_levels, 40),
            vix_q70=percentile(vix_levels, 70),
            vix_q90=percentile(vix_levels, 90),
            rv21_q90=percentile(rv, 90) if rv else float("inf"),
            fit_date=fit_date,
            n_train=len(vix_levels),
        )

    def vix_band_upper(self, regime: str) -> float:
        """Upper VIX bound implied by a regime label (used for decay detection)."""
        return {CALM: self.vix_q40, NORMAL: self.vix_q70,
                ELEVATED: self.vix_q90, STRESS: float("inf")}[regime]


def classify(feat: RegimeFeatures, thr: RegimeThresholds) -> str:
    """Map features -> regime label using trained thresholds. Pure / interpretable.

    Base level from VIX quantiles, then bumped UP (never down) by any stress
    signal: backwardation, a realized-vol spike, or realized vol above the train
    90th percentile. Monotone in stress.
    """
    if feat.vix < thr.vix_q40:
        level = 0
    elif feat.vix < thr.vix_q70:
        level = 1
    elif feat.vix < thr.vix_q90:
        level = 2
    else:
        level = 3

    bump = 0
    if feat.term_ratio is not None and feat.term_ratio > 1.0:     # backwardation
        bump = 1
    if feat.rv_spike is not None and feat.rv_spike > 1.30:        # short vol >> long vol
        bump = max(bump, 1)
    if feat.rv21 is not None and feat.rv21 > thr.rv21_q90:        # realized vol extreme
        bump = max(bump, 1)

    return REGIME_ORDER[min(level + bump, 3)]


@dataclass
class AdaptiveDecision:
    regime: str
    multiplier: float         # in [0, 1]
    target_delta: float       # in [MIN_DELTA, BASE_DELTA]
    skip: bool
    reasons: list[str] = field(default_factory=list)


def decide(feat: RegimeFeatures, thr: RegimeThresholds) -> AdaptiveDecision:
    regime = classify(feat, thr)
    mult, delta = POLICY[regime]
    reasons = [f"regime={regime}", f"VIX={feat.vix:.1f} (q40/70/90="
               f"{thr.vix_q40:.1f}/{thr.vix_q70:.1f}/{thr.vix_q90:.1f})"]
    if feat.term_ratio is not None:
        reasons.append(f"term VIX/VIX3M={feat.term_ratio:.2f}"
                       f"{' BACKWARDATION' if feat.term_ratio > 1.0 else ''}")
    if feat.rv_spike is not None:
        reasons.append(f"rv10/rv21={feat.rv_spike:.2f}")
    return AdaptiveDecision(regime=regime, multiplier=mult, target_delta=delta,
                            skip=(mult <= 0.0), reasons=reasons)


def clamp_to_base(decision: AdaptiveDecision, base_multiplier: float,
                  base_delta: float = BASE_DELTA) -> tuple[float, float]:
    """Guarantee the adaptive output is <= the base rule on EVERY axis.

    Returns (effective_multiplier, effective_delta). This is the invariant that
    makes the layer monotonically de-risking regardless of policy/classifier bugs:
      * effective_multiplier = min(base, adaptive), bounded to [0, 1]
      * effective_delta      = min(base_delta, adaptive), bounded to [MIN_DELTA, base_delta]
    """
    eff_mult = min(base_multiplier, decision.multiplier, 1.0)
    eff_mult = max(0.0, eff_mult)
    eff_delta = min(base_delta, decision.target_delta)
    eff_delta = max(MIN_DELTA, min(eff_delta, base_delta))
    return eff_mult, eff_delta


# ---- staleness / retrain guidance -----------------------------------------
RETRAIN_CADENCE_DAYS = 182          # refit at least every ~6 months
STALE_WARN_DAYS = 210               # warn in live runs past this age


def staleness_days(thr: RegimeThresholds, as_of: date) -> int:
    return (as_of - thr.fit_date).days


def is_stale(thr: RegimeThresholds, as_of: date) -> bool:
    return staleness_days(thr, as_of) > STALE_WARN_DAYS
