#!/usr/bin/env python3
"""Historical simulation of the premium-selling strategy ("what if we ran it a
month ago?").

IMPORTANT -- what this is and is NOT:
  * It uses REAL historical data: underlying daily closes (SPY/QQQ/IWM) and VIX
    from Yahoo Finance, and each name's own trailing realized volatility.
  * It reuses the LIVE strategy code unchanged: the same chain normalization,
    Black-Scholes 30-delta strike selection, sizing, regime filter, and the four
    guardrails (pp_options/*).
  * It does NOT use real historical option quotes -- those are not available for
    free and PentPort has no historical-chain endpoint. Option prices (and thus
    the credit collected and mark-to-market) are MODELED with Black-Scholes using
    each name's realized vol as the IV input.

Consequences of the model (read before trusting the dollar figures):
  * No volatility skew/smile -- real OTM puts trade richer than flat-vol BS, so
    the real credit collected would typically be HIGHER than shown.
  * IV proxied by trailing realized vol (no vol-risk-premium) -- understates
    entry credit somewhat.
  * Mid-price fills, no bid/ask slippage or commissions.
  * The WIN/LOSS direction is driven by the REAL price path; the dollar magnitude
    is the approximate part.

Usage:
    python backtest.py            # entry = ~31 days ago, mark-to-market today
    python backtest.py 2026-05-11 # explicit entry date
"""

from __future__ import annotations

import datetime as dt
import math
import sys
from dataclasses import dataclass
from typing import Optional

import requests

from pp_options import chain as chainmod
from pp_options import config, guardrails
from pp_options.bsm import bs_price
from pp_options.risk import BookRiskTracker
from pp_options.strategy import build_call_debit_spread, build_put_credit_spread

EQUITY = 100_000.0
TARGET_DTE = 37
# VIX is SPX implied vol. QQQ/IWM trade at structurally higher IV; apply the
# usual rough uplift so each name's modeled IV reflects its own vol surface.
IV_FACTOR = {"SPY": 1.00, "QQQ": 1.12, "IWM": 1.25}
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{s}"
_HDR = {"User-Agent": "Mozilla/5.0 (compatible; pp-options-backtest/1.0)"}


