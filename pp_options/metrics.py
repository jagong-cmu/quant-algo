"""Performance / risk metrics for a simulated fold.

Tail-first by design: for short-premium strategies Sharpe/Sortino look great
right up until the tail event, so they are computed but flagged as secondary.
The headline metrics are drawdown and the loss tail (95%/99%, worst trade,
worst day).
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Optional

from .regime_model import percentile
from .simulate import EQUITY, SimResult, TradeResult

TRADING_DAYS = 252


def daily_returns(curve: list[tuple[dt.date, float]]) -> list[float]:
    return [curve[i][1] / curve[i - 1][1] - 1.0
            for i in range(1, len(curve)) if curve[i - 1][1] > 0]


def cagr(curve: list[tuple[dt.date, float]]) -> Optional[float]:
    if len(curve) < 2 or curve[0][1] <= 0:
        return None
    years = max((curve[-1][0] - curve[0][0]).days, 1) / 365.25
    return (curve[-1][1] / curve[0][1]) ** (1.0 / years) - 1.0


def max_drawdown(curve: list[tuple[dt.date, float]]) -> float:
    """Largest peak-to-trough decline (fraction)."""
    peak = -math.inf
    mdd = 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, (peak - eq) / peak)
    return mdd


def sharpe(rets: list[float]) -> Optional[float]:
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    return (m / sd) * math.sqrt(TRADING_DAYS) if sd > 0 else None


def sortino(rets: list[float]) -> Optional[float]:
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    downside = [min(r, 0.0) ** 2 for r in rets]
    dd = math.sqrt(sum(downside) / len(rets))
    return (m / dd) * math.sqrt(TRADING_DAYS) if dd > 0 else None


def worst_day(curve: list[tuple[dt.date, float]]) -> tuple[float, float]:
    """(worst $ daily change, worst % daily change)."""
    worst_d = 0.0
    worst_p = 0.0
    for i in range(1, len(curve)):
        d = curve[i][1] - curve[i - 1][1]
        worst_d = min(worst_d, d)
        if curve[i - 1][1] > 0:
            worst_p = min(worst_p, d / curve[i - 1][1])
    return worst_d, worst_p


@dataclass
class Metrics:
    n_trades: int
    total_pnl: float
    ret_pct: float                  # total return on EQUITY
    cagr: Optional[float]
    max_dd: float
    worst_trade: float
    worst_day_dollar: float
    worst_day_pct: float
    var95_loss: float               # 5th-percentile trade P/L (a loss)
    var99_loss: float               # 1st-percentile trade P/L
    hit_rate: Optional[float]
    avg_win: Optional[float]
    avg_loss: Optional[float]
    sharpe: Optional[float]         # secondary -- see module docstring
    sortino: Optional[float]        # secondary


def compute(sr: SimResult) -> Metrics:
    trades = sr.trades
    pnls = [t.realized_pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    curve = sr.daily_equity
    rets = daily_returns(curve)
    wd_d, wd_p = worst_day(curve) if curve else (0.0, 0.0)
    total = sum(pnls)
    return Metrics(
        n_trades=len(trades),
        total_pnl=total,
        ret_pct=total / EQUITY,
        cagr=cagr(curve) if curve else None,
        max_dd=max_drawdown(curve) if curve else 0.0,
        worst_trade=min(pnls) if pnls else 0.0,
        worst_day_dollar=wd_d,
        worst_day_pct=wd_p,
        var95_loss=percentile(pnls, 5) if pnls else 0.0,
        var99_loss=percentile(pnls, 1) if pnls else 0.0,
        hit_rate=(len(wins) / len(pnls)) if pnls else None,
        avg_win=(sum(wins) / len(wins)) if wins else None,
        avg_loss=(sum(losses) / len(losses)) if losses else None,
        sharpe=sharpe(rets),
        sortino=sortino(rets),
    )


# ---- paired adaptive-vs-static trade differences ---------------------------
def paired_trade_diffs(adaptive: SimResult, static: SimResult) -> list[float]:
    """adaptive_pnl - static_pnl per static trade (0 if adaptive skipped it),
    expressed as a fraction of EQUITY. Same entry dates -> trades align by key."""
    def key(t: TradeResult):
        return (t.entry_date, t.underlying, t.kind)
    a = {key(t): t.realized_pnl for t in adaptive.trades}
    diffs = []
    for t in static.trades:
        ap = a.get(key(t), 0.0)
        diffs.append((ap - t.realized_pnl) / EQUITY)
    return diffs
