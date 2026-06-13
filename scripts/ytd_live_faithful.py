#!/usr/bin/env python3
"""YTD backtest — LIVE-FAITHFUL: enforces the GLOBAL 20% book cap across all
concurrent overlapping cycles (the one thing simulate.py does not do, because it
builds a fresh BookRiskTracker per underlying per cycle).

Mirrors the live engine path: SPY/QQQ/IWM, 30-delta short puts + call-debit when
above 200d SMA, StaticSizer (VIX-gate scaling == engine.py), 3%/trade + 20%/book
caps. $1M equity, weekly entries from 2026-01-01.

Emits a full trade ledger + analysis. Still HOLDS to expiry (no early 50%/2x/21d
management) and options are Black-Scholes-modeled — both stated in the output.
"""
import datetime as dt
from collections import defaultdict

from pp_options import chain as chainmod
from pp_options import config, guardrails
from pp_options import simulate as S
from pp_options import metrics as M
from pp_options.risk import BookRiskTracker
from pp_options.strategy import build_put_credit_spread, build_call_debit_spread

S.EQUITY = 100_000.0
EQUITY = S.EQUITY
START = dt.date(2026, 1, 1)
CADENCE = 5
DTE = 37


def rec(pos, pl, asof, closed):
    return dict(underlying=pos.underlying, kind=pos.kind, entry=pos.entry_date,
                expiry=pos.expiry, short_k=pos.short_k, long_k=pos.long_k,
                contracts=pos.contracts, entry_net=pos.entry_net, max_loss=pos.max_loss,
                pnl=pl, asof=asof, closed=closed,
                days=(min(pos.expiry, asof) - pos.entry_date).days)


md = S.MarketData(rng="2y")
END = md.last_date
cal = md.trading_days(START, END)

tracker = BookRiskTracker(equity=EQUITY, open_risk=0.0)  # ONE persistent tracker
entry_dates = [cal[i] for i in range(0, len(cal), CADENCE)]
entry_set = set(entry_dates)

open_pos = []      # [Position, release_amount]
ledger = []
skips = []         # (date, underlying, kind, reason)
realized = 0.0
daily_equity = []
peak_concurrent = 0
peak_book = 0.0


def try_commit(spread, kind, u, entry, expiry):
    if spread is None:
        skips.append((entry, u, kind, "no viable spread"))
        return
    rep = guardrails.evaluate_order(spread, EQUITY, tracker)
    if rep.blocked:
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
                norm = chainmod.normalize(S.build_raw_chain(u, S0, iv0, expiry, DTE), u, S0)
                sp, _ = build_put_credit_spread(norm, u, EQUITY, base_scale,
                                                target_short_delta=S.BASE_DELTA)
                try_commit(sp, "put_credit", u, d, expiry)
                sma0 = S.sma(md.closes_up_to(u, d), config.TREND_SMA_DAYS)
                if sma0 is not None and S0 > sma0:
                    cd, _ = build_call_debit_spread(norm, u, EQUITY, risk_scale=base_scale)
                    try_commit(cd, "call_debit", u, d, expiry)

    still = []
    for pos, rel in open_pos:
        if pos.expiry <= d:
            Sx = md.close(pos.underlying, d) or md.close(pos.underlying, pos.expiry)
            ivx = S.iv_from_vix(pos.underlying, md.close("^VIX", d) or 18.0)
            pl = pos.pnl(Sx, ivx, pos.expiry)
            realized += pl
            tracker.open_risk -= rel
            ledger.append(rec(pos, pl, d, closed=True))
        else:
            still.append([pos, rel])
    open_pos = still

    peak_concurrent = max(peak_concurrent, len(open_pos))
    peak_book = max(peak_book, tracker.open_risk)
    mtm = 0.0
    vix_d = md.close("^VIX", d) or 18.0
    for pos, _ in open_pos:
        Sx = md.close(pos.underlying, d)
        if Sx is not None:
            mtm += pos.pnl(Sx, S.iv_from_vix(pos.underlying, vix_d), d)
    daily_equity.append((d, EQUITY + realized + mtm))

if daily_equity:
    last_day = daily_equity[-1][0]
    vix_d = md.close("^VIX", last_day) or 18.0
    for pos, _ in open_pos:
        Sx = md.close(pos.underlying, last_day)
        if Sx is None:
            continue
        ledger.append(rec(pos, pos.pnl(Sx, S.iv_from_vix(pos.underlying, vix_d), last_day),
                          last_day, closed=False))

# ---- metrics via library (build a SimResult) ----
trades = [S.TradeResult(underlying=r["underlying"], kind=r["kind"], entry_date=r["entry"],
                        expiry=r["expiry"], contracts=r["contracts"], max_loss=r["max_loss"],
                        realized_pnl=r["pnl"], regime="", reduced=False, short_k=r["short_k"],
                        long_k=r["long_k"], entry_net=r["entry_net"],
                        open_at_end=not r["closed"]) for r in ledger]
m = M.compute(S.SimResult(daily_equity=daily_equity, trades=trades, entries=[]))

# ================= OUTPUT =================
W = 100
print("=" * W)
print("YTD BACKTEST — LIVE-FAITHFUL (global 20% book cap ENFORCED across concurrent cycles)")
print(f"Equity ${EQUITY:,.0f} | SPY/QQQ/IWM | weekly entries (cadence {CADENCE}d) | dte {DTE}d | "
      f"StaticSizer (= engine.py)")
print(f"Window {START} -> {END} ({len(cal)} trading days)")
print("=" * W)