# ---- real historical data --------------------------------------------------
def fetch_series(symbol: str) -> list[tuple[dt.date, float]]:
    r = requests.get(YAHOO.format(s=symbol), params={"range": "2y", "interval": "1d"},
                     headers=_HDR, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    ts, cl = res["timestamp"], res["indicators"]["quote"][0]["close"]
    out = []
    for t, c in zip(ts, cl):
        if c is not None:
            out.append((dt.datetime.fromtimestamp(t, dt.timezone.utc).date(), float(c)))
    return out


def on_or_before(series: list[tuple[dt.date, float]], target: dt.date) -> tuple[dt.date, float]:
    cands = [(d, c) for d, c in series if d <= target]
    return cands[-1] if cands else series[0]


def closes_up_to(series, target: dt.date) -> list[float]:
    return [c for d, c in series if d <= target]


def realized_vol(closes: list[float], n: int = 21) -> Optional[float]:
    """Annualized close-to-close realized volatility over the last n returns."""
    if len(closes) < n + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def sma(closes: list[float], n: int) -> Optional[float]:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def iv_from_vix(underlying: str, vix_level: float) -> float:
    """Model IV from contemporaneous VIX (implied vol), with a per-name uplift."""
    return (vix_level / 100.0) * IV_FACTOR.get(underlying.upper(), 1.0)


# ---- synthetic (BS-priced) chain at a historical date ----------------------
def build_raw_chain(underlying: str, S: float, iv: float, expiry: dt.date, dte: int) -> dict:
    r = config.RISK_FREE_RATE
    q = config.DIVIDEND_YIELD.get(underlying.upper(), config.DEFAULT_DIVIDEND_YIELD)
    T = dte / 365.0
    options = []
    for strike in range(int(S * 0.80), int(S * 1.12) + 1):
        for right in ("C", "P"):
            fair = bs_price(S, strike, r, q, iv, T, right)
            spr = max(0.02, 0.01 * fair)
            options.append({
                "putCall": "CALL" if right == "C" else "PUT",
                "strikePrice": float(strike),
                "bid": max(0.01, round(fair - spr / 2, 2)),
                "ask": round(fair + spr / 2, 2),
                "volatility": round(iv * 100, 2),
                "expirationDate": expiry.isoformat(),
                "daysToExpiration": dte,
                # no "delta" -> exercises the live BS-delta fallback
            })
    return {"symbol": underlying.upper(), "underlyingPrice": S, "options": options}


# ---- outcome valuation -----------------------------------------------------
@dataclass
class Trade:
    underlying: str
    kind: str
    detail: str
    contracts: int
    entry_net: float          # credit (put) or debit (call), per share
    max_loss: float           # total $ defined risk
    short_k: float
    long_k: float
    expiry: dt.date


def value_to_close(tr: Trade, S: float, iv: float, val_date: dt.date) -> float:
    """Current per-share value to CLOSE the spread (puts: buy-back cost;
    calls: sale proceeds). bs_price returns intrinsic when T<=0 (expired)."""
    r = config.RISK_FREE_RATE
    q = config.DIVIDEND_YIELD.get(tr.underlying.upper(), config.DEFAULT_DIVIDEND_YIELD)
    T = max((tr.expiry - val_date).days, 0) / 365.0
    right = "P" if tr.kind == "put_credit" else "C"
    short_p = bs_price(S, tr.short_k, r, q, iv, T, right)
    long_p = bs_price(S, tr.long_k, r, q, iv, T, right)
    if tr.kind == "put_credit":
        return short_p - long_p          # cost to buy the spread back
    return long_p - short_p              # proceeds from selling the call spread


def pnl(tr: Trade, close_val: float) -> float:
    if tr.kind == "put_credit":
        return (tr.entry_net - close_val) * 100 * tr.contracts
    return (close_val - tr.entry_net) * 100 * tr.contracts


# ---- driver ----------------------------------------------------------------
def main() -> int:
    today = dt.date.today()
    entry = (dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1
             else today - dt.timedelta(days=31))
    expiry = entry + dt.timedelta(days=TARGET_DTE)

    print("=" * 78)
    print("HISTORICAL SIMULATION (Black-Scholes; real underlying/VIX, MODELED options)")
    print(f"Entry date: {entry}   target {TARGET_DTE} DTE -> expiry {expiry}   valued: {today}")
    print(f"Equity: ${EQUITY:,.0f}   caps: {config.MAX_TRADE_RISK_PCT:.0%}/trade, "
          f"{config.MAX_BOOK_RISK_PCT:.0%}/book")
    print("=" * 78)

    series = {s: fetch_series(s) for s in ["SPY", "QQQ", "IWM"]}
    vix = fetch_series("^VIX")

    # regime at entry (real VIX)
    vd, vlevel = on_or_before(vix, entry)
    vprev = closes_up_to(vix, entry - dt.timedelta(days=1))
    vprev = vprev[-1] if vprev else None
    regime = guardrails.vix_gate(vlevel, vprev)
    print(f"\nEntry VIX {vlevel:.2f} ({vd}) | {' | '.join(regime.reasons)}")

    tracker = BookRiskTracker(equity=EQUITY, open_risk=0.0)
    trades: list[Trade] = []

    for u in config.UNIVERSE:
        s_entry_d, S0 = on_or_before(series[u], entry)
        hist0 = closes_up_to(series[u], entry)
        iv0 = iv_from_vix(u, vlevel)
        sma200_0 = sma(hist0, config.TREND_SMA_DAYS)
        raw = build_raw_chain(u, S0, iv0, expiry, TARGET_DTE)
        norm = chainmod.normalize(raw, u, S0)
        print(f"\n[{u}] entry {s_entry_d} S=${S0:.2f}  modeled-IV={iv0:.1%} (VIX-based)  "
              f"200dSMA={'n/a' if sma200_0 is None else f'${sma200_0:.2f}'}")

        # PRIMARY put credit (short premium, regime-gated)
        if regime.short_premium_allowed:
            sp, why = build_put_credit_spread(norm, u, EQUITY, regime.risk_scale)
            _accept(sp, why, "put_credit", u, expiry, tracker, trades)
        else:
            print(f"  put_credit  SKIP (regime): {' | '.join(regime.reasons)}")

        # SECONDARY call debit (trend-gated on 200d SMA)
        ok, why = guardrails.trend_gate(S0, sma200_0)
        if ok:
            cd, why2 = build_call_debit_spread(norm, u, EQUITY)
            _accept(cd, why2, "call_debit", u, expiry, tracker, trades)
        else:
            print(f"  call_debit  SKIP (trend): {why}")

    # ---- value every trade as of today (real prices, realized-vol IV) ----
    print("\n" + "=" * 78)
    print(f"OUTCOME as of {today}  ({(expiry - today).days} days to expiry)")
    print("=" * 78)
    vix_now = vix[-1][1]
    total_pnl = total_risk = total_credit = 0.0
    for tr in trades:
        s_today_d, S_now = series[tr.underlying][-1]
        iv_now = iv_from_vix(tr.underlying, vix_now)
        cv = value_to_close(tr, S_now, iv_now, today)
        p = pnl(tr, cv)
        total_pnl += p
        total_risk += tr.max_loss
        total_credit += (tr.entry_net * 100 * tr.contracts) if tr.kind == "put_credit" else 0.0
        status = "EXPIRED" if today >= tr.expiry else "OPEN"
        print(f"[{tr.underlying}] {tr.kind:11} {tr.detail}")
        print(f"     S now ${S_now:.2f} | entry net {tr.entry_net:+.2f} | close val {cv:.2f} "
              f"| {status} | max risk ${tr.max_loss:,.0f} | P/L ${p:+,.0f}")

    print("-" * 78)
    print(f"Trades: {len(trades)} | credit collected ${total_credit:,.0f} | "
          f"capital at risk ${total_risk:,.0f} ({total_risk/EQUITY:.1%} of equity)")
    print(f"TOTAL P/L (mark-to-market): ${total_pnl:+,.0f}  "
          f"({total_pnl/EQUITY:+.2%} of equity, {total_pnl/total_risk:+.1%} of risk)" if total_risk
          else f"TOTAL P/L: ${total_pnl:+,.0f}")
    print("=" * 78)
    print("NOTE: option prices are Black-Scholes-modeled (flat realized vol, no skew, "
          "mid fills,\nno commissions). Real credits would typically be richer; treat "
          "magnitudes as approximate.\nWin/loss direction reflects the REAL price path.")
    return 0


def _accept(spread, why, kind, u, expiry, tracker, trades) -> None:
    if spread is None:
        print(f"  {kind:11} SKIP: {' | '.join(why)}")
        return
    rep = guardrails.evaluate_order(spread, EQUITY, tracker)
    if rep.blocked:
        print(f"  {kind:11} BLOCKED: {' | '.join(rep.block_reasons)}")
        return
    payload = spread.to_payload()
    tracker.add(payload)
    entry_net = spread.net_credit if kind == "put_credit" else spread.net_debit
    trades.append(Trade(
        underlying=u, kind=kind, detail="; ".join(spread.notes), contracts=spread.contracts,
        entry_net=entry_net, max_loss=spread.total_max_loss,
        short_k=spread.short_leg.option.strike, long_k=spread.long_leg.option.strike,
        expiry=expiry,
    ))
    print(f"  {kind:11} OPEN: {'; '.join(spread.notes)} | max risk ${spread.total_max_loss:,.0f}")


if __name__ == "__main__":
    sys.exit(main())
