"""Synthetic data source for an offline dry-run (no API key, no network).

Builds an internally-consistent option chain (prices from Black-Scholes; IV
quoted in percent; NO delta field) so the full pipeline -- normalization,
implied-vol solve, BS-delta strike selection, guardrails, and order-payload
logging -- runs end to end and produces real logged would-be orders.

Scenario is deliberately varied to show several guardrail behaviors at once:
  * VIX benign       -> short premium allowed at full size
  * SPY, QQQ > SMA   -> risk-on, call debit spreads considered
  * IWM < SMA        -> trend gate skips its call debit spread
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from . import bsm, chain as chainmod, config
from .chain import NormOption

# spot, annualized IV, 200d SMA (above => risk-on)
_SCENARIO = {
    "SPY": {"spot": 540.0, "iv": 0.16, "sma200": 525.0},
    "QQQ": {"spot": 470.0, "iv": 0.20, "sma200": 455.0},
    "IWM": {"spot": 205.0, "iv": 0.22, "sma200": 212.0},  # below SMA -> no calls
}
_DTE = 37
_EQUITY = 100_000.0
_VIX_LEVEL = 16.0
_VIX_PREV = 15.6


class MockDataSource:
    def __init__(self):
        self._expiry = date.today() + timedelta(days=_DTE)

    def equity(self) -> float:
        return _EQUITY

    def positions(self) -> dict:
        return {"positions": []}

    def submit(self, payload: dict) -> dict:
        # Mirrors Broker.submit_options_order's dry-run behavior; LIVE has no
        # meaning offline, so this never places anything.
        from .logutil import get_logger, log_payload
        log_payload(get_logger(), "[MOCK DRY-RUN] WOULD SUBMIT trade_options payload:", payload)
        return {"ok": True, "dry_run": True, "simulated": True}

    def vix(self) -> tuple[Optional[float], Optional[float]]:
        return _VIX_LEVEL, _VIX_PREV

    def sma(self, underlying: str, days: int) -> Optional[float]:
        return _SCENARIO[underlying.upper()]["sma200"]

    def spot(self, underlying: str) -> Optional[float]:
        return _SCENARIO[underlying.upper()]["spot"]

    def normalized_chain(self, underlying: str) -> tuple[list[NormOption], Optional[float]]:
        raw = self._build_raw_chain(underlying)
        spot = _SCENARIO[underlying.upper()]["spot"]
        return chainmod.normalize(raw, underlying, spot), spot

    def _build_raw_chain(self, underlying: str) -> dict:
        sc = _SCENARIO[underlying.upper()]
        S, iv = sc["spot"], sc["iv"]
        r = config.RISK_FREE_RATE
        q = config.DIVIDEND_YIELD.get(underlying.upper(), config.DEFAULT_DIVIDEND_YIELD)
        T = _DTE / 365.0
        options = []
        lo, hi = int(S * 0.80), int(S * 1.12)
        for strike in range(lo, hi + 1):  # $1 strike spacing
            for right in ("C", "P"):
                fair = bsm.bs_price(S, strike, r, q, iv, T, right)
                spread = max(0.02, 0.01 * fair)
                bid = max(0.01, round(fair - spread / 2, 2))
                ask = round(fair + spread / 2, 2)
                options.append({
                    "putCall": "CALL" if right == "C" else "PUT",
                    "strikePrice": float(strike),
                    "bid": bid,
                    "ask": ask,
                    "volatility": round(iv * 100, 2),   # percent, to test normalization
                    "expirationDate": self._expiry.isoformat(),
                    "daysToExpiration": _DTE,
                    # NOTE: intentionally NO "delta" -> forces the BS fallback path
                })
        return {"symbol": underlying.upper(), "underlyingPrice": S, "options": options}
