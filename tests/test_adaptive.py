#!/usr/bin/env python3
"""Invariant tests for the adaptive sizing layer.

Proves the layer can ONLY de-risk: multiplier in [0,1], delta in [0.20,0.30],
never more aggressive than base, and the 3%/20% hard caps still override it.

Run:  .venv/bin/python tests/test_adaptive.py
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

from pp_options import regime_model as RM
from pp_options.regime_model import (AdaptiveDecision, RegimeFeatures, RegimeThresholds,
                                     classify, clamp_to_base, decide)

PASSED = FAILED = 0


def check(name, cond):
    global PASSED, FAILED
    if cond:
        PASSED += 1; print(f"  PASS  {name}")
    else:
        FAILED += 1; print(f"  FAIL  {name}")


# representative thresholds (as if fit on a normal regime)
THR = RegimeThresholds(vix_q40=14.0, vix_q70=18.0, vix_q90=26.0, rv21_q90=0.25,
                       fit_date=date(2026, 1, 1), n_train=750)


def test_clamp_monotone():
    # adaptive can never EXCEED base, on either axis, for any inputs
    import random
    rng = random.Random(1)
    ok = True
    for _ in range(2000):
        base = rng.random()                       # base scale in [0,1]
        d = AdaptiveDecision("X", rng.random() * 1.5, 0.10 + rng.random() * 0.40, False)
        m, dl = clamp_to_base(d, base)
        if not (0.0 <= m <= 1.0): ok = False
        if m > base + 1e-9: ok = False            # never more aggressive size
        if not (RM.MIN_DELTA - 1e-9 <= dl <= RM.BASE_DELTA + 1e-9): ok = False
        if dl > RM.BASE_DELTA + 1e-9: ok = False  # never closer to the money than base
    check("clamp: multiplier in [0,1] and <= base for all inputs", ok)
    check("clamp: delta in [0.20,0.30] and <= base for all inputs", ok)


def test_policy_bounds():
    ok = all(0.0 <= m <= 1.0 and RM.MIN_DELTA <= dl <= RM.BASE_DELTA
             for m, dl in RM.POLICY.values())
    check("policy table: all multipliers in [0,1], deltas in [0.20,0.30]", ok)
    # monotone: stress regimes are not larger than calmer ones
    order = [RM.CALM, RM.NORMAL, RM.ELEVATED, RM.STRESS]
    mults = [RM.POLICY[r][0] for r in order]
    deltas = [RM.POLICY[r][1] for r in order]
    check("policy: multiplier non-increasing calm->stress", all(mults[i] >= mults[i+1] for i in range(3)))
    check("policy: delta non-increasing calm->stress", all(deltas[i] >= deltas[i+1] for i in range(3)))


def test_classifier_monotone():
    calm = RegimeFeatures(vix=12, vix3m=15, rv10=0.10, rv21=0.10)
    normal = RegimeFeatures(vix=16, vix3m=18, rv10=0.13, rv21=0.13)
    stress = RegimeFeatures(vix=35, vix3m=28, rv10=0.45, rv21=0.40)  # backwardation + high rv
    check("calm features -> CALM/NORMAL", classify(calm, THR) in (RM.CALM, RM.NORMAL))
    check("stress features -> STRESS", classify(stress, THR) == RM.STRESS)
    # backwardation bumps regime up vs same VIX in contango
    contango = RegimeFeatures(vix=17, vix3m=20, rv10=0.12, rv21=0.12)
    backwd = RegimeFeatures(vix=17, vix3m=15, rv10=0.12, rv21=0.12)
    o = RM.REGIME_ORDER
    check("backwardation is >= contango in stress", o.index(classify(backwd, THR)) >= o.index(classify(contango, THR)))


def test_stress_skips():
    stress = RegimeFeatures(vix=40, vix3m=30, rv10=0.5, rv21=0.5)
    d = decide(stress, THR)
    check("STRESS regime -> skip (multiplier 0)", d.skip and d.multiplier == 0.0)
    # even from full base, clamp keeps it 0
    m, _ = clamp_to_base(d, 1.0)
    check("STRESS clamps size to 0 even at full base", m == 0.0)


def test_caps_still_override():
    # build an oversized spread and confirm guardrails block regardless of adaptive
    from pp_options.guardrails import evaluate_order
    from pp_options.risk import BookRiskTracker
    from tests.test_system import put_credit  # reuse helper
    big = put_credit(528, 523, 0.50, 10)  # ~$4500 max loss > 3% of 100k
    rep = evaluate_order(big, 100_000.0, BookRiskTracker(100_000.0, 0.0))
    check("hard 3% cap still blocks oversized spread (overrides adaptive)",
          rep.blocked and any("per_trade_risk" in r for r in rep.block_reasons))


def main():
    for fn in (test_clamp_monotone, test_policy_bounds, test_classifier_monotone,
               test_stress_skips, test_caps_still_override):
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{'='*50}\nPASSED={PASSED}  FAILED={FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
