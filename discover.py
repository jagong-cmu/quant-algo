#!/usr/bin/env python3
"""READ-ONLY discovery: dump the real shapes of PentPort responses.

The PentPort docs never show response bodies, so a few field names are resolved
defensively at runtime (account equity, chain greeks, quote price, positions).
This script makes the GET-only calls and prints a trimmed view of each response
so you can pin the exact fields in config/broker/chain. It NEVER places a trade.

Usage:
    PENTPORT_API_KEY=... python discover.py
"""

from __future__ import annotations

import json
import sys

from pp_options.broker import Broker, extract_equity
from pp_options.envload import load_env
from pp_options.live import _dig_quote_price


def _show(label: str, obj, depth_keys: int = 40) -> None:
    print(f"\n===== {label} =====")
    try:
        s = json.dumps(obj, indent=2, default=str)
    except TypeError:
        s = repr(obj)
    lines = s.splitlines()
    if len(lines) > 120:
        print("\n".join(lines[:120]))
        print(f"... [{len(lines) - 120} more lines truncated]")
    else:
        print(s)


def main() -> int:
    load_env()  # load .env into os.environ (no-op if absent; real env vars win)
    b = Broker()
    print("Discovery is READ-ONLY. No orders will be placed.")

    acct = b.choose_account()
    _show("choose_account()", acct)

    try:
        bal = b.account_balance()
        _show("account_balance()", bal)
        eq = extract_equity(bal)
    except Exception as e:
        print(f"\n===== account_balance() FAILED: {type(e).__name__}: {e}")
        eq = extract_equity(acct)
        print(">> falling back to account summary for equity")
    print(f"\n>> equity resolved: {eq} "
          f"({'OK' if eq else 'NOT FOUND -- pin the field in broker.EQUITY_FIELD_CANDIDATES'})")

    for name, fn in [("usage()", b.usage), ("positions()", b.positions), ("orders()", b.orders)]:
        try:
            _show(name, fn())
        except Exception as e:
            print(f"\n===== {name} FAILED: {type(e).__name__}: {e}")

    for sym in ["SPY", "$VIX", "VIX", "^VIX"]:
        try:
            q = b.quotes([sym])
            price = _dig_quote_price(q, sym)
            _show(f"quotes(['{sym}'])  -> price resolved: {price}", q)
        except Exception as e:
            print(f"\nquotes(['{sym}']) failed: {e}")

    try:
        chain = b.options_chain("SPY")
        # Show only the top-level keys + one sample contract to keep it readable.
        if isinstance(chain, dict):
            print("\n===== options_chain('SPY') top-level keys =====")
            print(list(chain.keys()))
            _show("options_chain('SPY') (trimmed)", _trim_chain(chain))
    except Exception as e:
        print(f"\noptions_chain('SPY') failed: {e}")

    print("\nDone. Use these shapes to confirm/pin field names, then run: python run.py")
    return 0


def _trim_chain(chain: dict) -> dict:
    """Return a small sample of the chain so a real contract dict is visible."""
    out = {k: v for k, v in chain.items() if not isinstance(v, (list, dict))}
    for k, v in chain.items():
        if isinstance(v, list) and v:
            out[k] = v[:2]
        elif isinstance(v, dict):
            # nested maps (TDA style): grab one expiry -> one strike -> contracts
            sample = {}
            for ek, ev in list(v.items())[:1]:
                if isinstance(ev, dict):
                    for sk, sv in list(ev.items())[:1]:
                        sample[f"{ek}/{sk}"] = sv
                else:
                    sample[ek] = ev
            out[k] = sample or v
    return out


if __name__ == "__main__":
    sys.exit(main())
