"""Live data source: PentPort for account/chain/quotes, Yahoo Finance for trend + VIX."""

from __future__ import annotations

from typing import Optional

from . import chain as chainmod
from . import config, marketdata
from .broker import Broker
from .chain import NormOption
from .logutil import get_logger

VIX_QUOTE_CANDIDATES = ["$VIX", "VIX", "^VIX", "VIX.X", "$VIX.X"]


class LiveDataSource:
    def __init__(self, broker: Optional[Broker] = None):
        self.broker = broker or Broker()
        self.log = get_logger()
        self._equity_series_cache: dict[str, marketdata.DailySeries] = {}
        self._vix_series: Optional[marketdata.DailySeries] = None

    # account state ----------------------------------------------------------
    def equity(self) -> float:
        return self.broker.equity()

    def positions(self) -> dict:
        return self.broker.positions()

    def submit(self, payload: dict) -> dict:
        return self.broker.submit_options_order(payload)

    # market data ------------------------------------------------------------
    def normalized_chain(self, underlying: str) -> tuple[list[NormOption], Optional[float]]:
        raw = self.broker.options_chain(underlying)
        spot = chainmod.underlying_price(raw) or self._quote_price(underlying)
        return chainmod.normalize(raw, underlying, spot), spot

    def spot(self, underlying: str) -> Optional[float]:
        price = self._quote_price(underlying)
        if price is not None:
            return price
        try:
            return self._equity_series(underlying).last
        except Exception:
            return None

    def vix(self) -> tuple[Optional[float], Optional[float]]:
        # Trend/series (level + prior close) from Yahoo; current level optionally
        # refreshed from a live PentPort quote so we honor "pull a VIX quote".
        level = prev = None
        try:
            if self._vix_series is None:
                self._vix_series = marketdata.vix_series()
            level, prev = self._vix_series.last, self._vix_series.prev
            self.log.info("VIX (Yahoo): level=%.2f prev=%s", level, _fmt(prev))
        except Exception as e:
            self.log.warning("Yahoo VIX fetch failed: %s", e)

        live = self._pull_pentport_vix()
        if live is not None:
            self.log.info("VIX (PentPort live quote): %.2f", live)
            level = live  # prefer the more current quote for the level
        if level is None:
            self.log.warning("VIX unavailable from all sources -> short premium will fail closed")
        return level, prev

    def sma(self, underlying: str, days: int) -> Optional[float]:
        try:
            return self._equity_series(underlying).sma(days)
        except Exception as e:
            self.log.warning("[%s] Yahoo SMA fetch failed: %s", underlying, e)
            return None

    # internals --------------------------------------------------------------
    def _equity_series(self, underlying: str) -> marketdata.DailySeries:
        key = underlying.upper()
        if key not in self._equity_series_cache:
            self._equity_series_cache[key] = marketdata.equity_series(key)
        return self._equity_series_cache[key]

    def _pull_pentport_vix(self) -> Optional[float]:
        for sym in VIX_QUOTE_CANDIDATES:
            try:
                resp = self.broker.quotes([sym])
            except Exception:
                continue
            price = _dig_quote_price(resp, sym)
            if price is not None and price > 0:
                return price
        return None

    def _quote_price(self, symbol: str) -> Optional[float]:
        try:
            resp = self.broker.quotes([symbol])
        except Exception as e:
            self.log.warning("[%s] quote fetch failed: %s", symbol, e)
            return None
        return _dig_quote_price(resp, symbol)


def _fmt(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.2f}"


_PRICE_KEYS = ("last", "lastPrice", "mark", "price", "close", "regularMarketLastPrice", "bid", "ask")


def _dig_quote_price(resp: dict, symbol: str) -> Optional[float]:
    """Defensively pull a price for `symbol` from an unknown quotes() shape."""
    if not isinstance(resp, dict):
        return None
    sym = symbol.upper().lstrip("$^")

    def price_from(d: dict) -> Optional[float]:
        low = {str(k).lower(): v for k, v in d.items()}
        for k in _PRICE_KEYS:
            if k.lower() in low:
                try:
                    f = float(low[k.lower()])
                    if f > 0:
                        return f
                except (TypeError, ValueError):
                    continue
        return None

    # shape: {"quotes": {"SPY": {...}}} or {"SPY": {...}} or {"quotes": [ {...} ]}
    containers = [resp]
    if isinstance(resp.get("quotes"), dict):
        containers.append(resp["quotes"])
    if isinstance(resp.get("data"), dict):
        containers.append(resp["data"])
    for c in containers:
        for key, val in c.items():
            if isinstance(val, dict) and str(key).upper().lstrip("$^") == sym:
                p = price_from(val)
                if p is not None:
                    return p
    # shape: list of quote dicts
    for listkey in ("quotes", "data", "results"):
        lst = resp.get(listkey)
        if isinstance(lst, list):
            for q in lst:
                if isinstance(q, dict):
                    qsym = str(q.get("symbol") or q.get("ticker") or "").upper().lstrip("$^")
                    if qsym == sym:
                        p = price_from(q)
                        if p is not None:
                            return p
    # last resort: a top-level price on the response
    return price_from(resp)
