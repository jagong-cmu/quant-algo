"""Short-DTE intraday MOMENTUM strategy on defined-risk debit spreads.

A separate, AGGRESSIVE strategy for a 14-day competition -- distinct from the
conservative premium-selling system (whose 3%/20% caps are left untouched).

Signal:   intraday EMA(fast) vs EMA(slow) crossover on the underlying.
          cross UP  -> bullish -> CALL debit spread (buy ~ATM call, sell OTM call)
          cross DOWN-> bearish -> PUT  debit spread (buy ~ATM put,  sell OTM put)
Risk:     every position is a DEBIT spread => max loss = net debit (defined, no
          naked exposure). Aggressive per-trade sizing + a hard daily-loss circuit
          breaker that halts new entries for the day.
Data:     underlying bars from Yahoo (free, intraday). Option prices are
          Black-Scholes-MODELED off the underlying + a VIX-based IV (no real chain
          on the free tier) -- direction is real, magnitudes approximate.

LIVE_TRADING stays False; this module backtests and logs would-be orders only.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Optional

import requests

from . import bsm, config

# ---- aggressive intraday config (separate from the conservative caps) ------
INTRADAY_UNIVERSE = ["SPY", "QQQ", "IWM"]   # 0-DTE-capable index ETFs
EMA_FAST = 9
EMA_SLOW = 21
BAR_INTERVAL = "5m"
AGGR_TRADE_RISK_PCT = 0.07        # ~7% of equity max loss per spread (aggressive)
DAILY_LOSS_HALT_PCT = 0.08        # halt new entries after -8% on the day
MAX_CONCURRENT_RISK_PCT = 0.40    # cap total open defined risk
CANDIDATE_WIDTHS = [1.0, 2.0, 3.0]
LONG_LEG_DELTA = 0.50             # buy ~ATM
SHORT_LEG_DELTA = 0.30            # sell OTM to cap cost
STOP_FRAC = 0.50                  # exit if the spread loses 50% of the debit
TARGET_FRAC = 0.80                # exit at 80% of max profit (width - debit)
SHORT_DTE_DAYS = 0                # 0 = same-day expiry (0DTE)
IV_FACTOR = {"SPY": 1.00, "QQQ": 1.12, "IWM": 1.25}

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{s}"
_HDR = {"User-Agent": "Mozilla/5.0 (compatible; pp-intraday/1.0)"}
_MARKET_CLOSE_UTC = dt.time(20, 0)   # 16:00 ET ~ 20:00 UTC (DST-approx; fine for sim)


@dataclass
class Bar:
    ts: dt.datetime          # tz-aware UTC
    open: float
    high: float
    low: float
    close: float


def fetch_bars(symbol: str, interval: str = BAR_INTERVAL, rng: str = "1mo",
               period1: Optional[int] = None, period2: Optional[int] = None) -> list[Bar]:
    params = {"interval": interval, "includePrePost": "false"}
    if period1 and period2:                       # explicit window (e.g. full 60d of 5m)
        params["period1"] = int(period1); params["period2"] = int(period2)
    else:
        params["range"] = rng
    r = requests.get(YAHOO.format(s=symbol), params=params, headers=_HDR, timeout=25)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    ts = res.get("timestamp", []) or []
    q = res["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        bars.append(Bar(dt.datetime.fromtimestamp(t, dt.timezone.utc), o, h, l, c))
    return bars


def ema(values: list[float], n: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def vix_by_date(rng: str = "3mo") -> dict[dt.date, float]:
    r = requests.get(YAHOO.format(s="^VIX"), params={"range": rng, "interval": "1d"},
                     headers=_HDR, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    out = {}
    for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
        if c is not None:
            out[dt.datetime.fromtimestamp(t, dt.timezone.utc).date()] = float(c)
    return out


# ---- option / spread modeling ---------------------------------------------
def _years_to(expiry: dt.datetime, now: dt.datetime) -> float:
    return max((expiry - now).total_seconds(), 60.0) / (365.25 * 86400.0)


def _delta(S, K, T, iv, right, q):
    return bsm.bs_delta(S, K, config.RISK_FREE_RATE, q, iv, T, right)


@dataclass
class Spread:
    symbol: str
    direction: str            # "bull" (call debit) | "bear" (put debit)
    right: str                # "C" | "P"
    long_k: float
    short_k: float
    contracts: int
    debit: float              # per share, > 0
    width: float
    expiry: dt.datetime

    @property
    def max_loss(self) -> float:
        return self.debit * 100 * self.contracts

    @property
    def max_profit(self) -> float:
        return (self.width - self.debit) * 100 * self.contracts

    def value(self, S: float, iv: float, now: dt.datetime) -> float:
        """Per-share value of the debit spread now (long - short)."""
        T = _years_to(self.expiry, now)
        q = config.DIVIDEND_YIELD.get(self.symbol, config.DEFAULT_DIVIDEND_YIELD)
        lp = bsm.bs_price(S, self.long_k, config.RISK_FREE_RATE, q, iv, T, self.right)
        sp = bsm.bs_price(S, self.short_k, config.RISK_FREE_RATE, q, iv, T, self.right)
        return lp - sp

    def pnl(self, S: float, iv: float, now: dt.datetime) -> float:
        return (self.value(S, iv, now) - self.debit) * 100 * self.contracts


def build_spread(symbol: str, direction: str, S: float, iv: float, expiry: dt.datetime,
                 now: dt.datetime, equity: float, risk_pct: float = AGGR_TRADE_RISK_PCT
                 ) -> Optional[Spread]:
    """Build an aggressively-sized, defined-risk debit spread in `direction`.

    Returns None (skip) if no width yields a positive debit that fits >=1 contract
    within the risk budget -- never a naked or undefined-risk position.
    """
    right = "C" if direction == "bull" else "P"
    q = config.DIVIDEND_YIELD.get(symbol, config.DEFAULT_DIVIDEND_YIELD)
    T = _years_to(expiry, now)
    if T <= 0 or iv <= 0:
        return None

    # long leg ~ ATM (nearest integer strike to spot)
    long_k = float(round(S))
    budget = risk_pct * equity
    best: Optional[Spread] = None
    for w in sorted(CANDIDATE_WIDTHS):
        short_k = long_k + w if direction == "bull" else long_k - w
        if short_k <= 0:
            continue
        lp = bsm.bs_price(S, long_k, config.RISK_FREE_RATE, q, iv, T, right)
        sp = bsm.bs_price(S, short_k, config.RISK_FREE_RATE, q, iv, T, right)
        debit = lp - sp
        if debit <= 0 or debit >= w:           # must be a real, defined-risk debit
            continue
        per_contract_loss = debit * 100
        contracts = int(math.floor(budget / per_contract_loss))
        if contracts < 1:
            continue
        best = Spread(symbol=symbol, direction=direction, right=right, long_k=long_k,
                      short_k=short_k, contracts=contracts, debit=round(debit, 2),
                      width=w, expiry=expiry)
        break   # smallest viable width (cheapest, most contracts)
    return best


# ---- backtest --------------------------------------------------------------
@dataclass
class Trade:
    symbol: str
    direction: str
    entry_ts: dt.datetime
    exit_ts: dt.datetime
    contracts: int
    debit: float
    width: float
    pnl: float
    exit_reason: str
    max_loss: float


@dataclass
class DayResult:
    day: dt.date
    start_equity: float
    end_equity: float
    trades: list[Trade] = field(default_factory=list)
    halted: bool = False


@dataclass
class BacktestResult:
    days: list[DayResult]
    equity_curve: list[tuple[dt.datetime, float]]
    trades: list[Trade]


def _group_by_day(bars: list[Bar]) -> dict[dt.date, list[Bar]]:
    out: dict[dt.date, list[Bar]] = {}
    for b in bars:
        out.setdefault(b.ts.date(), []).append(b)
    return out


def _sma_std(closes: list[float], n: int):
    smas = [None] * len(closes); stds = [None] * len(closes)
    for i in range(len(closes)):
        if i + 1 >= n:
            w = closes[i - n + 1:i + 1]
            m = sum(w) / n
            smas[i] = m
            stds[i] = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    return smas, stds


LONG_EMA_N = 50          # higher-timeframe trend proxy (50 x 5m ~ 4h)
MAX_HOLD_BARS = 12       # time-stop: ~1h on 5m bars
COOLDOWN_BARS = 3        # bars to wait after a stop/time exit before re-entering


def _indicators(closes: list[float], signal: str, sma_n: int):
    if signal == "momentum":
        return {"ef": ema(closes, EMA_FAST), "es": ema(closes, EMA_SLOW)}
    smas, stds = _sma_std(closes, sma_n)
    z = [None] * len(closes)
    for i in range(len(closes)):
        if smas[i] is not None and stds[i] and stds[i] > 0:
            z[i] = (closes[i] - smas[i]) / stds[i]
    return {"z": z, "long_ema": ema(closes, LONG_EMA_N), "close": closes}


def _entry_dir(ind: dict, i: int, signal: str, z_entry: float,
               hardened: bool = True) -> Optional[str]:
    if signal == "momentum":
        ef, es = ind["ef"], ind["es"]
        if ef[i] > es[i] and ef[i - 1] <= es[i - 1]:
            return "bull"
        if ef[i] < es[i] and ef[i - 1] >= es[i - 1]:
            return "bear"
        return None

    z, zp = ind["z"][i], ind["z"][i - 1]
    if z is None or zp is None:
        return None

    bull = z <= -z_entry and zp > -z_entry
    bear = z >= z_entry and zp < z_entry

    if hardened:
        # Trend gate by the long-EMA SLOPE (prevailing direction), not price-vs-EMA:
        # a momentary 2-sigma dip breaches the EMA but should not flip the trend read.
        # Don't buy dips in a clear downtrend; don't fade rips in a clear uptrend.
        le, k, eps = ind["long_ema"], 10, 0.0005
        slope = (le[i] - le[i - k]) / le[i - k] if i >= k and le[i - k] > 0 else 0.0
        if bull and slope < -eps:
            bull = False
        if bear and slope > eps:
            bear = False

    return "bull" if bull else ("bear" if bear else None)


def _want_flip_exit(ind: dict, i: int, direction: str, signal: str,
                    z_entry: float, z_exit: float) -> bool:
    if signal == "momentum":
        ef, es = ind["ef"], ind["es"]
        up = ef[i] > es[i] and ef[i - 1] <= es[i - 1]
        dn = ef[i] < es[i] and ef[i - 1] >= es[i - 1]
        return (direction == "bull" and dn) or (direction == "bear" and up)
    z = ind["z"][i]
    if z is None:
        return False
    if abs(z) <= z_exit:                                    # reverted to the mean
        return True
    return (direction == "bull" and z >= z_entry) or (direction == "bear" and z <= -z_entry)


def backtest(equity0: float = 100_000.0, interval: str = BAR_INTERVAL,
             rng: str = "1mo", universe: Optional[list[str]] = None,
             signal: str = "momentum", risk_pct: float = AGGR_TRADE_RISK_PCT,
             sma_n: int = 20, z_entry: float = 2.0, z_exit: float = 0.5,
             hardened: bool = True, daily_halt_pct: float = DAILY_LOSS_HALT_PCT,
             max_hold_bars: int = MAX_HOLD_BARS, cooldown_bars: int = COOLDOWN_BARS,
             series: Optional[dict] = None, vix: Optional[dict] = None
             ) -> BacktestResult:
    universe = universe or INTRADAY_UNIVERSE
    vix = vix if vix is not None else vix_by_date()
    series = series if series is not None else {s: fetch_bars(s, interval, rng) for s in universe}
    by_day = {s: _group_by_day(series[s]) for s in universe}
    all_days = sorted(set().union(*[set(by_day[s]) for s in universe]))

    equity = equity0
    days: list[DayResult] = []
    curve: list[tuple[dt.datetime, float]] = []
    all_trades: list[Trade] = []

    for day in all_days:
        iv_day = vix.get(day) or vix.get(max((d for d in vix if d <= day), default=day)) or 18.0
        day_start_eq = equity
        day_res = DayResult(day=day, start_equity=day_start_eq, end_equity=day_start_eq)
        expiry = dt.datetime.combine(day, _MARKET_CLOSE_UTC, tzinfo=dt.timezone.utc)

        bars = {s: by_day[s].get(day, []) for s in universe}
        ind = {s: _indicators([b.close for b in bars[s]], signal, sma_n) for s in universe}

        timeline = sorted(set(b.ts for s in universe for b in bars[s]))
        idx = {s: {b.ts: i for i, b in enumerate(bars[s])} for s in universe}
        open_pos: dict[str, Spread] = {}
        open_entry: dict[str, dt.datetime] = {}
        open_entry_i: dict[str, int] = {}
        cooldown_until: dict[str, int] = {}
        realized_day = 0.0
        halted = False

        for ts in timeline:
            mtm = 0.0
            for s, sp in open_pos.items():
                if ts in idx[s]:
                    mtm += sp.pnl(bars[s][idx[s][ts]].close, _iv(iv_day, s), ts)
            curve.append((ts, day_start_eq + realized_day + mtm))

            for s in universe:
                if ts not in idx[s]:
                    continue
                i = idx[s][ts]
                if i == 0:
                    continue
                bar = bars[s][i]
                iv_s = _iv(iv_day, s)

                if s in open_pos:
                    sp = open_pos[s]
                    val = sp.value(bar.close, iv_s, ts)
                    reason = None
                    if _want_flip_exit(ind[s], i, sp.direction, signal, z_entry, z_exit):
                        reason = "signal_flip"
                    elif val <= sp.debit * (1 - STOP_FRAC):
                        reason = "stop"
                    elif val >= sp.debit + TARGET_FRAC * (sp.width - sp.debit):
                        reason = "target"
                    elif (i - open_entry_i[s]) >= max_hold_bars:
                        reason = "time"
                    elif ts >= expiry - dt.timedelta(minutes=10):
                        reason = "eod"
                    if reason:
                        pnl = sp.pnl(bar.close, iv_s, ts)
                        realized_day += pnl
                        t = Trade(s, sp.direction, open_entry[s], ts, sp.contracts, sp.debit,
                                  sp.width, pnl, reason, sp.max_loss)
                        day_res.trades.append(t); all_trades.append(t)
                        if reason in ("stop", "time") or pnl < 0:   # cooldown after a bad exit
                            cooldown_until[s] = i + cooldown_bars
                        del open_pos[s]; del open_entry[s]; del open_entry_i[s]

                if realized_day <= -daily_halt_pct * day_start_eq:
                    halted = True
                if halted or s in open_pos:
                    continue
                if ts >= expiry - dt.timedelta(minutes=30):
                    continue
                if i < cooldown_until.get(s, -1):
                    continue
                direction = _entry_dir(ind[s], i, signal, z_entry, hardened)
                if direction is None:
                    continue
                eq_now = day_start_eq + realized_day
                if sum(p.max_loss for p in open_pos.values()) >= MAX_CONCURRENT_RISK_PCT * eq_now:
                    continue
                sp = build_spread(s, direction, bar.close, iv_s, expiry, ts, eq_now, risk_pct)
                if sp is None:
                    continue
                if sp.max_loss > risk_pct * eq_now + 1e-6:    # hard per-trade cap
                    continue
                open_pos[s] = sp
                open_entry[s] = ts
                open_entry_i[s] = i

        equity = day_start_eq + realized_day
        day_res.end_equity = equity
        day_res.halted = halted
        days.append(day_res)

    return BacktestResult(days=days, equity_curve=curve, trades=all_trades)


def _iv(vix_level: float, symbol: str) -> float:
    return (vix_level / 100.0) * IV_FACTOR.get(symbol, 1.0)
