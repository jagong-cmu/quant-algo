#!/usr/bin/env python3
"""Backtest the aggressive short-DTE intraday momentum strategy on recent
5-minute data and report trades + how the money moved.

Black-Scholes-MODELED options off the underlying + VIX (no real chain on the
free tier): direction reflects the real intraday price path; dollar magnitudes
are approximate -- especially for 0DTE, where real IV/skew differs sharply from a
flat VIX input. LIVE_TRADING stays False.

Usage:
    python intraday_backtest.py
    python intraday_backtest.py --interval 5m --range 1mo
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import os
import sys

from pp_options import intraday as ID

REPORT_DIR = "reports"


def max_drawdown(curve):
    peak = -math.inf; mdd = 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, (peak - eq) / peak)
    return mdd


def main() -> int:
    ap = argparse.ArgumentParser(description="Aggressive intraday momentum backtest")
    ap.add_argument("--interval", default=ID.BAR_INTERVAL)
    ap.add_argument("--range", default="1mo")
    ap.add_argument("--equity", type=float, default=100_000.0)
    # Defaults reflect the chosen, validated config: hardened mean-reversion,
    # moderate 3% sizing, -5% daily circuit breaker.
    ap.add_argument("--signal", choices=["momentum", "mean_reversion"], default="mean_reversion")
    ap.add_argument("--risk", type=float, default=0.03,
                    help="per-trade max-loss fraction of equity")
    ap.add_argument("--daily-halt", type=float, default=0.05,
                    help="halt new entries after this daily loss fraction")
    ap.add_argument("--naive", action="store_true",
                    help="disable hardening (trend filter / time-stop / cooldown)")
    ap.add_argument("--days", type=int, default=0,
                    help="fetch this many calendar days of 5m bars (max ~60); 0 = use --range")
    ap.add_argument("--dte", type=int, default=ID.INTRADAY_DTE,
                    help="0 = 0-DTE flat-IV version (FRAGILE); >0 = short-dated on the "
                         "validated competition surface (TRUSTWORTHY, default 5)")
    args = ap.parse_args()
    ID.INTRADAY_DTE = args.dte   # 0 = 0-DTE flat-IV version | >0 = short-dated surface version

    series = vix = None
    win = args.range
    if args.days:
        import time
        now = int(time.time()); p1 = now - args.days * 86400
        series = {s: ID.fetch_bars(s, args.interval, period1=p1, period2=now)
                  for s in ID.INTRADAY_UNIVERSE}
        vix = ID.vix_by_date("6mo")
        win = f"{args.days}d"

    print(f"Intraday {args.signal} backtest | {'naive' if args.naive else 'HARDENED'} | "
          f"interval={args.interval} window={win} risk={args.risk:.0%}/trade "
          f"halt=-{args.daily_halt:.0%} universe={ID.INTRADAY_UNIVERSE}")
    print("Fetching bars and simulating (Black-Scholes-modeled options) ...")
    res = ID.backtest(args.equity, args.interval, args.range, signal=args.signal,
                      risk_pct=args.risk, hardened=not args.naive, daily_halt_pct=args.daily_halt,
                      series=series, vix=vix)

    trades = res.trades
    if not trades:
        print("No trades generated in the sample window.")
        return 0
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    end_eq = args.equity + total
    n_days = len(res.days)
    mdd = max_drawdown(res.equity_curve)

    # daily summary
    os.makedirs(REPORT_DIR, exist_ok=True)
    stamp = res.days[-1].day.isoformat() if res.days else "now"
    csv_path = os.path.join(REPORT_DIR, f"intraday_trades_{stamp}.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["entry_ts", "exit_ts", "symbol", "direction", "contracts", "debit",
                    "width", "pnl", "exit_reason", "max_loss"])
        for t in trades:
            w.writerow([t.entry_ts.isoformat(), t.exit_ts.isoformat(), t.symbol, t.direction,
                        t.contracts, f"{t.debit:.2f}", t.width, f"{t.pnl:.0f}", t.exit_reason,
                        f"{t.max_loss:.0f}"])

    print("\n" + "=" * 80)
    print(f"RESULT  {res.days[0].day} -> {res.days[-1].day}  ({n_days} trading days)")
    print("=" * 80)
    print(f"  Start ${args.equity:,.0f} -> End ${end_eq:,.0f}   "
          f"Net P/L ${total:+,.0f} ({total/args.equity:+.1%})")
    print(f"  Trades: {len(trades)}  (~{len(trades)/max(n_days,1):.0f}/day)  "
          f"| hit rate {len(wins)/len(trades):.0%}  | max drawdown {mdd:.1%}")
    if wins:
        print(f"  avg win ${sum(wins)/len(wins):,.0f}  | "
              f"avg loss ${(sum(losses)/len(losses) if losses else 0):,.0f}  | "
              f"worst trade ${min(pnls):,.0f}  | best ${max(pnls):,.0f}")
    halted = [d for d in res.days if d.halted]
    print(f"  Days hitting the -{ID.DAILY_LOSS_HALT_PCT:.0%} circuit breaker: {len(halted)}/{n_days}")

    # exit-reason breakdown
    from collections import Counter
    reasons = Counter(t.exit_reason for t in trades)
    print("  Exit reasons:", ", ".join(f"{k}={v}" for k, v in reasons.most_common()))

    print("\n  Day-by-day:")
    print(f"  {'date':<12}{'trades':>7}{'P/L':>12}{'end equity':>14}{'':>4}")
    for d in res.days:
        flag = "  HALTED" if d.halted else ""
        print(f"  {str(d.day):<12}{len(d.trades):>7}{d.end_equity-d.start_equity:>+12,.0f}"
              f"{d.end_equity:>14,.0f}{flag}")

    print(f"\n  Trade log CSV: {csv_path}")
    from pp_options import intraday as _ID
    if _ID.INTRADAY_DTE > 0:
        print(f"\n  PRICING: {_ID.INTRADAY_DTE}-DTE options on the calibrated competition skew "
              "surface (state/iv_surface_short.json).")
        print("  Faithful to PentPort's BS pricing (no bid/ask, no fees); skew shape is a snapshot.")
    else:
        print("\n  NOTE: 0DTE priced at a FLAT IV -- the 0-DTE backtest is wildly IV-sensitive; "
              "magnitudes are NOT trustworthy.")
    print("  LIVE_TRADING=False.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
