#!/usr/bin/env python3
"""YTD backtest — LIVE-FAITHFUL + EARLY MANAGEMENT.

Adds the runner's exit policy (runner.py:233-258) on top of the global 20% book
cap: each day every open spread is marked-to-model and closed on the FIRST of
  * profit target : captured >= 50% of max profit                (both kinds)
  * stop-loss     : loss >= 2x credit received                   (put_credit only,
                    exactly as runner.py; debit spreads have no credit stop)
  * time stop     : DTE <= 21
  * hard backstop : DTE <= 7   (rarely fires; time stop hits first)
Anything still open at the last sim day is marked-to-model (not realized).

Mirrors the live engine path otherwise: SPY/QQQ/IWM, 30d short puts + call-debit
above 200d SMA, StaticSizer (= engine.py), 3%/trade + 20%/book caps. $100k equity,
weekly entries from 2026-01-01. Options Black-Scholes-modeled.
"""
import datetime as dt
from collections import defaultdict, OrderedDict

from pp_options import chain as chainmod
from pp_options import config, guardrails
from pp_options import simulate as S
from pp_options import metrics as M
from pp_options.risk import BookRiskTracker
from pp_options.strategy import build_put_credit_spread, build_call_debit_spread

import os
from pp_options import ivsurface
S.EQUITY = 100_000.0
EQUITY = S.EQUITY
config.MAX_BOOK_RISK_PCT = float(os.environ.get("BOOK_CAP", config.MAX_BOOK_RISK_PCT))  # 0.20 default
IV_MODE = os.environ.get("IV_SURFACE", "on").lower()
if IV_MODE in ("off", "0", "flat"):
    ivsurface.load = lambda: None       # force legacy flat VIX-based IV
SURFACE_ON = ivsurface.load() is not None
START = dt.date(2026, 1, 1)
CADENCE = 5
DTE = 37
PROFIT_TARGET_FRAC = 0.50
STOP_LOSS_MULT = 2.0
MANAGE_DTE = 21
EXIT_DTE = 7


def max_profit_total(pos):
    if pos.kind == "put_credit":
        return pos.entry_net * 100 * pos.contracts
    width = abs(pos.short_k - pos.long_k)
    return (width - pos.entry_net) * 100 * pos.contracts


def exit_reason(pos, S_now, iv_now, d, vix=None):
    """Return (reason, pnl) if the spread should close today, else (None, pnl)."""
    pnl = pos.pnl(S_now, iv_now, d, vix=vix)
    dte = (pos.expiry - d).days
    mp = max_profit_total(pos)
    credit_total = pos.entry_net * 100 * pos.contracts  # only meaningful for put_credit
    if mp > 0 and pnl >= PROFIT_TARGET_FRAC * mp:
        return f"profit target (>={PROFIT_TARGET_FRAC:.0%} max profit)", pnl
    if pos.kind == "put_credit" and pnl <= -STOP_LOSS_MULT * credit_total:
        return f"stop-loss (loss>={STOP_LOSS_MULT:.0f}x credit)", pnl
    if dte <= MANAGE_DTE:
        return f"time stop (DTE<={MANAGE_DTE})", pnl
    if dte <= EXIT_DTE:
        return f"hard backstop (DTE<={EXIT_DTE})", pnl
    return None, pnl


md = S.MarketData(rng="2y")
END = md.last_date
cal = md.trading_days(START, END)
tracker = BookRiskTracker(equity=EQUITY, open_risk=0.0)
entry_dates = [cal[i] for i in range(0, len(cal), CADENCE)]
entry_set = set(entry_dates)

open_pos = []          # [Position, release_amount]
ledger = []
skips = []
realized = 0.0
daily_equity = []
peak_concurrent = 0
peak_book = 0.0
exit_reasons = defaultdict(int)


def rec(pos, pl, asof, closed, reason, held):
    ledger.append(dict(underlying=pos.underlying, kind=pos.kind, entry=pos.entry_date,
                       expiry=pos.expiry, short_k=pos.short_k, long_k=pos.long_k,
                       contracts=pos.contracts, entry_net=pos.entry_net, max_loss=pos.max_loss,
                       pnl=pl, asof=asof, closed=closed, reason=reason, days=held))


