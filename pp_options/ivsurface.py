"""Calibrated implied-volatility surface for the backtest.

The PentPort competition prices options via Black-Scholes at a per-strike IV
surface (validated 2026-06-12: BS@reportedIV reproduces lastPrice to ~1-4% ATM,
bid/ask is null everywhere -> no spread). The backtest historically used a single
FLAT VIX-derived IV per symbol, which is wrong on two counts:
  1. the ATM level: SPY ATM IV (~15%) sits BELOW VIX (~18%) because VIX embeds skew;
  2. the skew: index puts trade at materially higher IV than ATM/calls.

There is no HISTORICAL surface to pull (the chain is a live snapshot), so we:
  * calibrate the skew SHAPE and the ATM-IV/VIX ratio from today's live surface;
  * anchor the ATM level to each historical date's VIX, keeping the shape fixed.

This makes modeled prices exact at-the-money and faithful in the wings, under the
(reasonable, stated) assumption that the normalized skew shape is stable over the
backtest window. Model: IV(k) = atm * (1 + beta*k + alpha*k^2), k = ln(K/S),
atm = f * VIX/100. Coeffs are per symbol in state/iv_surface.json.
"""
from __future__ import annotations

import json
import math
import os
from typing import Optional

PATH = "state/iv_surface.json"               # dated (~35 DTE) surface for the swing sim
SHORT_PATH = "state/iv_surface_short.json"   # short-dated (~3-6 DTE) surface for intraday
_CACHE: dict = {}


class SurfaceModel:
    def __init__(self, coeffs: dict):
        self.coeffs = coeffs  # sym -> {f, beta, alpha, atm_iv, vix, n, dte}

    def iv(self, sym: str, S: float, K: float, vix: float) -> Optional[float]:
        p = self.coeffs.get(sym.upper())
        if not p or S <= 0 or K <= 0 or vix is None:
            return None
        k = math.log(K / S)
        atm = p["f"] * (vix / 100.0)
        mult = 1.0 + p["beta"] * k + p["alpha"] * k * k
        return max(0.03, min(2.0, atm * mult))

    def to_json(self) -> dict:
        return self.coeffs


def load(path: str = PATH) -> "Optional[SurfaceModel]":
    """Lazy-load the calibrated surface at `path`; cached per path. None if absent."""
    if path not in _CACHE:
        model = None
        if os.path.exists(path):
            try:
                with open(path) as fh:
                    model = SurfaceModel(json.load(fh))
            except (json.JSONDecodeError, OSError):
                model = None
        _CACHE[path] = model
    return _CACHE[path]


def reset_cache() -> None:
    _CACHE.clear()


# ---- calibration -----------------------------------------------------------
def _solve3(A: list[list[float]], b: list[float]) -> list[float]:
    """Solve a 3x3 linear system by Gaussian elimination with partial pivoting."""
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        if abs(M[col][col]) < 1e-12:
            raise ValueError("singular system")
        for r in range(3):
            if r != col:
                f = M[r][col] / M[col][col]
                for c in range(col, 4):
                    M[r][c] -= f * M[col][c]
    return [M[i][3] / M[i][i] for i in range(3)]


def fit_quadratic(points: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Least-squares IV = c + b*k + a*k^2 over (k, iv) points. Returns (c, b, a)."""
    S0 = float(len(points))
    Sk = sum(k for k, _ in points)
    Sk2 = sum(k * k for k, _ in points)
    Sk3 = sum(k ** 3 for k, _ in points)
    Sk4 = sum(k ** 4 for k, _ in points)
    Sy = sum(v for _, v in points)
    Sky = sum(k * v for k, v in points)
    Sk2y = sum(k * k * v for k, v in points)
    A = [[S0, Sk, Sk2], [Sk, Sk2, Sk3], [Sk2, Sk3, Sk4]]
    c, b, a = _solve3(A, [Sy, Sky, Sk2y])
    return c, b, a


def calibrate(symbol_points: dict, vix_now: float, dte: int) -> SurfaceModel:
    """symbol_points: sym -> list of (log_moneyness, iv). Builds the model."""
    coeffs = {}
    for sym, pts in symbol_points.items():
        if len(pts) < 8:
            continue
        c, b, a = fit_quadratic(pts)
        if c <= 0:
            continue
        coeffs[sym.upper()] = {
            "f": c / (vix_now / 100.0),  # ATM-IV / VIX ratio (level anchor)
            "beta": b / c,               # normalized linear skew
            "alpha": a / c,              # normalized curvature (smile)
            "atm_iv": c, "vix": vix_now, "n": len(pts), "dte": dte,
        }
    return SurfaceModel(coeffs)


def save(model: SurfaceModel, path: str = PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(model.to_json(), fh, indent=2)
    reset_cache()
