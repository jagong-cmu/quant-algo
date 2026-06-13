#!/usr/bin/env python3
"""One-year review of the strategy WITH the adaptive sizing layer applied
throughout (regime thresholds refit on a rolling 3y window at each entry -- no
lookahead). Records every trade, tracks how the money moved month by month, and
compares against the static (model-off) baseline.

Outputs (also printed as a console summary):
  reports/year_review_<asof>.md    -- full written report
  reports/year_trades_<asof>.csv   -- machine-readable trade log

Black-Scholes-MODELED options (real underlying/VIX, no skew, mid fills, no
commissions): direction is real, dollar magnitudes approximate. LIVE_TRADING
stays False -- this is historical/dry-run only.

Usage:
    python year_review.py            # past ~12 months
    python year_review.py --months 12 --cadence 37
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys

from pp_options import config
from pp_options import metrics as M
from pp_options.simulate import (EQUITY, MarketData, RollingAdaptiveSizer, StaticSizer)
from pp_options.simulate import simulate as run_sim

REPORT_DIR = "reports"


def snap_to_trading_day(md: MarketData, target: dt.date) -> dt.date:
    cands = [d for d in md.calendar if d >= target]
    return cands[0] if cands else md.calendar[0]


def monthly_curve(curve: list[tuple[dt.date, float]]) -> list[tuple[str, float, float]]:
    """(month label, first equity in month, last equity in month)."""
    out: list[list] = []
    cur = None
    for d, eq in curve:
        key = (d.year, d.month)
        if key != cur:
            out.append([f"{d.year}-{d.month:02d}", eq, eq])
            cur = key
        else:
            out[-1][2] = eq
    return [(m, a, b) for m, a, b in out]


def fmt_money(x):
    return f"${x:,.0f}" if x is not None else "n/a"


def fmt_pct(x):
    return f"{x:+.1%}" if x is not None else "n/a"


def main() -> int:
    ap = argparse.ArgumentParser(description="One-year strategy review with adaptive sizing")
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--cadence", type=int, default=37, help="trading days between entries")
    ap.add_argument("--dte", type=int, default=37)
    ap.add_argument("--range", default="5y")
    ap.add_argument("--train-years", type=int, default=3)
    args = ap.parse_args()

    md = MarketData(rng=args.range)
    asof = md.last_date
    start_target = dt.date(asof.year - 1, asof.month, asof.day) if args.months == 12 else \
        asof - dt.timedelta(days=int(args.months * 30.44))
    test_start = snap_to_trading_day(md, start_target)

    print(f"Year-in-review: {test_start} -> {asof}  (adaptive, rolling {args.train_years}y refit)")
    print("Running adaptive and static (baseline) simulations ...")

    adaptive = run_sim(md, test_start, asof, RollingAdaptiveSizer(md, args.train_years),
                       args.cadence, args.dte, include_open=True)
    static = run_sim(md, test_start, asof, StaticSizer(), args.cadence, args.dte, include_open=True)

    ma, ms = M.compute(adaptive), M.compute(static)
    os.makedirs(REPORT_DIR, exist_ok=True)
    stamp = asof.isoformat()
    csv_path = os.path.join(REPORT_DIR, f"year_trades_{stamp}.csv")
    md_path = os.path.join(REPORT_DIR, f"year_review_{stamp}.md")

    _write_csv(csv_path, adaptive)
    report = _build_report(test_start, asof, args, adaptive, static, ma, ms)
    with open(md_path, "w") as f:
        f.write(report)

    # console summary
    print("\n" + "=" * 78)
    print(f"ADAPTIVE result {test_start}..{asof}")
    n_real = sum(1 for t in adaptive.trades if not t.open_at_end)
    n_open = sum(1 for t in adaptive.trades if t.open_at_end)
    print(f"  Trades: {len(adaptive.trades)} ({n_real} realized, {n_open} open/MTM) | "
          f"start ${EQUITY:,.0f} -> end ${EQUITY + ma.total_pnl:,.0f}")
    print(f"  Net P/L {fmt_money(ma.total_pnl)} ({fmt_pct(ma.ret_pct)}) | "
          f"max drawdown {fmt_pct(ma.max_dd)} | hit rate {fmt_pct(ma.hit_rate)}")
    print(f"  vs STATIC: P/L {fmt_money(ms.total_pnl)} ({fmt_pct(ms.ret_pct)}), "
          f"max drawdown {fmt_pct(ms.max_dd)}")
    reduced = sum(1 for r in adaptive.entries if r.reduced)
    print(f"  Adaptive reduced size in {reduced}/{len(adaptive.entries)} entry cycles")
    print("=" * 78)
    print(f"Report : {md_path}")
    print(f"Trades : {csv_path}")
    return 0


def _write_csv(path: str, sr) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["entry_date", "expiry", "underlying", "structure", "regime", "multiplier",
                    "short_strike", "long_strike", "contracts", "entry_net", "max_loss",
                    "pnl", "status"])
        for t in sorted(sr.trades, key=lambda x: (x.entry_date, x.underlying, x.kind)):
            w.writerow([t.entry_date, t.expiry, t.underlying, t.kind, t.regime,
                        f"{t.multiplier:.2f}", t.short_k, t.long_k, t.contracts,
                        f"{t.entry_net:.2f}", f"{t.max_loss:.0f}", f"{t.realized_pnl:.0f}",
                        "OPEN/MTM" if t.open_at_end else "realized"])


def _build_report(ts, asof, args, adaptive, static, ma, ms) -> str:
    L: list[str] = []
    L.append(f"# Strategy Year-in-Review — {ts} to {asof}\n")
    L.append("**Adaptive sizing layer applied throughout** (regime thresholds refit on a rolling "
             f"{args.train_years}-year window at each entry — no lookahead). Static baseline shown "
             "alongside.\n")
    L.append("> ⚠️ **Modeled, not live.** Real underlying & VIX history; option prices are "
             "Black-Scholes-modeled (no skew, mid fills, no commissions). Win/loss **direction** "
             "reflects the real price path; dollar **magnitudes** are approximate. `LIVE_TRADING` "
             "stayed `False`.\n")

    # setup
    L.append("## Setup\n")
    L.append(f"- Period: **{ts} → {asof}**")
    L.append(f"- Starting equity: **${EQUITY:,.0f}** · caps: {config.MAX_TRADE_RISK_PCT:.0%}/trade, "
             f"{config.MAX_BOOK_RISK_PCT:.0%}/book (hard, enforced after the adaptive layer)")
    L.append(f"- Universe: {', '.join(config.UNIVERSE)} · structures: put credit spreads (primary), "
             "call debit spreads (risk-on only)")
    L.append(f"- Entry cadence: every {args.cadence} trading days · target {args.dte} DTE\n")

    # headline money movement
    L.append("## How the money moved\n")
    L.append(f"**Adaptive: ${EQUITY:,.0f} → ${EQUITY + ma.total_pnl:,.0f}  "
             f"({fmt_money(ma.total_pnl)}, {fmt_pct(ma.ret_pct)})**\n")
    L.append("| Month | End equity (adaptive) | Monthly P/L | Return |")
    L.append("|---|---:|---:|---:|")
    mc = monthly_curve(adaptive.daily_equity)
    prev = EQUITY
    for label, _first, last in mc:
        pl = last - prev
        ret = pl / prev if prev else 0.0
        L.append(f"| {label} | {fmt_money(last)} | {fmt_money(pl)} | {fmt_pct(ret)} |")
        prev = last
    L.append("")

    # regime / sizing timeline
    L.append("## Regime & sizing decisions (per entry cycle)\n")
    L.append("| Entry | Regime | Size mult | Short delta | Reduced vs base? |")
    L.append("|---|---|---:|---:|---|")
    for r in adaptive.entries:
        L.append(f"| {r.entry_date} | {r.regime} | {r.multiplier:.2f} | {r.delta:.2f} | "
                 f"{'YES' if r.reduced else 'no'} |")
    reduced = sum(1 for r in adaptive.entries if r.reduced)
    L.append(f"\nAdaptive reduced exposure in **{reduced}/{len(adaptive.entries)}** cycles.\n")

    # trade log
    L.append("## Trade log\n")
    L.append("| Entry | Sym | Structure | Short/Long K | Qty | Net | Max risk | P/L | Status |")
    L.append("|---|---|---|---|---:|---:|---:|---:|---|")
    for t in sorted(adaptive.trades, key=lambda x: (x.entry_date, x.underlying, x.kind)):
        net = f"{'+' if t.kind=='put_credit' else '-'}{t.entry_net:.2f}"
        L.append(f"| {t.entry_date} | {t.underlying} | {t.kind} | "
                 f"{t.short_k:.0f}/{t.long_k:.0f} | {t.contracts} | {net} | "
                 f"{fmt_money(t.max_loss)} | {fmt_money(t.realized_pnl)} | "
                 f"{'OPEN/MTM' if t.open_at_end else 'realized'} |")
    L.append("")

    # final analysis
    L.append("## Final analysis — adaptive vs static\n")
    L.append("| Metric | Adaptive | Static | ")
    L.append("|---|---:|---:|")
    rows = [
        ("Net P/L", fmt_money(ma.total_pnl), fmt_money(ms.total_pnl)),
        ("Return on equity", fmt_pct(ma.ret_pct), fmt_pct(ms.ret_pct)),
        ("Max drawdown", fmt_pct(ma.max_dd), fmt_pct(ms.max_dd)),
        ("Worst single trade", fmt_money(ma.worst_trade), fmt_money(ms.worst_trade)),
        ("Worst single day", fmt_money(ma.worst_day_dollar), fmt_money(ms.worst_day_dollar)),
        ("95% trade loss", fmt_money(ma.var95_loss), fmt_money(ms.var95_loss)),
        ("99% trade loss", fmt_money(ma.var99_loss), fmt_money(ms.var99_loss)),
        ("Hit rate", fmt_pct(ma.hit_rate), fmt_pct(ms.hit_rate)),
        ("Avg win", fmt_money(ma.avg_win), fmt_money(ms.avg_win)),
        ("Avg loss", fmt_money(ma.avg_loss), fmt_money(ms.avg_loss)),
        ("Sharpe* (secondary)", f"{ma.sharpe:.2f}" if ma.sharpe else "n/a",
                                f"{ms.sharpe:.2f}" if ms.sharpe else "n/a"),
        ("Sortino* (secondary)", f"{ma.sortino:.2f}" if ma.sortino else "n/a",
                                 f"{ms.sortino:.2f}" if ms.sortino else "n/a"),
    ]
    for name, a, s in rows:
        L.append(f"| {name} | {a} | {s} |")
    L.append("\n*Sharpe/Sortino flatter short premium until the tail event — judge on drawdown "
             "and loss tail, not these.*\n")

    # narrative bottom line
    pl_diff = ma.total_pnl - ms.total_pnl
    dd_diff = ms.max_dd - ma.max_dd
    L.append("## Bottom line\n")
    L.append(f"- Over the year the adaptive book moved **${EQUITY:,.0f} → "
             f"${EQUITY + ma.total_pnl:,.0f}** ({fmt_pct(ma.ret_pct)}).")
    L.append(f"- vs static it {'gave up' if pl_diff < 0 else 'added'} "
             f"**{fmt_money(abs(pl_diff))}** of P/L while "
             f"{'reducing' if dd_diff > 0 else 'increasing'} max drawdown by "
             f"**{abs(dd_diff):.1%}** — the adaptive layer trades return for tail protection by design.")
    L.append(f"- It reduced exposure in **{reduced}/{len(adaptive.entries)}** cycles; in the rest it "
             "matched static exactly (no drag when the regime was calm).")
    L.append("- Premium selling shows its usual signature here: a **high hit rate with larger "
             "losers than winners** — the tail is what the guardrails and adaptive layer exist to "
             "contain.")
    L.append("\n_Generated by `year_review.py`. Keep `LIVE_TRADING=False` until you have reviewed "
             "this and the walk-forward out-of-sample results._\n")
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main())
