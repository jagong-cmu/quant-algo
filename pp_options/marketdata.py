"""External market data: trailing daily closes for the 200-day SMA and VIX.

PentPort has no equity price-history endpoint, so the trend filter (underlying
vs its 200-day SMA) and a VIX series come from a free, no-API-key source:
Yahoo Finance's chart JSON endpoint. It covers the ETF universe (SPY/QQQ/IWM)
and the VIX index (^VIX) through one interface, using only `requests`.

(The original plan named stooq, but stooq has since put its CSV behind a
JavaScript bot-wall; Yahoo's public chart endpoint is the equivalent free,
keyless substitute. Swap _fetch() if you prefer another provider -- the
DailySeries contract is all the rest of the system depends on.)

If the feed is unreachable, callers MUST fail closed: with no trend/VIX data we
do not place new entries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; pp-options/1.0)"}


@dataclass
class DailySeries:
    symbol: str
    closes: list[float]   # chronological, oldest -> newest

    @property
    def last(self) -> float:
        return self.closes[-1]

    @property
    def prev(self) -> Optional[float]:
        return self.closes[-2] if len(self.closes) >= 2 else None

    def sma(self, n: int) -> Optional[float]:
        if len(self.closes) < n:
            return None
        return sum(self.closes[-n:]) / n


def _fetch(symbol: str, timeout: float = 15.0) -> DailySeries:
    """symbol examples: 'SPY', 'QQQ', '^VIX'."""
    resp = requests.get(
        YAHOO_CHART_URL.format(symbol=symbol),
        params={"range": "2y", "interval": "1d"},
        headers=_HEADERS,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        result = data["chart"]["result"][0]
        raw = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"unexpected chart response for {symbol!r}: {e}")
    closes = [float(c) for c in raw if c is not None]
    if not closes:
        raise RuntimeError(f"no closes parsed for {symbol!r}")
    return DailySeries(symbol=symbol, closes=closes)


def equity_series(underlying: str) -> DailySeries:
    return _fetch(underlying.upper())


def vix_series() -> DailySeries:
    return _fetch("^VIX")