total_pl = sum(r["pnl"] for r in ledger)
n_put = sum(1 for r in ledger if r["kind"] == "put_credit")
n_call = sum(1 for r in ledger if r["kind"] == "call_debit")
n_closed = sum(1 for r in ledger if r["closed"])
n_open = sum(1 for r in ledger if not r["closed"])
n_skip = len(skips)
wins = [r for r in ledger if r["pnl"] > 0]
losses = [r for r in ledger if r["pnl"] <= 0]
gross_win = sum(r["pnl"] for r in wins)
gross_loss = -sum(r["pnl"] for r in losses)

print(f"\nSpreads OPENED: {len(ledger)}  (put_credit {n_put}, call_debit {n_call}) | "
      f"closed {n_closed}, open@end {n_open}")
print(f"Spreads SKIPPED by guardrails: {n_skip}  "
      f"(book-cap {sum(1 for s in skips if 'book' in s[3])}, "
      f"other {sum(1 for s in skips if 'book' not in s[3])})")
print(f"Peak concurrent positions: {peak_concurrent} | peak book risk: "
      f"${peak_book:,.0f} ({peak_book/EQUITY:.1%} of equity, cap 20%)")

print(f"\nTOTAL P/L (realized + MTM): ${total_pl:+,.0f}  ({total_pl/EQUITY:+.2%} of equity)")
print(f"Profit factor: {(gross_win/gross_loss if gross_loss else float('inf')):.2f}  "
      f"(gross win ${gross_win:,.0f} / gross loss ${gross_loss:,.0f})")

print("\n--- performance metrics ---")
def line(lbl, v, money=False, pct=False):
    s = "n/a" if v is None else (f"{v:+.1%}" if pct else (f"${v:,.0f}" if money else f"{v:.2f}"))
    print(f"  {lbl:<22}{s:>16}")
line("return", m.ret_pct, pct=True)
line("CAGR (annualized)", m.cagr, pct=True)
line("max drawdown", m.max_dd, pct=True)
line("hit rate", m.hit_rate, pct=True)
line("avg win", m.avg_win, money=True)
line("avg loss", m.avg_loss, money=True)
line("worst trade", m.worst_trade, money=True)
line("worst day ($)", m.worst_day_dollar, money=True)
line("95% loss (VaR)", m.var95_loss, money=True)
line("99% loss (VaR)", m.var99_loss, money=True)
line("Sharpe*", m.sharpe)
line("Sortino*", m.sortino)

# breakdowns
def breakdown(title, keyfn):
    print(f"\n--- by {title} ---")
    groups = defaultdict(list)
    for r in ledger:
        groups[keyfn(r)].append(r)
    print(f"  {'group':<14}{'n':>4}{'wins':>6}{'hit%':>7}{'P/L':>14}{'avg/trade':>12}")
    for g in sorted(groups):
        rs = groups[g]
        w = sum(1 for r in rs if r["pnl"] > 0)
        pl = sum(r["pnl"] for r in rs)
        print(f"  {str(g):<14}{len(rs):>4}{w:>6}{100*w/len(rs):>6.0f}%"
              f"{pl:>+14,.0f}{pl/len(rs):>+12,.0f}")

breakdown("structure", lambda r: r["kind"])
breakdown("underlying", lambda r: r["underlying"])
breakdown("entry month", lambda r: f"{r['entry'].year}-{r['entry'].month:02d}")

# monthly equity trajectory
print("\n--- monthly equity trajectory (modeled) ---")
from collections import OrderedDict
month_last = OrderedDict()
for d, eq in daily_equity:
    month_last[(d.year, d.month)] = eq
prev = EQUITY
print(f"  {'month':<10}{'equity':>16}{'Δ month':>14}{'cum P/L':>16}")
for (y, mo), eq in month_last.items():
    print(f"  {y}-{mo:02d}{'':<3}{eq:>16,.0f}{eq-prev:>+14,.0f}{eq-EQUITY:>+16,.0f}")
    prev = eq

# full trade ledger
print("\n" + "=" * W)
print("FULL TRADE LEDGER")
print("=" * W)
hdr = f"{'#':>3} {'entry':<11}{'expiry':<11}{'sym':<5}{'kind':<11}{'short/long':<14}" \
      f"{'qty':>4}{'net':>8}{'maxloss':>10}{'days':>5}{'P/L':>12}  result"
print(hdr)
print("-" * W)
for i, r in enumerate(sorted(ledger, key=lambda x: (x["entry"], x["underlying"], x["kind"])), 1):
    legs = f"{r['short_k']:.0f}/{r['long_k']:.0f}"
    res = "OPEN@END" if not r["closed"] else ("WIN " if r["pnl"] > 0 else "LOSS")
    print(f"{i:>3} {r['entry'].isoformat():<11}{r['expiry'].isoformat():<11}{r['underlying']:<5}"
          f"{r['kind']:<11}{legs:<14}{r['contracts']:>4}{r['entry_net']:>8.2f}"
          f"{r['max_loss']:>10,.0f}{r['days']:>5}{r['pnl']:>+12,.0f}  {res}")

# skip log (cap activity = the whole point of option A)
print("\n" + "=" * W)
print(f"GUARDRAIL SKIP LOG ({len(skips)} skips — these are trades the LIVE bot would NOT take)")
print("=" * W)
skip_by_reason = defaultdict(int)
for s in skips:
    skip_by_reason[s[3]] += 1
for reason, n in sorted(skip_by_reason.items(), key=lambda x: -x[1]):
    print(f"  {n:>4}  {reason}")

print("\n" + "=" * W)
print("CAVEATS: sim HOLDS to expiry; live runner manages early (50% PT / 2x stop / 21-DTE),")
print("so realized P/L and drawdown would differ. Options Black-Scholes-modeled (no skew, mid")
print("fills, no commissions); dollar magnitudes approximate, win/loss DIRECTION is the real path.")
print("=" * W)
