"""Backtest simulation engine shared by backtest.py and the walk-forward harness.

Real historical underlying + VIX data (Yahoo); option prices are Black-Scholes-
MODELED (IV from contemporaneous VIX, no skew, mid fills, no commissions). The
win/loss DIRECTION is driven by the real price path; dollar magnitudes are
approximate. See backtest.py header for the full list of modeling caveats.

The engine is sizing-policy agnostic: it takes a Sizer (static or adaptive) and
runs non-overlapping ~DTE cycles, marking every open position to model each
trading day so the harness gets a well-defined daily equity curve.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

import requests

from . import chain as chainmod
from . import config, guardrails
from .bsm import bs_price
from .regime_model import BASE_DELTA, RegimeFeatures
from .risk import BookRiskTracker
from .strategy import build_call_debit_spread, build_put_credit_spread

EQUITY = 100_000.0
TARGET_DTE = 37
IV_FACTOR = {"SPY": 1.00, "QQQ": 1.12, "IWM": 1.25}
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{s}"
_HDR = {"User-Agent": "Mozilla/5.0 (compatible; pp-options-sim/1.0)"}


# ---- data ------------------------------------------------------------------
def _fetch(symbol: str, rng: str) -> list[tuple[dt.date, float]]:
    r = requests.get(YAHOO.format(s=symbol), params={"range": rng, "interval": "1d"},
                     headers=_HDR, timeout=25)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    ts, cl = res["timestamp"], res["indicators"]["quote"][0]["close"]
    return [(dt.datetime.fromtimestamp(t, dt.timezone.utc).date(), float(c))
            for t, c in zip(ts, cl) if c is not None]


class MarketData:
    """Aligned multi-symbol daily closes with a SPY-based trading calendar."""

    SYMBOLS = ["SPY", "QQQ", "IWM", "^VIX", "^VIX3M"]

    def __init__(self, rng: str = "5y"):
        self.series: dict[str, list[tuple[dt.date, float]]] = {
            s: _fetch(s, rng) for s in self.SYMBOLS
        }
        self.maps: dict[str, dict[dt.date, float]] = {
            s: {d: c for d, c in self.series[s]} for s in self.SYMBOLS
        }
        self.calendar: list[dt.date] = [d for d, _ in self.series["SPY"]]
        self._cal_index = {d: i for i, d in enumerate(self.calendar)}

    @property
    def first_date(self) -> dt.date:
        return self.calendar[0]

    @property
    def last_date(self) -> dt.date:
        return self.calendar[-1]

    def on_or_before(self, sym: str, d: dt.date) -> Optional[tuple[dt.date, float]]:
        ser = self.series[sym]
        cands = [(dd, c) for dd, c in ser if dd <= d]
        return cands[-1] if cands else None

    def close(self, sym: str, d: dt.date) -> Optional[float]:
        hit = self.on_or_before(sym, d)
        return hit[1] if hit else None

    def closes_up_to(self, sym: str, d: dt.date) -> list[float]:
        return [c for dd, c in self.series[sym] if dd <= d]

    def prev_trading_day(self, d: dt.date) -> Optional[dt.date]:
        cands = [dd for dd in self.calendar if dd < d]
        return cands[-1] if cands else None

    def trading_days(self, start: dt.date, end: dt.date) -> list[dt.date]:
        return [d for d in self.calendar if start <= d <= end]


def realized_vol(closes: list[float], n: int) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def sma(closes: list[float], n: int) -> Optional[float]:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def iv_from_vix(underlying: str, vix_level: float) -> float:
    return (vix_level / 100.0) * IV_FACTOR.get(underlying.upper(), 1.0)


def features_at(md: MarketData, d: dt.date) -> Optional[RegimeFeatures]:
    """Market-regime features using ONLY data with date <= d (no lookahead)."""
    vix = md.close("^VIX", d)
    if vix is None:
        return None
    spy_closes = md.closes_up_to("SPY", d)
    return RegimeFeatures(
        vix=vix,
        vix3m=md.close("^VIX3M", d),
        rv10=realized_vol(spy_closes, 10),
        rv21=realized_vol(spy_closes, 21),
    )


# ---- synthetic BS chain at a historical date -------------------------------
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
            })
    return {"symbol": underlying.upper(), "underlyingPrice": S, "options": options}


# ---- positions -------------------------------------------------------------
@dataclass
class Position:
    underlying: str
    kind: str                 # put_credit | call_debit
    short_k: float
    long_k: float
    contracts: int
    entry_net: float          # credit (put) / debit (call) per share
    max_loss: float
    entry_date: dt.date
    expiry: dt.date

    def _close_value(self, S: float, iv: float, d: dt.date) -> float:
        r = config.RISK_FREE_RATE
        q = config.DIVIDEND_YIELD.get(self.underlying.upper(), config.DEFAULT_DIVIDEND_YIELD)
        T = max((self.expiry - d).days, 0) / 365.0
        right = "P" if self.kind == "put_credit" else "C"
        sp = bs_price(S, self.short_k, r, q, iv, T, right)
        lp = bs_price(S, self.long_k, r, q, iv, T, right)
        return (sp - lp) if self.kind == "put_credit" else (lp - sp)

    def pnl(self, S: float, iv: float, d: dt.date) -> float:
        cv = self._close_value(S, iv, d)
        if self.kind == "put_credit":
            return (self.entry_net - cv) * 100 * self.contracts
        return (cv - self.entry_net) * 100 * self.contracts


@dataclass
class TradeResult:
    underlying: str
    kind: str
    entry_date: dt.date
    expiry: dt.date
    contracts: int
    max_loss: float
    realized_pnl: float
    regime: str
    reduced: bool             # adaptive made this strictly smaller/further-OTM than base
    short_k: float = 0.0
    long_k: float = 0.0
    entry_net: float = 0.0    # credit (put) / debit (call) per share
    multiplier: float = 1.0
    open_at_end: bool = False  # not yet expired as of the last sim day (MTM, not realized)


@dataclass
class EntryRecord:
    entry_date: dt.date
    regime: str
    multiplier: float
    delta: float
    base_multiplier: float
    reduced: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class SimResult:
    daily_equity: list[tuple[dt.date, float]]
    trades: list[TradeResult]
    entries: list[EntryRecord]


# ---- sizers ----------------------------------------------------------------
class Sizer(Protocol):
    name: str
    def decide(self, underlying: str, entry_date: dt.date, base_scale: float,
               feat: Optional[RegimeFeatures]) -> tuple[float, float, str, bool, list[str]]:
        """Return (effective_multiplier, effective_delta, regime, reduced, reasons)."""
        ...


class StaticSizer:
    """Model OFF: base rules only (vix_gate scale + 0.30 delta)."""
    name = "static"

    def decide(self, underlying, entry_date, base_scale, feat):
        return base_scale, BASE_DELTA, "static", False, []


class AdaptiveSizer:
    """Model ON with FIXED thresholds (one fit per walk-forward fold)."""
    name = "adaptive"

    def __init__(self, thresholds):
        from .regime_model import clamp_to_base, decide
        self._thr = thresholds
        self._decide = decide
        self._clamp = clamp_to_base

    def decide(self, underlying, entry_date, base_scale, feat):
        if feat is None:
            return base_scale, BASE_DELTA, "no_features", False, ["no features -> base"]
        d = self._decide(feat, self._thr)
        eff_mult, eff_delta = self._clamp(d, base_scale)
        reduced = (eff_mult < base_scale - 1e-9) or (eff_delta < BASE_DELTA - 1e-9)
        return eff_mult, eff_delta, d.regime, reduced, d.reasons


class RollingAdaptiveSizer:
    """Model ON, REFIT per entry on a trailing window (true 'adaptive throughout',
    no lookahead -- thresholds use only data strictly before each entry date)."""
    name = "adaptive_rolling"

    def __init__(self, md: "MarketData", train_years: int = 3):
        from .regime_model import clamp_to_base, decide
        self.md = md
        self.train_years = train_years
        self._decide = decide
        self._clamp = clamp_to_base
        self._cache: dict = {}

    def _fit(self, entry_date: dt.date):
        if entry_date in self._cache:
            return self._cache[entry_date]
        try:
            start = entry_date.replace(year=entry_date.year - self.train_years)
        except ValueError:
            start = entry_date.replace(year=entry_date.year - self.train_years, day=28)
        thr = fit_thresholds_window(self.md, start, entry_date)
        self._cache[entry_date] = thr
        return thr

    def decide(self, underlying, entry_date, base_scale, feat):
        if feat is None:
            return base_scale, BASE_DELTA, "no_features", False, ["no features -> base"]
        thr = self._fit(entry_date)
        d = self._decide(feat, thr)
        eff_mult, eff_delta = self._clamp(d, base_scale)
        reduced = (eff_mult < base_scale - 1e-9) or (eff_delta < BASE_DELTA - 1e-9)
        return eff_mult, eff_delta, d.regime, reduced, d.reasons


def fit_thresholds_window(md: "MarketData", start: dt.date, end: dt.date):
    """Fit regime thresholds on [start, end) using only data in that window."""
    from .regime_model import RegimeThresholds
    vix_levels = [c for d, c in md.series["^VIX"] if start <= d < end]
    spy = md.series["SPY"]
    vals = [c for _, c in spy]
    rv_levels = []
    for i in range(21, len(spy)):
        d = spy[i][0]
        if start <= d < end:
            rv = realized_vol(vals[i - 21:i + 1], 21)
            if rv is not None:
                rv_levels.append(rv)
    last = max((d for d, _ in spy if d < end), default=end)
    return RegimeThresholds.fit(vix_levels, rv_levels, fit_date=last)


# ---- cycle / fold simulation ----------------------------------------------
def _open_cycle(md: MarketData, entry: dt.date, expiry: dt.date, dte: int,
                sizer: Sizer) -> tuple[list[Position], EntryRecord]:
    vix_e = md.close("^VIX", entry)
    vix_prev = md.close("^VIX", md.prev_trading_day(entry)) if md.prev_trading_day(entry) else None
    base = guardrails.vix_gate(vix_e, vix_prev)
    base_scale = base.risk_scale if base.short_premium_allowed else 0.0
    feat = features_at(md, entry)

    eff_mult, eff_delta, regime, reduced, reasons = sizer.decide("MKT", entry, base_scale, feat)
    rec = EntryRecord(entry_date=entry, regime=regime, multiplier=eff_mult, delta=eff_delta,
                      base_multiplier=base_scale, reduced=reduced, reasons=reasons)

    positions: list[Position] = []
    if eff_mult <= 0.0:
        return positions, rec

    for u in config.UNIVERSE:
        hit = md.on_or_before(u, entry)
        if hit is None:
            continue
        _, S0 = hit
        iv0 = iv_from_vix(u, vix_e)
        norm = chainmod.normalize(build_raw_chain(u, S0, iv0, expiry, dte), u, S0)
        tracker = BookRiskTracker(equity=EQUITY, open_risk=0.0)

        sp, _ = build_put_credit_spread(norm, u, EQUITY, eff_mult, target_short_delta=eff_delta)
        _commit(sp, "put_credit", u, entry, expiry, regime, reduced, tracker, positions)

        sma0 = sma(md.closes_up_to(u, entry), config.TREND_SMA_DAYS)
        if sma0 is not None and S0 > sma0:
            cd, _ = build_call_debit_spread(norm, u, EQUITY, risk_scale=eff_mult)
            _commit(cd, "call_debit", u, entry, expiry, regime, reduced, tracker, positions)
    return positions, rec


def _commit(spread, kind, u, entry, expiry, regime, reduced, tracker, positions) -> None:
    if spread is None:
        return
    if guardrails.evaluate_order(spread, EQUITY, tracker).blocked:   # hard caps override
        return
    tracker.add(spread.to_payload())
    entry_net = spread.net_credit if kind == "put_credit" else spread.net_debit
    positions.append(Position(
        underlying=u, kind=kind, short_k=spread.short_leg.option.strike,
        long_k=spread.long_leg.option.strike, contracts=spread.contracts,
        entry_net=entry_net, max_loss=spread.total_max_loss, entry_date=entry, expiry=expiry,
    ))


def simulate(md: MarketData, test_start: dt.date, test_end: dt.date, sizer: Sizer,
             cadence_days: int = TARGET_DTE, dte: int = TARGET_DTE,
             include_open: bool = False) -> SimResult:
    """Run non-overlapping cycles in [test_start, test_end]; mark daily to model.

    Entries are scheduled every `cadence_days` trading days. By default only
    cycles whose full DTE outcome is observable (entry+dte <= last_date) are taken
    so every trade is realized. With include_open=True a final not-yet-expired
    cycle is also opened and marked-to-model at the last sim day (used by the
    year-in-review so the most recent month is covered). Equity resets to EQUITY
    at test_start.
    """
    cal = md.trading_days(test_start, test_end)
    if not cal:
        return SimResult([], [], [])

    # schedule entry indices on cadence
    entry_dates: list[dt.date] = []
    i = 0
    while i < len(cal):
        e = cal[i]
        if include_open or (e + dt.timedelta(days=dte)) <= md.last_date:
            entry_dates.append(e)
        i += cadence_days
    entry_set = set(entry_dates)

    # simulate day by day until the last opened cycle expires
    realized = 0.0
    open_pos: list[Position] = []
    closed: list[TradeResult] = []
    entries: list[EntryRecord] = []
    daily: list[tuple[dt.date, float]] = []

    last_expiry = max((e + dt.timedelta(days=dte) for e in entry_dates), default=test_end)
    sim_days = [d for d in md.calendar if test_start <= d <= min(last_expiry, md.last_date)]

    for d in sim_days:
        if d in entry_set:
            pos, rec = _open_cycle(md, d, d + dt.timedelta(days=dte), dte, sizer)
            open_pos.extend(pos)
            entries.append(rec)

        still_open: list[Position] = []
        for p in open_pos:
            if p.expiry <= d:
                S = md.close(p.underlying, d) or md.close(p.underlying, p.expiry)
                ivx = iv_from_vix(p.underlying, md.close("^VIX", d) or 18.0)
                pl = p.pnl(S, ivx, p.expiry)
                realized += pl
                closed.append(TradeResult(
                    underlying=p.underlying, kind=p.kind, entry_date=p.entry_date,
                    expiry=p.expiry, contracts=p.contracts, max_loss=p.max_loss,
                    realized_pnl=pl, regime="", reduced=False,
                    short_k=p.short_k, long_k=p.long_k, entry_net=p.entry_net))
            else:
                still_open.append(p)
        open_pos = still_open

        mtm = 0.0
        vix_d = md.close("^VIX", d) or 18.0
        for p in open_pos:
            S = md.close(p.underlying, d)
            if S is not None:
                mtm += p.pnl(S, iv_from_vix(p.underlying, vix_d), d)
        daily.append((d, EQUITY + realized + mtm))

    # mark any still-open positions to model as of the last sim day (year-end MTM)
    if open_pos and daily:
        last_day = daily[-1][0]
        vix_d = md.close("^VIX", last_day) or 18.0
        for p in open_pos:
            S = md.close(p.underlying, last_day)
            if S is None:
                continue
            closed.append(TradeResult(
                underlying=p.underlying, kind=p.kind, entry_date=p.entry_date,
                expiry=p.expiry, contracts=p.contracts, max_loss=p.max_loss,
                realized_pnl=p.pnl(S, iv_from_vix(p.underlying, vix_d), last_day),
                regime="", reduced=False, short_k=p.short_k, long_k=p.long_k,
                entry_net=p.entry_net, open_at_end=True))

    # attach regime/reduced/multiplier to trades from their entry record
    by_entry = {r.entry_date: r for r in entries}
    for t in closed:
        r = by_entry.get(t.entry_date)
        if r:
            t.regime, t.reduced, t.multiplier = r.regime, r.reduced, r.multiplier
    return SimResult(daily_equity=daily, trades=closed, entries=entries)
