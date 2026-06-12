#!/usr/bin/env python3
"""Proves the SpreadExecutor can NEVER leave a naked short, on a venue that legs
out orders and offers no cancel.

Run:  .venv/bin/python tests/test_execution.py
"""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

from pp_options.execution import SpreadExecutor
from pp_options.models import Leg, Spread
from tests.test_system import _put   # reuse the NormOption helper

PASSED = FAILED = 0


def check(name, cond):
    global PASSED, FAILED
    if cond:
        PASSED += 1; print(f"  PASS  {name}")
    else:
        FAILED += 1; print(f"  FAIL  {name}")


class FakeBroker:
    """Simulates leg-out: a leg is 'held' only if listed in `fills`."""
    def __init__(self, fills: dict):
        self.fills = fills           # symbol -> "long" | "short"
        self.submitted: list = []    # (symbol, instruction, qty) in order
        self.held: dict = {}
        self.log = logging.getLogger("test_exec")

    def submit_options_order(self, payload):
        leg = payload["legs"][0]
        self.submitted.append((leg["symbol"], leg["instruction"], leg["quantity"]))
        f = self.fills.get(leg["symbol"])
        if f == "long":
            self.held[leg["symbol"]] = (leg["quantity"], 0)
        elif f == "short":
            l, _ = self.held.get(leg["symbol"], (0, 0))
            self.held[leg["symbol"]] = (l, leg["quantity"])
        return {"ok": True}

    def positions(self):
        plist = [{"instrument": {"symbol": s}, "longQuantity": l, "shortQuantity": sh}
                 for s, (l, sh) in self.held.items()]
        return {"positions": [{"securitiesAccount": {"positions": plist}}]}


EXP = date.today() + timedelta(days=36)
SHORT = _put(720, -0.30, 8.30)   # SELL_TO_OPEN (the would-be-naked leg)
LONG = _put(715, -0.22, 7.20)    # BUY_TO_OPEN  (protective)
SPREAD = Spread("SPY", "put_credit", Leg(SHORT, "SELL_TO_OPEN"), Leg(LONG, "BUY_TO_OPEN"),
                contracts=1, net_credit=1.10)


def _exec(fills):
    b = FakeBroker(fills)
    r = SpreadExecutor(b, poll_interval=0, fill_timeout=0, sleep=lambda x: None).open_spread(SPREAD)
    return b, r


def test_both_fill():
    b, r = _exec({LONG.symbol: "long", SHORT.symbol: "short"})
    check("both legs fill -> COMPLETE", r.status == "COMPLETE")
    check("long submitted BEFORE short", b.submitted[0][0] == LONG.symbol and b.submitted[1][0] == SHORT.symbol)
    check("not naked", not r.naked)


def test_short_does_not_fill():
    b, r = _exec({LONG.symbol: "long"})   # long fills, short legs out
    check("short legs out -> LONG_ONLY", r.status == "LONG_ONLY")
    check("long was still submitted first", b.submitted[0][0] == LONG.symbol)
    check("NOT naked (we hold the long, short unfilled)", not r.naked)


def test_long_does_not_fill():
    b, r = _exec({})   # long never fills
    check("long never fills -> NONE", r.status == "NONE")
    check("short was NEVER submitted", all(s[0] != SHORT.symbol for s in b.submitted))
    check("NOT naked (no position at all)", not r.naked)


def test_never_naked_any_order():
    # whatever fills, the result must never be naked
    import itertools
    ok = True
    for combo in itertools.product([None, "long"], [None, "short"]):
        fills = {}
        if combo[0]: fills[LONG.symbol] = "long"
        if combo[1] and combo[0]: fills[SHORT.symbol] = "short"  # short can only fill after long sent
        _, r = _exec(fills)
        if r.naked:
            ok = False
    check("never naked across all fill combinations", ok)


def main():
    for fn in (test_both_fill, test_short_does_not_fill, test_long_does_not_fill, test_never_naked_any_order):
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{'='*50}\nPASSED={PASSED}  FAILED={FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
