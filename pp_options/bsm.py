"""Black-Scholes-Merton helpers.

Used only when the options chain does NOT provide a delta field. We then either
implied-vol-solve from the option's mid price and compute delta, or (if the
chain provides an IV) use it directly. If neither delta nor a usable price/IV is
available, the caller must fail closed -- never guess a strike blindly.
"""

from __future__ import annotations

import math
from typing import Optional


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1(S: float, K: float, r: float, q: float, sigma: float, T: float) -> float:
    return (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))


def bs_price(S: float, K: float, r: float, q: float, sigma: float, T: float, right: str) -> float:
    """European option price under BSM with continuous dividend yield q."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        # intrinsic value fallback
        intrinsic = max(0.0, (S - K) if right == "C" else (K - S))
        return intrinsic
    d1 = _d1(S, K, r, q, sigma, T)
    d2 = d1 - sigma * math.sqrt(T)
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    if right == "C":
        return S * disc_q * _norm_cdf(d1) - K * disc_r * _norm_cdf(d2)
    return K * disc_r * _norm_cdf(-d2) - S * disc_q * _norm_cdf(-d1)


def bs_delta(S: float, K: float, r: float, q: float, sigma: float, T: float, right: str) -> float:
    """Signed delta. Calls in (0, 1); puts in (-1, 0)."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        # degenerate: 0/1 step
        if right == "C":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1 = _d1(S, K, r, q, sigma, T)
    disc_q = math.exp(-q * T)
    if right == "C":
        return disc_q * _norm_cdf(d1)
    return -disc_q * _norm_cdf(-d1)


def implied_vol(price: float, S: float, K: float, r: float, q: float, T: float, right: str) -> Optional[float]:
    """Solve for implied volatility by bisection. Returns None if not solvable."""
    if price is None or price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return None
    # price must be within no-arbitrage bounds, else unsolvable
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    if right == "C":
        lo_bound = max(0.0, S * disc_q - K * disc_r)
        hi_bound = S * disc_q
    else:
        lo_bound = max(0.0, K * disc_r - S * disc_q)
        hi_bound = K * disc_r
    if not (lo_bound - 1e-6 <= price <= hi_bound + 1e-6):
        return None

    lo, hi = 1e-4, 5.0
    f_lo = bs_price(S, K, r, q, lo, T, right) - price
    f_hi = bs_price(S, K, r, q, hi, T, right) - price
    if f_lo * f_hi > 0:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        f_mid = bs_price(S, K, r, q, mid, T, right) - price
        if abs(f_mid) < 1e-6:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)
