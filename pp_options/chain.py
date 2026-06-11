"""Normalize the options_chain() response into a flat list of contracts and
select strikes by delta.

The PentPort docs never show the chain response body. The order schema matches
the Schwab / TD Ameritrade Trader API, so the chain is *probably* TDA-style
(putExpDateMap / callExpDateMap), but we defensively also handle a flat list and
split call/put lists. Field lookups are by candidate name.

Delta handling:
  * If a contract exposes a delta field, we use it.
  * Otherwise we compute a Black-Scholes delta, taking IV from the chain if
    present, else implying it from the contract mid price.
  * If neither delta nor a usable price/IV exists, the contract has delta=None
    and is skipped by the selector (fail closed -- never guess a strike).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from . import bsm, config
from .occ import build_occ, parse_occ


@dataclass
class NormOption:
    underlying: str
    right: str               # "C" or "P"
    strike: float
    expiry: date
    dte: int
    bid: Optional[float]
    ask: Optional[float]
    mid: Optional[float]
    delta: Optional[float]   # signed; calls (0,1), puts (-1,0)
    iv: Optional[float]
    symbol: str              # OCC

    @property
    def abs_delta(self) -> Optional[float]:
        return abs(self.delta) if self.delta is not None else None


# ---- field extraction helpers ---------------------------------------------
def _first(d: dict, *names, default=None):
    low = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        if n.lower() in low and low[n.lower()] is not None:
            return low[n.lower()]
    return default


def _num(v) -> Optional[float]:
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


def _norm_right(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().upper()
    if s in ("C", "CALL", "CALLS"):
        return "C"
    if s in ("P", "PUT", "PUTS"):
        return "P"
    return None


def _norm_iv(v) -> Optional[float]:
    f = _num(v)
    if f is None or f <= 0:
        return None
    # Some APIs report IV in percent (e.g. 18.5 -> 0.185).
    return f / 100.0 if f > 3.0 else f


def _greek_delta(contract: dict) -> Optional[float]:
    d = _first(contract, "delta")
    if d is None:
        greeks = _first(contract, "greeks")
        if isinstance(greeks, dict):
            d = _first(greeks, "delta")
    return _num(d)


def _today() -> date:
    return date.today()


# ---- response shape handling ----------------------------------------------
def _unwrap(chain: dict) -> dict:
    """PentPort nests the chain under a 'chain' key; unwrap to that dict."""
    if isinstance(chain, dict) and isinstance(chain.get("chain"), dict):
        return chain["chain"]
    return chain


def _iter_contracts(chain: dict):
    """Yield (contract_dict, right_hint) from whatever shape the chain takes.

    right_hint is 'P'/'C' inferred from the put/call map (real PentPort contracts
    don't carry putCall on the contract itself), or None for flat lists where the
    contract should declare its own right.
    """
    chain = _unwrap(chain)

    # 1) Nested maps: {exp: {strike: [contracts]}} (TDA) OR {exp: [contracts]} (PentPort)
    for map_key, hint in (("putExpDateMap", "P"), ("callExpDateMap", "C"),
                          ("put_exp_date_map", "P"), ("call_exp_date_map", "C")):
        m = chain.get(map_key) if isinstance(chain, dict) else None
        if not isinstance(m, dict):
            continue
        for _exp, val in m.items():
            if isinstance(val, dict):
                for _k, contracts in val.items():
                    if isinstance(contracts, list):
                        for c in contracts:
                            if isinstance(c, dict):
                                yield c, hint
                    elif isinstance(contracts, dict):
                        yield contracts, hint
            elif isinstance(val, list):
                for c in val:
                    if isinstance(c, dict):
                        yield c, hint

    # 2) split lists
    for key, hint in (("calls", "C"), ("puts", "P")):
        lst = chain.get(key) if isinstance(chain, dict) else None
        if isinstance(lst, list):
            for c in lst:
                if isinstance(c, dict):
                    yield c, hint

    # 3) flat list under a variety of keys (contract declares its own right)
    for key in ("options", "contracts", "data", "results"):
        lst = chain.get(key) if isinstance(chain, dict) else None
        if isinstance(lst, list):
            for c in lst:
                if isinstance(c, dict):
                    yield c, None


def underlying_price(chain: dict) -> Optional[float]:
    chain = _unwrap(chain)
    for k in ("underlyingPrice", "underlying_price", "underlyingLast", "last", "mark", "spot"):
        v = _num(chain.get(k)) if isinstance(chain, dict) else None
        if v:
            return v
    u = chain.get("underlying") if isinstance(chain, dict) else None
    if isinstance(u, dict):
        return _num(_first(u, "last", "mark", "price", "close"))
    return None


def _contract_expiry_dte(contract: dict) -> tuple[Optional[date], Optional[int]]:
    sym = _first(contract, "symbol", "occ", "contractSymbol", "option_symbol")
    expiry = None
    if sym:
        try:
            expiry = parse_occ(str(sym)).expiry
        except ValueError:
            expiry = None
    if expiry is None:
        raw = _first(contract, "expirationDate", "expiration", "expiry", "expiration_date")
        if raw is not None:
            try:
                expiry = date.fromisoformat(str(raw)[:10])
            except ValueError:
                expiry = None
    dte = _first(contract, "daysToExpiration", "dte", "days_to_expiration")
    dte = int(dte) if dte is not None else (None if expiry is None else (expiry - _today()).days)
    return expiry, dte


def normalize(chain: dict, underlying: str, spot: Optional[float] = None) -> list[NormOption]:
    spot = spot or underlying_price(chain)
    q = config.DIVIDEND_YIELD.get(underlying.upper(), config.DEFAULT_DIVIDEND_YIELD)
    r = config.RISK_FREE_RATE

    out: list[NormOption] = []
    for c, right_hint in _iter_contracts(chain):
        right = _norm_right(_first(c, "putCall", "type", "right", "option_type", "side")) or right_hint
        strike = _num(_first(c, "strikePrice", "strike", "strike_price", "K"))
        if right is None or strike is None:
            continue
        expiry, dte = _contract_expiry_dte(c)
        if expiry is None or dte is None:
            continue

        bid = _num(_first(c, "bid", "bidPrice", "bid_price"))
        ask = _num(_first(c, "ask", "askPrice", "ask_price"))
        mark = _num(_first(c, "mark", "mid", "midPrice"))
        last = _num(_first(c, "last", "lastPrice"))
        if bid is not None and ask is not None and ask >= bid >= 0:
            mid = (bid + ask) / 2.0
        else:
            mid = mark or last

        iv = _norm_iv(_first(c, "volatility", "iv", "impliedVolatility", "implied_volatility"))
        delta = _greek_delta(c)

        # Build/validate OCC symbol.
        raw_sym = _first(c, "symbol", "occ", "contractSymbol", "option_symbol")
        try:
            symbol = build_occ(underlying, expiry, right, strike)
        except ValueError:
            continue
        if raw_sym and str(raw_sym).strip().upper() != symbol:
            # Trust the exchange-provided symbol if it parses cleanly.
            try:
                parse_occ(str(raw_sym))
                symbol = str(raw_sym).strip().upper()
            except ValueError:
                pass

        # Delta fallback via Black-Scholes.
        if delta is None and spot:
            T = max(dte, 0) / 365.0
            use_iv = iv
            if use_iv is None and mid:
                use_iv = bsm.implied_vol(mid, spot, strike, r, q, T, right)
            if use_iv:
                delta = bsm.bs_delta(spot, strike, r, q, use_iv, T, right)
                if iv is None:
                    iv = use_iv

        out.append(NormOption(
            underlying=underlying.upper(), right=right, strike=strike, expiry=expiry,
            dte=dte, bid=bid, ask=ask, mid=mid, delta=delta, iv=iv, symbol=symbol,
        ))
    return out


# ---- selection -------------------------------------------------------------
def in_dte_window(options: list[NormOption]) -> list[NormOption]:
    return [o for o in options if config.DTE_MIN <= o.dte <= config.DTE_MAX]


def choose_expiry(options: list[NormOption]) -> Optional[date]:
    """Pick the expiry inside the DTE window with the most listed strikes."""
    windowed = in_dte_window(options)
    if not windowed:
        return None
    by_exp: dict[date, int] = {}
    for o in windowed:
        by_exp[o.expiry] = by_exp.get(o.expiry, 0) + 1
    # Prefer the expiry closest to the middle of the window, tie-break on count.
    target = (config.DTE_MIN + config.DTE_MAX) / 2
    return min(by_exp, key=lambda e: (abs(_dte_of(windowed, e) - target), -by_exp[e]))


def _dte_of(options: list[NormOption], expiry: date) -> int:
    for o in options:
        if o.expiry == expiry:
            return o.dte
    return 9999


def select_by_delta(options: list[NormOption], right: str, expiry: date,
                    target_delta: float) -> Optional[NormOption]:
    """Closest |delta| to target among contracts of `right` in `expiry` that have
    a known delta. Returns None if none qualify (fail closed)."""
    cands = [o for o in options if o.right == right and o.expiry == expiry and o.abs_delta is not None]
    if not cands:
        return None
    return min(cands, key=lambda o: abs(o.abs_delta - target_delta))


def strikes_for(options: list[NormOption], right: str, expiry: date) -> list[NormOption]:
    return sorted((o for o in options if o.right == right and o.expiry == expiry), key=lambda o: o.strike)