def try_commit(spread, kind, u, entry, expiry):
    if spread is None:
        skips.append((entry, u, kind, "no viable spread"))
        return
    if guardrails.evaluate_order(spread, EQUITY, tracker).blocked:
        reason = "book cap (20%)" if tracker.would_breach(spread.total_max_loss) else "per-trade/sanity"
        skips.append((entry, u, kind, reason))
        return
    tracker.add(spread.to_payload())
    entry_net = spread.net_credit if kind == "put_credit" else spread.net_debit
    pos = S.Position(underlying=u, kind=kind, short_k=spread.short_leg.option.strike,
                     long_k=spread.long_leg.option.strike, contracts=spread.contracts,
                     entry_net=entry_net, max_loss=spread.total_max_loss,
                     entry_date=entry, expiry=expiry)
    open_pos.append([pos, spread.total_max_loss])


last_expiry = max((e + dt.timedelta(days=DTE) for e in entry_dates), default=END)
sim_days = [d for d in md.calendar if START <= d <= min(last_expiry, md.last_date)]

for d in sim_days:
    if d in entry_set:
        vix_e = md.close("^VIX", d)
        vix_prev = md.close("^VIX", md.prev_trading_day(d)) if md.prev_trading_day(d) else None
        base = guardrails.vix_gate(vix_e, vix_prev)
        base_scale = base.risk_scale if base.short_premium_allowed else 0.0
        if base_scale <= 0.0:
            skips.append((d, "ALL", "cycle", f"VIX gate closed (VIX {vix_e:.1f})"))
        else:
            expiry = d + dt.timedelta(days=DTE)
            for u in config.UNIVERSE:
                hit = md.on_or_before(u, d)
                if hit is None:
                    continue
                _, S0 = hit
                iv0 = S.iv_from_vix(u, vix_e)
                norm = chainmod.normalize(S.build_raw_chain(u, S0, iv0, expiry, DTE, vix=vix_e), u, S0)
                sp, _ = build_put_credit_spread(norm, u, EQUITY, base_scale,
                                                target_short_delta=S.BASE_DELTA)
                try_commit(sp, "put_credit", u, d, expiry)
                sma0 = S.sma(md.closes_up_to(u, d), config.TREND_SMA_DAYS)
                if sma0 is not None and S0 > sma0:
                    cd, _ = build_call_debit_spread(norm, u, EQUITY, risk_scale=base_scale)
                    try_commit(cd, "call_debit", u, d, expiry)

    vix_d = md.close("^VIX", d) or 18.0
    still = []
    for pos, rel in open_pos:
        held = (min(pos.expiry, d) - pos.entry_date).days
        if pos.expiry <= d:                       # expired
            Sx = md.close(pos.underlying, d) or md.close(pos.underlying, pos.expiry)
            pl = pos.pnl(Sx, S.iv_from_vix(pos.underlying, vix_d), pos.expiry, vix=vix_d)
            realized += pl
            tracker.open_risk -= rel
            rec(pos, pl, d, True, "expired", held)
            exit_reasons["expired"] += 1
            continue
        Sx = md.close(pos.underlying, d)
        if Sx is None:
            still.append([pos, rel]); continue
        reason, pl = exit_reason(pos, Sx, S.iv_from_vix(pos.underlying, vix_d), d, vix=vix_d)
        if reason:                                # early-managed close
            realized += pl
            tracker.open_risk -= rel
            rec(pos, pl, d, True, reason, held)
            exit_reasons[reason.split(" (")[0]] += 1
        else:
            still.append([pos, rel])
    open_pos = still

    peak_concurrent = max(peak_concurrent, len(open_pos))
    peak_book = max(peak_book, tracker.open_risk)
    mtm = sum(pos.pnl(md.close(pos.underlying, d), S.iv_from_vix(pos.underlying, vix_d), d, vix=vix_d)
              for pos, _ in open_pos if md.close(pos.underlying, d) is not None)
    daily_equity.append((d, EQUITY + realized + mtm))

