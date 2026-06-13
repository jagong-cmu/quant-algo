#!/usr/bin/env python3
"""Self-contained tests proving the guardrails BLOCK, not just pass.

Run:  .venv/bin/python tests/test_system.py
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

from pp_options import bsm, config, guardrails
from pp_options.broker import extract_equity
from pp_options.chain import NormOption
from pp_options.models import Leg, Spread
from pp_options.occ import build_occ, is_valid_occ, parse_occ
from pp_options.risk import BookRiskTracker

EXPIRY = date.today() + timedelta(days=37)
PASSED = 0
FAILED = 0


def check(name: str, cond: bool) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}")


def _put(strike: float, delta: float, mid: float) -> NormOption:
    return NormOption("SPY", "P", strike, EXPIRY, 37, mid - 0.05, mid + 0.05, mid, delta, 0.16,
                      build_occ("SPY", EXPIRY, "P", strike))


def _call(strike: float, delta: float, mid: float) -> NormOption:
    return NormOption("SPY", "C", strike, EXPIRY, 37, mid - 0.05, mid + 0.05, mid, delta, 0.16,
                      build_occ("SPY", EXPIRY, "C", strike))


def put_credit(short_k, long_k, credit, contracts) -> Spread:
    return Spread("SPY", "put_credit",
                  Leg(_put(short_k, -0.30, 3.00), "SELL_TO_OPEN"),
                  Leg(_put(long_k, -0.20, 3.00 - credit), "BUY_TO_OPEN"),
                  contracts=contracts, net_credit=credit)


# ---- OCC ------------------------------------------------------------------
def test_occ():
    sym = build_occ("SPY", date(2026, 5, 15), "C", 200.0)
    check("OCC build matches docs example format", sym == "SPY260515C00200000")
    check("OCC round-trips", parse_occ(sym).strike == 200.0 and parse_occ(sym).right == "C")
    check("OCC rejects garbage", not is_valid_occ("NOTASYMBOL"))
    check("OCC rejects bad strike padding", not is_valid_occ("SPY260515C0020000"))


# ---- Black-Scholes --------------------------------------------------------
def test_bsm():
    d_put = bsm.bs_delta(540, 528, 0.04, 0.013, 0.16, 37 / 365, "P")
    d_call = bsm.bs_delta(540, 552, 0.04, 0.013, 0.16, 37 / 365, "C")
    check("put delta is negative", -1 < d_put < 0)
    check("call delta is positive", 0 < d_call < 1)
    # implied vol round-trips
    price = bsm.bs_price(540, 528, 0.04, 0.013, 0.18, 37 / 365, "P")
    iv = bsm.implied_vol(price, 540, 528, 0.04, 0.013, 37 / 365, "P")
    check("implied vol round-trips ~0.18", iv is not None and abs(iv - 0.18) < 0.005)


# ---- guardrail 4: sanity --------------------------------------------------
def test_sanity():
    good = put_credit(528, 527, 0.31, 1)
    ok, issues = guardrails.sanity_checks(good)
    check("valid put credit passes sanity", ok)

    # wrong instruction (would be naked-ish / malformed)
    bad = put_credit(528, 527, 0.31, 1)
    bad.short_leg.instruction = "BUY_TO_OPEN"
    ok, _ = guardrails.sanity_checks(bad)
    check("wrong instruction blocked", not ok)

    # non-positive credit
    badc = put_credit(528, 527, -0.10, 1)
    ok, _ = guardrails.sanity_checks(badc)
    check("negative credit blocked", not ok)

    # credit >= width -> undefined/negative max loss -> blocked
    badw = put_credit(528, 527, 1.50, 1)
    check("credit>=width => max loss not computable", badw.per_contract_max_loss is None)
    ok, _ = guardrails.sanity_checks(badw)
    check("credit>=width blocked by sanity", not ok)

    # short strike not above long strike
    inv = put_credit(527, 528, 0.31, 1)
    ok, _ = guardrails.sanity_checks(inv)
    check("inverted strikes blocked", not ok)

    # non-integer quantity
    fq = put_credit(528, 527, 0.31, 1)
    fq.contracts = 2.5  # type: ignore
    ok, _ = guardrails.sanity_checks(fq)
    check("non-integer contracts blocked", not ok)


# ---- guardrails 1 & 2: risk caps -----------------------------------------
def test_risk_caps():
    equity = 100_000.0
    tracker = BookRiskTracker(equity=equity, open_risk=0.0)

    # per-trade cap: width 5, credit 0.50 -> $450/contract; 10 contracts = $4500 > 3% ($3000)
    big = put_credit(528, 523, 0.50, 10)
    rep = guardrails.evaluate_order(big, equity, tracker)
    check("per-trade cap blocks >3% spread",
          rep.blocked and any("per_trade_risk" in r for r in rep.block_reasons))

    # within per-trade cap
    ok_spread = put_credit(528, 527, 0.31, 40)  # $69 * 40 = $2760 < $3000
    rep2 = guardrails.evaluate_order(ok_spread, equity, tracker)
    check("compliant spread passes both caps", not rep2.blocked)

    # book cap: pre-load near the 20% ($20k) limit, new $2,760 should breach
    near = BookRiskTracker(equity=equity, open_risk=19_000.0)
    rep3 = guardrails.evaluate_order(ok_spread, equity, near)
    check("book cap blocks when 20% would be breached",
          rep3.blocked and any("book_risk" in r for r in rep3.block_reasons))


# ---- guardrail 3: regime --------------------------------------------------
def test_regime():
    check("VIX None -> fail closed", not guardrails.vix_gate(None, None).short_premium_allowed)
    check("VIX >= skip -> blocked", not guardrails.vix_gate(30.0, 29.0).short_premium_allowed)
    rising = guardrails.vix_gate(20.0, 16.0)  # +25% dod
    check("VIX rising sharply -> blocked", not rising.short_premium_allowed)
    elevated = guardrails.vix_gate(24.0, 23.8)
    check("VIX elevated -> scaled down", elevated.short_premium_allowed and elevated.risk_scale == config.SCALE_DOWN_FACTOR)
    benign = guardrails.vix_gate(15.0, 15.0)
    check("VIX benign -> full size", benign.short_premium_allowed and benign.risk_scale == 1.0)

    check("trend None -> fail closed", not guardrails.trend_gate(None, 100.0)[0])
    check("spot above SMA -> calls allowed", guardrails.trend_gate(540.0, 525.0)[0])
    check("spot below SMA -> no calls", not guardrails.trend_gate(205.0, 212.0)[0])


# ---- equity extraction ----------------------------------------------------
def test_equity_extraction():
    check("flat equity field", extract_equity({"equity": 50000}) == 50000)
    check("nested net_liquidation", extract_equity({"balances": {"netLiquidation": 75000}}) == 75000)
    check("missing -> None (fail closed)", extract_equity({"foo": "bar"}) is None)


def main() -> int:
    for fn in (test_occ, test_bsm, test_sanity, test_risk_caps, test_regime, test_equity_extraction):
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{'='*50}\nPASSED={PASSED}  FAILED={FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
