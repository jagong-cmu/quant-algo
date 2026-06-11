"""OCC option symbol construction, parsing, and validation.

PentPort uses the standard OCC contract symbol with no internal padding, e.g.
    AAPL260515C00200000
    = <root><YYMMDD><C|P><strike * 1000, zero-padded to 8 digits>

This module is the single source of truth for symbol <-> fields conversion and
is used by the order sanity checks (a leg whose symbol does not round-trip is
rejected).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

_OCC_RE = re.compile(r"^(?P<root>[A-Z]{1,6})(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<cp>[CP])(?P<strike>\d{8})$")


@dataclass(frozen=True)
class OccOption:
    root: str
    expiry: date
    right: str          # "C" or "P"
    strike: float

    @property
    def symbol(self) -> str:
        return build_occ(self.root, self.expiry, self.right, self.strike)


def build_occ(root: str, expiry: date, right: str, strike: float) -> str:
    root = root.strip().upper()
    right = right.strip().upper()
    if not re.fullmatch(r"[A-Z]{1,6}", root):
        raise ValueError(f"bad option root: {root!r}")
    if right not in ("C", "P"):
        raise ValueError(f"right must be 'C' or 'P', got {right!r}")
    strike_milli = int(round(strike * 1000))
    if strike_milli <= 0:
        raise ValueError(f"strike must be positive, got {strike}")
    return f"{root}{expiry:%y%m%d}{right}{strike_milli:08d}"


def parse_occ(symbol: str) -> OccOption:
    m = _OCC_RE.match(symbol.strip().upper())
    if not m:
        raise ValueError(f"not a valid OCC symbol: {symbol!r}")
    yy = int(m.group("yy"))
    expiry = date(2000 + yy, int(m.group("mm")), int(m.group("dd")))
    return OccOption(
        root=m.group("root"),
        expiry=expiry,
        right=m.group("cp"),
        strike=int(m.group("strike")) / 1000.0,
    )


def is_valid_occ(symbol: str) -> bool:
    try:
        parsed = parse_occ(symbol)
        # round-trip must be exact
        return parsed.symbol == symbol.strip().upper()
    except (ValueError, Exception):
        return False
