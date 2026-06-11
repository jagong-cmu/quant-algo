#!/usr/bin/env python3
"""Single 1-contract execution-path test: a real, defined-risk put credit spread.

Builds ONE conservative ~30-delta put credit spread from REAL PentPort chain data
(real delta for strike selection; net credit from lastPrice, since PentPort gives
no option bid/ask), runs it through the existing guardrails, and submits via the
gated path. With LIVE_TRADING=False it logs the exact would-be order; flip to True
to actually place the single test order, then it polls orders()/positions() to
confirm acceptance + fill.

Purpose: verify the execution plumbing end-to-end on something sound -- NOT to bet
on edge. Max loss on the one spread is ~$400-500.

Because PentPort gives no option bid/ask, the limit credit is derived from
lastPrice. --haircut shaves the credit we ask for (accept a little less) to
improve fill odds without a live two-sided market; the haircut is applied to the
risk calc too, so max loss is computed conservatively against the lower credit.

Usage:
    python live_test_order.py                       # dry-run: show the real would-be order
    python live_test_order.py --haircut 0.15        # ask 15c less credit (better fill)
    # then, at the next OPEN, set LIVE_TRADING=True in config.py and re-run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from pp_options import chain as chainmod
from pp_options import config, guardrails
from pp_options.broker import Broker
from pp_options.envload import load_env
from pp_options.logutil import get_logger, log_payload, setup_logging
from pp_options.models import Leg, Spread
from pp_options.risk import BookRiskTracker

UNDERLYING = "SPY"
WIDTH = 5.0           # $5-wide spread -> defined max loss


def pick_expiry(broker: Broker, underlying: str) -> str:
    """Choose the listed expiry closest to the middle of the DTE window."""
    raw = broker.options_chain(underlying)
    inner = raw.get("chain", raw)
    allx = inner.get("allExpiries") or []
    today = dt.date.today()
    target = (config.DTE_MIN + config.DTE_MAX) / 2
    dated = [(e, (dt.date.fromisoformat(e) - today).days) for e in allx]
    dated = [(e, d) for e, d in dated if config.DTE_MIN - 5 <= d <= config.DTE_MAX + 10]
    if not dated:
        raise RuntimeError(f"no expiry near {config.DTE_MIN}-{config.DTE_MAX} DTE in {allx}")
    return min(dated, key=lambda x: abs(x[1] - target))[0]


def nearest_below(puts, ref_strike, target):
    below = [o for o in puts if o.strike < ref_strike - 1e-9]
    return min(below, key=lambda o: abs(o.strike - target)) if below else None


def main() -> int:
    ap = argparse.ArgumentParser(description="1-contract execution-path test")
    ap.add_argument("--underlying", default=UNDERLYING)
    ap.add_argument("--width", type=float, default=WIDTH)
    ap.add_argument("--contracts", type=int, default=1)
    ap.add_argument("--haircut", type=float, default=0.0,
                    help="reduce the asked net credit by this much to improve fill odds")
    args = ap.parse_args()

    load_env()
    log, path = setup_logging(tag="live_test_order")
    log.info("Execution-path test | LIVE_TRADING=%s | %s %d-contract put credit spread (haircut %.2f)",
             config.LIVE_TRADING, args.underlying, args.contracts, args.haircut)
    broker = Broker()

    equity = broker.equity()
    expiry = pick_expiry(broker, args.underlying)
    log.info("Using expiry %s (DTE=%d)", expiry, (dt.date.fromisoformat(expiry) - dt.date.today()).days)

    raw = broker.options_chain(args.underlying, expiry=expiry)
    spot = chainmod.underlying_price(raw)
    norm = chainmod.normalize(raw, args.underlying, spot)
    exp_date = dt.date.fromisoformat(expiry)
    puts = [o for o in chainmod.strikes_for(norm, "P", exp_date) if o.delta is not None and o.mid]
    if not puts:
        log.error("No puts with delta+price for %s %s -- aborting.", args.underlying, expiry)
        return 1

    short_put = min(puts, key=lambda o: abs(o.abs_delta - config.TARGET_SHORT_PUT_DELTA))
    long_put = nearest_below(puts, short_put.strike, short_put.strike - args.width)
    if long_put is None:
        log.error("No long put below %.0f -- aborting.", short_put.strike)
        return 1

    fair_credit = short_put.mid - long_put.mid
    net_credit = round(fair_credit - args.haircut, 2)   # ask less -> better fill; conservative risk
    if net_credit <= 0:
        log.error("Credit after haircut is non-positive (%.2f); aborting.", net_credit)
        return 1
    spread = Spread(
        underlying=args.underlying, kind="put_credit",
        short_leg=Leg(short_put, "SELL_TO_OPEN"),
        long_leg=Leg(long_put, "BUY_TO_OPEN"),
        contracts=args.contracts, net_credit=net_credit,
        notes=[f"short {short_put.strike}P (d={short_put.delta:.2f}) / long {long_put.strike}P, "
               f"width {short_put.strike-long_put.strike:.0f}, fair {fair_credit:.2f} -> "
               f"limit {net_credit:.2f} (lastPrice; haircut {args.haircut:.2f})"],
    )
    log.info("Built: %s | spot=%.2f | max loss $%.0f", "; ".join(spread.notes), spot,
             spread.total_max_loss or 0)

    # guardrails (per-trade + book + sanity) must pass before any submit
    report = guardrails.evaluate_order(spread, equity, BookRiskTracker(equity, 0.0))
    if report.blocked:
        log.error("BLOCKED by guardrails: %s", " | ".join(report.block_reasons))
        return 1
    log.info("Guardrails OK (%s)", report.summary())

    payload = spread.to_payload()
    log_payload(log, "Order payload:", payload)
    result = broker.submit_options_order(payload)   # gated by LIVE_TRADING

    if config.LIVE_TRADING and isinstance(result, dict) and not result.get("dry_run"):
        log.info("Submitted. Confirming via orders()/positions() ...")
        try:
            log_payload(log, "orders():", broker.orders())
            log_payload(log, "positions():", broker.positions())
        except Exception as e:
            log.warning("confirmation read failed: %s", e)
    else:
        log.info("DRY-RUN: nothing submitted. Flip LIVE_TRADING=True at the open to place it.")
    log.info("Log: %s", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
