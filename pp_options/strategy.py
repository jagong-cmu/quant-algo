"""Construct defined-risk vertical spreads from a normalized chain.

Primary:   put credit spread  -- sell ~30-delta put, buy a further-OTM put.
Secondary: call debit spread  -- buy ~ATM call, sell a further-OTM call.

Each builder returns (Spread | None, reasons). When it returns None the reasons
explain why no valid structure was found, so the engine can log a clear skip.
Sizing fits the per-trade risk budget; the guardrail engine re-verifies the caps
independently before anything is submitted.
"""

from __future__ import annotations

import math
from typing import Optional

from . import config
from .chain import NormOption, choose_expiry, select_by_delta, strikes_for
from .models import Leg, Spread


def _nearest_below(strikes: list[NormOption], ref_strike: float, target: float) -> Optional[NormOption]:
    """Among strikes strictly below ref_strike, the one closest to `target`."""
    below = [o for o in strikes if o.strike < ref_strike - 1e-9]
    if not below:
        return None
    return min(below, key=lambda o: abs(o.strike - target))


def _nearest_above_by_delta(strikes: list[NormOption], ref_strike: float,
                            target_delta: float) -> Optional[NormOption]:
    above = [o for o in strikes if o.strike > ref_strike + 1e-9 and o.abs_delta is not None]
    if not above:
        return None
    return min(above, key=lambda o: abs(o.abs_delta - target_delta))


def _size_contracts(per_contract_max_loss: float, equity: float, risk_scale: float) -> int:
    budget = config.MAX_TRADE_RISK_PCT * equity * risk_scale
    if per_contract_max_loss <= 0:
        return 0
    return int(math.floor(budget / per_contract_max_loss))


def build_put_credit_spread(options: list[NormOption], underlying: str, equity: float,
                            risk_scale: float,
                            target_short_delta: float = config.TARGET_SHORT_PUT_DELTA
                            ) -> tuple[Optional[Spread], list[str]]:
    reasons: list[str] = []
    expiry = choose_expiry(options)
    if expiry is None:
        return None, [f"no expiry in DTE window [{config.DTE_MIN},{config.DTE_MAX}]"]

    short_put = select_by_delta(options, "P", expiry, target_short_delta)
    if short_put is None:
        return None, [f"no put with a known delta near {target_short_delta} "
                      f"for expiry {expiry} (delta unavailable -> fail closed)"]
    if short_put.mid is None:
        return None, [f"short put {short_put.strike} has no usable price"]

    puts = strikes_for(options, "P", expiry)

    best: Optional[Spread] = None
    for width in sorted(config.CANDIDATE_WIDTHS):
        long_put = _nearest_below(puts, short_put.strike, short_put.strike - width)
        if long_put is None or long_put.mid is None:
            continue
        actual_width = short_put.strike - long_put.strike
        net_credit = short_put.mid - long_put.mid
        if net_credit <= 0:
            continue
        if net_credit / actual_width < config.MIN_CREDIT_TO_WIDTH:
            continue
        per_contract_loss = (actual_width - net_credit) * 100.0
        if per_contract_loss <= 0:
            continue  # credit >= width: nonsensical, skip
        contracts = _size_contracts(per_contract_loss, equity, risk_scale)
        if contracts < 1:
            reasons.append(f"width {actual_width:.1f}: 1 contract (${per_contract_loss:.0f}) "
                           f"exceeds per-trade budget")
            continue
        spread = Spread(
            underlying=underlying, kind="put_credit",
            short_leg=Leg(short_put, "SELL_TO_OPEN"),
            long_leg=Leg(long_put, "BUY_TO_OPEN"),
            contracts=contracts, net_credit=round(net_credit, 2),
            notes=[f"short {short_put.strike}P (|d|={short_put.abs_delta:.2f}) / "
                   f"long {long_put.strike}P, width {actual_width:.1f}, "
                   f"credit {net_credit:.2f}, x{contracts}"],
        )
        best = spread  # smallest viable width wins (most conservative)
        break

    if best is None:
        reasons.append("no viable put-credit width found (price/credit/budget constraints)")
        return None, reasons
    return best, best.notes


def build_call_debit_spread(options: list[NormOption], underlying: str,
                            equity: float, risk_scale: float = 1.0
                            ) -> tuple[Optional[Spread], list[str]]:
    reasons: list[str] = []
    expiry = choose_expiry(options)
    if expiry is None:
        return None, [f"no expiry in DTE window [{config.DTE_MIN},{config.DTE_MAX}]"]

    long_call = select_by_delta(options, "C", expiry, config.LONG_CALL_TARGET_DELTA)
    if long_call is None or long_call.mid is None:
        return None, [f"no call with a known delta near {config.LONG_CALL_TARGET_DELTA} "
                      f"for expiry {expiry} (fail closed)"]

    calls = strikes_for(options, "C", expiry)
    best: Optional[Spread] = None
    # prefer the short strike nearest the target short delta, but constrained to be
    # above the long strike; if width too wide for budget, this still gets sized.
    short_call = _nearest_above_by_delta(calls, long_call.strike, config.SHORT_CALL_TARGET_DELTA)
    if short_call is None or short_call.mid is None:
        return None, ["no OTM short call with known delta above the long strike"]

    net_debit = long_call.mid - short_call.mid
    if net_debit <= 0:
        return None, [f"call debit non-positive ({net_debit:.2f}); skipping"]
    width = short_call.strike - long_call.strike
    per_contract_loss = net_debit * 100.0
    contracts = _size_contracts(per_contract_loss, equity, risk_scale)
    if contracts < 1:
        return None, [f"1 contract (${per_contract_loss:.0f} debit) exceeds per-trade budget"]

    best = Spread(
        underlying=underlying, kind="call_debit",
        long_leg=Leg(long_call, "BUY_TO_OPEN"),
        short_leg=Leg(short_call, "SELL_TO_OPEN"),
        contracts=contracts, net_debit=round(net_debit, 2),
        notes=[f"long {long_call.strike}C (|d|={long_call.abs_delta:.2f}) / "
               f"short {short_call.strike}C (|d|={short_call.abs_delta:.2f}), "
               f"width {width:.1f}, debit {net_debit:.2f}, x{contracts}"],
    )
    return best, best.notes