if daily_equity:
    last_day = daily_equity[-1][0]
    vix_d = md.close("^VIX", last_day) or 18.0
    for pos, _ in open_pos:
        Sx = md.close(pos.underlying, last_day)
        if Sx is None:
            continue
        held = (min(pos.expiry, last_day) - pos.entry_date).days
        rec(pos, pos.pnl(Sx, S.iv_from_vix(pos.underlying, vix_d), last_day, vix=vix_d),
            last_day, False, "open@end", held)
        exit_reasons["open@end"] += 1

# ---- metrics ----
trades = [S.TradeResult(underlying=r["underlying"], kind=r["kind"], entry_date=r["entry"],
                        expiry=r["expiry"], contracts=r["contracts"], max_loss=r["max_loss"],
                        realized_pnl=r["pnl"], regime="", reduced=False, short_k=r["short_k"],
                        long_k=r["long_k"], entry_net=r["entry_net"],
                        open_at_end=not r["closed"]) for r in ledger]
m = M.compute(S.SimResult(daily_equity=daily_equity, trades=trades, entries=[]))

# ================= OUTPUT =================
W = 104
print("=" * W)
print("YTD BACKTEST — LIVE-FAITHFUL + EARLY MANAGEMENT (50% PT / 2x stop / 21-DTE / 7-DTE backstop)")
print(f"Equity ${EQUITY:,.0f} | SPY/QQQ/IWM | weekly entries (cadence {CADENCE}d) | dte {DTE}d | "
      f"global {config.MAX_BOOK_RISK_PCT:.0%} book cap | IV={'CALIBRATED SURFACE' if SURFACE_ON else 'flat VIX'}")
print(f"Window {START} -> {END} ({len(cal)} trading days)")
print("=" * W)

total_pl = sum(r["pnl"] for r in ledger)
n_put = sum(1 for r in ledger if r["kind"] == "put_credit")
n_call = sum(1 for r in ledger if r["kind"] == "call_debit")
wins = [r for r in ledger if r["pnl"] > 0]
losses = [r for r in ledger if r["pnl"] <= 0]
gross_win = sum(r["pnl"] for r in wins)
gross_loss = -sum(r["pnl"] for r in losses)
avg_hold = sum(r["days"] for r in ledger) / len(ledger) if ledger else 0

print(f"\nSpreads OPENED: {len(ledger)}  (put_credit {n_put}, call_debit {n_call}) | "
      f"skipped by guardrails: {len(skips)} (book-cap {sum(1 for s in skips if 'book' in s[3])})")
print(f"Peak concurrent: {peak_concurrent} | peak book risk: ${peak_book:,.0f} "
      f"({peak_book/EQUITY:.1%} of equity) | avg holding: {avg_hold:.0f} days (vs {DTE} to expiry)")
print(f"\nTOTAL P/L: ${total_pl:+,.0f}  ({total_pl/EQUITY:+.2%} of equity) | "
      f"profit factor {(gross_win/gross_loss if gross_loss else float('inf')):.2f} "
      f"(win ${gross_win:,.0f} / loss ${gross_loss:,.0f})")

print("\n--- exit reasons (how positions closed) ---")
for r, n in sorted(exit_reasons.items(), key=lambda x: -x[1]):
    pl = sum(t["pnl"] for t in ledger if t["reason"].split(" (")[0] == r)
    print(f"  {n:>3}  {r:<28} P/L ${pl:>+12,.0f}")

print("\n--- performance metrics ---")
def line(lbl, v, money=False, pct=False):
    s = "n/a" if v is None else (f"{v:+.1%}" if pct else (f"${v:,.0f}" if money else f"{v:.2f}"))
    print(f"  {lbl:<22}{s:>16}")
for lbl, v, mo, pc in [("return", m.ret_pct, 0, 1), ("CAGR (annualized)", m.cagr, 0, 1),
                       ("max drawdown", m.max_dd, 0, 1), ("hit rate", m.hit_rate, 0, 1),
                       ("avg win", m.avg_win, 1, 0), ("avg loss", m.avg_loss, 1, 0),
                       ("worst trade", m.worst_trade, 1, 0), ("worst day ($)", m.worst_day_dollar, 1, 0),
                       ("95% loss (VaR)", m.var95_loss, 1, 0), ("99% loss (VaR)", m.var99_loss, 1, 0),
                       ("Sharpe*", m.sharpe, 0, 0), ("Sortino*", m.sortino, 0, 0)]:
    line(lbl, v, money=mo, pct=pc)

