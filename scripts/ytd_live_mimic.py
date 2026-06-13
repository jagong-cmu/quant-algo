#!/usr/bin/env python3
"""YTD backtest mimicking the live runner config, at $1M equity, weekly entries.

Mirrors the LIVE path: SPY/QQQ/IWM universe, 30-delta short puts, call-debit when
above 200d SMA, 3%/trade + 20%/book caps, VIX-gate sizing (StaticSizer == what
engine.py uses live). Start = 2026-01-01. Higher equity + weekly cadence = more
trades / overlapping concurrent positions like the 20-concurrent live runner.

Fidelity gaps (stated honestly): the sim HOLDS to expiry, whereas the live runner
manages early (50% PT / 2x stop / 21-DTE). Options are Black-Scholes-modeled.
"""
import datetime as dt
from pp_options import simulate as S
from pp_options import metrics as M

# --- raise equity to mimic a larger live account ---
S.EQUITY = 1_000_000.0

START = dt.date(2026, 1, 1)
CADENCE = 5     # weekly entries -> more trades, overlapping cycles
DTE = 37        # within live DTE_MIN/MAX 30..45

md = S.MarketData(rng="2y")
end = md.last_date
res = S.simulate(md, START, end, S.StaticSizer(), cadence_days=CADENCE, dte=DTE,
                 include_open=True)
m = M.compute(res)

print("=" * 84)
print("YTD BACKTEST — LIVE-CONFIG MIMIC (StaticSizer = live engine path)")
print(f"Equity ${S.EQUITY:,.0f} | universe SPY/QQQ/IWM | weekly entries (cadence {CADENCE}d) | dte {DTE}d")
print(f"Window {START} -> {end} ({len(md.trading_days(START, end))} trading days)")
print("Caps: 3%/trade, 20%/book (per-cycle in sim). Options Black-Scholes-modeled.")
print("=" * 84)

n_put = sum(1 for t in res.trades if t.kind == "put_credit")
n_call = sum(1 for t in res.trades if t.kind == "call_debit")
realized = [t for t in res.trades if not getattr(t, "open_at_end", False)]
open_end = [t for t in res.trades if getattr(t, "open_at_end", False)]
total_pl = sum(t.realized_pnl for t in res.trades)
credit = sum(t.entry_net * 100 * t.contracts for t in res.trades if t.kind == "put_credit")
risk_open = sum(t.max_loss for t in res.trades)

print(f"\nEntry cycles: {len(res.entries)} | total spreads: {len(res.trades)} "
      f"(put_credit {n_put}, call_debit {n_call})")
print(f"  realized (expired in-window): {len(realized)} | still-open at end (MTM): {len(open_end)}")
print(f"\nTOTAL P/L (realized + MTM): ${total_pl:+,.0f}  ({total_pl/S.EQUITY:+.2%} of equity)")
print(f"Put-credit collected: ${credit:,.0f} | defined max-risk across all spreads: ${risk_open:,.0f}")

print("\n--- performance metrics ---")
def row(lbl, v, money=False, pct=False):
    if v is None: s = "n/a"
    elif pct: s = f"{v:+.1%}"
    elif money: s = f"${v:,.0f}"
    else: s = f"{v:.2f}"
    print(f"  {lbl:<22}{s:>16}")
row("return", m.ret_pct, pct=True)
row("CAGR (annualized)", m.cagr, pct=True)
row("max drawdown", m.max_dd, pct=True)
row("hit rate", m.hit_rate, pct=True)
row("trades", float(m.n_trades))
row("avg win", m.avg_win, money=True)
row("avg loss", m.avg_loss, money=True)
row("worst trade", m.worst_trade, money=True)
row("worst day ($)", m.worst_day_dollar, money=True)
row("95% loss (VaR)", m.var95_loss, money=True)
row("99% loss (VaR)", m.var99_loss, money=True)
row("Sharpe*", m.sharpe)
row("Sortino*", m.sortino)

# monthly P/L trajectory from the daily equity curve
print("\n--- monthly equity trajectory (modeled) ---")
from collections import OrderedDict
month_last = OrderedDict()
for d, eq in res.daily_equity:
    month_last[(d.year, d.month)] = eq
prev = S.EQUITY
print(f"  {'month':<10}{'equity':>16}{'Δ month':>14}{'cum P/L':>16}")
for (y, mo), eq in month_last.items():
    print(f"  {y}-{mo:02d}{'':<3}{eq:>16,.0f}{eq-prev:>+14,.0f}{eq-S.EQUITY:>+16,.0f}")
    prev = eq
print("=" * 84)
print("NOTE: sim HOLDS to expiry; live runner manages early (50% PT / 2x stop / 21-DTE),")
print("so live exits would differ. Magnitudes approximate; win/loss direction is the real path.")
