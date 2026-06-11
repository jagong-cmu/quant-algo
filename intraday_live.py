#!/usr/bin/env python3
"""LIVE PAPER runner for the hardened intraday mean-reversion algo.

LIVE_TRADING is OFF by construction: this NEVER calls trade_options(). It reads
live 5-minute bars from Yahoo, runs the exact hardened mean-reversion logic, and
LOGS the would-be debit-spread orders it WOULD place. Account equity is read live
from PentPort (free tier allows account reads); option prices are Black-Scholes-
modeled (free tier blocks live chains) -- magnitudes approximate, direction real.

Single pass (default) shows what the algo has done so far today. With --loop it
polls every --interval seconds until ~market close (20:00 UTC), logging new
would-be orders as fresh bars arrive.

Usage:
    python intraday_live.py                 # one snapshot pass
    python intraday_live.py --loop          # paper-run the rest of the session
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time

from pp_options import config, intraday as ID
from pp_options.logutil import get_logger, log_payload, setup_logging
from pp_options.occ import build_occ

Z_ENTRY, Z_EXIT = 2.0, 0.5
CLOSE_UTC = dt.time(20, 0)


def account_equity() -> tuple[float, str]:
    try:
        from pp_options.envload import load_env
        load_env()
        from pp_options.broker import Broker
        return Broker().equity(), "live PentPort account"
    except Exception:
        return 100_000.0, "default $100k (PentPort read unavailable)"


def _legs_payload(sp: ID.Spread, expiry_date: dt.date) -> list[dict]:
    right = "C" if sp.direction == "bull" else "P"
    long_sym = build_occ(sp.symbol, expiry_date, right, sp.long_k)
    short_sym = build_occ(sp.symbol, expiry_date, right, sp.short_k)
    return [
        {"symbol": long_sym, "instruction": "BUY_TO_OPEN", "quantity": sp.contracts},
        {"symbol": short_sym, "instruction": "SELL_TO_OPEN", "quantity": sp.contracts},
    ]


def order_payload(sp: ID.Spread, expiry_date: dt.date) -> dict:
    return {
        "underlying": sp.symbol,
        "structure": "call_debit" if sp.direction == "bull" else "put_debit",
        "legs": _legs_payload(sp, expiry_date),
        "order_type": "LIMIT",
        "price": round(sp.debit, 2),                  # net debit, positive magnitude
        "complex_order_strategy_type": "VERTICAL",
        "_contracts": sp.contracts,
        "_max_loss": round(sp.max_loss, 2),
    }


def replay_today(bars: dict, iv_day: float, equity: float, expiry: dt.datetime,
                 hardened: bool, risk_pct: float, daily_halt: float):
    """Replay the hardened mean-reversion logic over today's bars so far.

    Returns (actions, open_pos, realized) where actions is an ordered list of
    ('ENTER'|'EXIT', ts, symbol, spread, pnl_or_None, reason_or_None).
    """
    universe = list(bars.keys())
    ind = {s: ID._indicators([b.close for b in bars[s]], "mean_reversion", 20) for s in universe}
    timeline = sorted({b.ts for s in universe for b in bars[s]})
    idx = {s: {b.ts: i for i, b in enumerate(bars[s])} for s in universe}

    open_pos, entry_i, cooldown = {}, {}, {}
    realized = 0.0
    halted = False
    actions = []

    for ts in timeline:
        for s in universe:
            if ts not in idx[s]:
                continue
            i = idx[s][ts]
            if i == 0:
                continue
            bar = bars[s][i]
            iv_s = ID._iv(iv_day, s)

            if s in open_pos:
                sp = open_pos[s]
                val = sp.value(bar.close, iv_s, ts)
                reason = None
                if ID._want_flip_exit(ind[s], i, sp.direction, "mean_reversion", Z_ENTRY, Z_EXIT):
                    reason = "signal_flip"
                elif val <= sp.debit * (1 - ID.STOP_FRAC):
                    reason = "stop"
                elif val >= sp.debit + ID.TARGET_FRAC * (sp.width - sp.debit):
                    reason = "target"
                elif (i - entry_i[s]) >= ID.MAX_HOLD_BARS:
                    reason = "time"
                elif ts >= expiry - dt.timedelta(minutes=10):
                    reason = "eod"
                if reason:
                    pnl = sp.pnl(bar.close, iv_s, ts)
                    realized += pnl
                    actions.append(("EXIT", ts, s, sp, pnl, reason))
                    if reason in ("stop", "time") or pnl < 0:
                        cooldown[s] = i + ID.COOLDOWN_BARS
                    del open_pos[s]; del entry_i[s]

            if realized <= -daily_halt * equity:
                halted = True
            if halted or s in open_pos:
                continue
            if ts >= expiry - dt.timedelta(minutes=30):
                continue
            if i < cooldown.get(s, -1):
                continue
            direction = ID._entry_dir(ind[s], i, "mean_reversion", Z_ENTRY, hardened)
            if direction is None:
                continue
            if sum(p.max_loss for p in open_pos.values()) >= ID.MAX_CONCURRENT_RISK_PCT * equity:
                continue
            sp = ID.build_spread(s, direction, bar.close, iv_s, expiry, ts, equity, risk_pct)
            if sp is None or sp.max_loss > risk_pct * equity + 1e-6:
                continue
            open_pos[s] = sp
            entry_i[s] = i
            actions.append(("ENTER", ts, s, sp, None, None))

    return actions, open_pos, realized, halted


def one_pass(equity: float, eq_src: str, hardened: bool, risk_pct: float, daily_halt: float,
             logged: set) -> bool:
    """Run a single snapshot; log new would-be orders. Returns True if session open."""
    log = get_logger()
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()
    expiry = dt.datetime.combine(today, CLOSE_UTC, tzinfo=dt.timezone.utc)

    bars = {s: [b for b in ID.fetch_bars(s, "5m", "1d") if b.ts.date() == today]
            for s in ID.INTRADAY_UNIVERSE}
    bars = {s: v for s, v in bars.items() if v}
    if not bars:
        log.info("No bars for today yet (market not open?).")
        return now.time() < CLOSE_UTC
    vix = ID.vix_by_date("5d")
    iv_day = vix.get(today) or vix.get(max(vix)) or 18.0

    actions, open_pos, realized, halted = replay_today(
        bars, iv_day, equity, expiry, hardened, risk_pct, daily_halt)

    # log only NEW actions since last poll
    for kind, ts, s, sp, pnl, reason in actions:
        key = (kind, ts.isoformat(), s)
        if key in logged:
            continue
        logged.add(key)
        if kind == "ENTER":
            log.info("[PAPER ENTER] %s %s %s spread  %d x  debit %.2f  max_loss $%.0f  @ %s",
                     s, "BULL" if sp.direction == "bull" else "BEAR",
                     "call" if sp.direction == "bull" else "put", sp.contracts, sp.debit,
                     sp.max_loss, ts.strftime("%H:%M"))
            log_payload(log, "  would-submit payload (NOT sent, LIVE_TRADING off):",
                        order_payload(sp, today))
        else:
            log.info("[PAPER EXIT ] %s %s  P/L $%+.0f  (%s)  @ %s",
                     s, sp.direction.upper(), pnl, reason, ts.strftime("%H:%M"))

    # current status
    mtm = 0.0
    last_close = {s: bars[s][-1].close for s in bars}
    for s, sp in open_pos.items():
        mtm += sp.pnl(last_close[s], ID._iv(iv_day, s), now)
    eq_now = equity + realized + mtm
    halt_note = "  [DAILY HALT HIT]" if halted else ""
    log.info(f"STATUS {now:%H:%M} | equity ${eq_now:,.0f} (start ${equity:,.0f} from {eq_src}) | "
             f"realized ${realized:+,.0f} | open {len(open_pos)} (MTM ${mtm:+,.0f}){halt_note}")
    for s, sp in open_pos.items():
        log.info("   holding %s %s  %dx  entry-debit %.2f  now %.2f",
                 s, sp.direction.upper(), sp.contracts, sp.debit,
                 sp.value(last_close[s], ID._iv(iv_day, s), now))
    return now.time() < CLOSE_UTC


def main() -> int:
    ap = argparse.ArgumentParser(description="Live PAPER runner (LIVE_TRADING off)")
    ap.add_argument("--loop", action="store_true", help="poll until ~market close")
    ap.add_argument("--interval", type=int, default=300, help="poll seconds (default 300)")
    ap.add_argument("--risk", type=float, default=0.03)
    ap.add_argument("--daily-halt", type=float, default=0.05)
    ap.add_argument("--naive", action="store_true")
    args = ap.parse_args()

    logger, path = setup_logging(tag="intraday_live")
    assert not config.LIVE_TRADING, "LIVE_TRADING must be False for the paper runner"
    logger.info("LIVE PAPER runner | hardened=%s risk=%.0f%% halt=-%.0f%% | LIVE_TRADING=False",
                not args.naive, args.risk * 100, args.daily_halt * 100)
    equity, eq_src = account_equity()

    logged: set = set()
    open_session = one_pass(equity, eq_src, not args.naive, args.risk, args.daily_halt, logged)
    if not args.loop:
        logger.info("Single pass done. Use --loop to paper-run the rest of the session. Log: %s", path)
        return 0

    while open_session:
        time.sleep(args.interval)
        try:
            open_session = one_pass(equity, eq_src, not args.naive, args.risk, args.daily_halt, logged)
        except Exception as e:
            logger.warning("poll error (continuing): %s", e)
    logger.info("Session closed (>= 20:00 UTC). Paper run complete. Log: %s", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