def breakdown(title, keyfn):
    print(f"\n--- by {title} ---")
    groups = defaultdict(list)
    for r in ledger:
        groups[keyfn(r)].append(r)
    print(f"  {'group':<14}{'n':>4}{'wins':>6}{'hit%':>7}{'P/L':>14}{'avg/trade':>12}")
    for g in sorted(groups):
        rs = groups[g]; w = sum(1 for r in rs if r["pnl"] > 0); pl = sum(r["pnl"] for r in rs)
        print(f"  {str(g):<14}{len(rs):>4}{w:>6}{100*w/len(rs):>6.0f}%{pl:>+14,.0f}{pl/len(rs):>+12,.0f}")

breakdown("structure", lambda r: r["kind"])
breakdown("underlying", lambda r: r["underlying"])
breakdown("entry month", lambda r: f"{r['entry'].year}-{r['entry'].month:02d}")

print("\n--- monthly equity trajectory (modeled) ---")
month_last = OrderedDict()
for d, eq in daily_equity:
    month_last[(d.year, d.month)] = eq
prev = EQUITY
print(f"  {'month':<10}{'equity':>14}{'Δ month':>12}{'cum P/L':>14}")
for (y, mo), eq in month_last.items():
    print(f"  {y}-{mo:02d}{'':<3}{eq:>14,.0f}{eq-prev:>+12,.0f}{eq-EQUITY:>+14,.0f}")
    prev = eq

print("\n--- daily trade activity ---")
opens_by_day = defaultdict(int)
closes_by_day = defaultdict(int)
for r in ledger:
    opens_by_day[r["entry"]] += 1
    if r["closed"]:
        closes_by_day[r["asof"]] += 1
n_trading_days = len(daily_equity)
n_entry_days = len(opens_by_day)
total_opens = len(ledger)
total_closes = sum(closes_by_day.values())
print(f"  trading days in window: {n_trading_days}")
print(f"  entry (open) days: {n_entry_days}  |  opens on those days: "
      f"min {min(opens_by_day.values())}, max {max(opens_by_day.values())}, "
      f"avg {total_opens/n_entry_days:.1f}")
print(f"  opens per trading day (all days):   {total_opens/n_trading_days:.2f}")
print(f"  closes per trading day (all days):  {total_closes/n_trading_days:.2f}")
print(f"  executions/day (opens+closes, all): {(total_opens+total_closes)/n_trading_days:.2f}")
print(f"  busiest day: {max((sum(v[d] for v in (opens_by_day, closes_by_day)), d) for d in set(list(opens_by_day)+list(closes_by_day)))[0]} order-events")

print("\n" + "=" * W)
print("FULL TRADE LEDGER")
print("=" * W)
print(f"{'#':>3} {'entry':<11}{'expiry':<11}{'sym':<5}{'kind':<11}{'s/l':<12}"
      f"{'qty':>4}{'net':>7}{'maxloss':>9}{'held':>5}{'P/L':>11}  exit")
print("-" * W)
for i, r in enumerate(sorted(ledger, key=lambda x: (x["entry"], x["underlying"], x["kind"])), 1):
    legs = f"{r['short_k']:.0f}/{r['long_k']:.0f}"
    print(f"{i:>3} {r['entry'].isoformat():<11}{r['expiry'].isoformat():<11}{r['underlying']:<5}"
          f"{r['kind']:<11}{legs:<12}{r['contracts']:>4}{r['entry_net']:>7.2f}"
          f"{r['max_loss']:>9,.0f}{r['days']:>5}{r['pnl']:>+11,.0f}  {r['reason']}")

print("\n" + "=" * W)
print("CAVEATS: options Black-Scholes-modeled (no skew, mid fills, no commissions); dollar magnitudes")
print("approximate, win/loss DIRECTION is the real price path. Call-debit spreads use a 50% profit")
print("target + DTE stops (no 2x-credit price stop — the live runner only price-stops put credits).")
print("=" * W)
