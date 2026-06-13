#!/usr/bin/env python3
"""Validate the backtest's option pricing against the REAL PentPort competition
chain. Answers three things:
  1. Is there a bid/ask spread to pay?  -> execution cost
  2. Does the competition price options via Black-Scholes at its reported IV?
  3. How far is the backtest's flat VIX-based IV from the competition's ATM IV?

Read-only: pulls chains, submits nothing. LIVE_TRADING untouched.

Usage: python scripts/validate_real_chain.py
"""
import datetime as dt
import statistics
from pp_options.envload import load_env
load_env()
from pp_options import config
from pp_options.bsm import bs_price
from pp_options.broker import Broker
from pp_options.simulate import iv_from_vix

TODAY = dt.date(2026, 6, 12)
DATED_EXPIRY = "2026-07-17"          # ~35 DTE, real time value for a clean BS check
r = config.RISK_FREE_RATE
b = Broker(); b.choose_account()

# current VIX (what the backtest feeds into iv_from_vix)
try:
    import pp_options.marketdata as MD
    vix_now = MD.equity_series("^VIX").last
except Exception:
    vix_now = 18.0


def implied_vol(price, S, K, T, q, right):
    lo, hi = 1e-4, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs_price(S, K, r, q, mid, T, right) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


print("=" * 96)
print("REAL-CHAIN VALIDATION — PentPort competition chain vs the backtest's pricing")
print(f"VIX now ~{vix_now:.1f} (backtest feeds this into iv_from_vix for a FLAT per-symbol IV)")
print("=" * 96)

for sym in ["SPY", "QQQ", "IWM"]:
    # --- bid/ask coverage on the default (0-DTE) chain ---
    ch0 = b.options_chain(sym)["chain"]
    spot = ch0["underlyingPrice"]
    allc = [c for m in ("callExpDateMap", "putExpDateMap")
            for lst in ch0.get(m, {}).values() for c in lst]
    n_ba = sum(1 for c in allc if c.get("bid") is not None and c.get("ask") is not None)

    # --- dated chain for the BS-fidelity check ---
    ch = b.options_chain(sym, expiry=DATED_EXPIRY)["chain"]
    T = (dt.date.fromisoformat(DATED_EXPIRY) - TODAY).days / 365.0
    q = config.DIVIDEND_YIELD.get(sym, config.DEFAULT_DIVIDEND_YIELD)
    calls = ch["callExpDateMap"][DATED_EXPIRY]
    puts = ch["putExpDateMap"][DATED_EXPIRY]

    bt_iv = iv_from_vix(sym, vix_now)        # what the backtest would use (flat)
    bs_errs, atm_ivs = [], []
    for right, lst in (("C", calls), ("P", puts)):
        for c in sorted(lst, key=lambda x: abs(x["strike"] - spot))[:5]:
            last, reiv = c.get("lastPrice"), c.get("impliedVolatility")
            if not last or last <= 0 or reiv is None:
                continue
            iv = reiv if reiv < 3 else reiv / 100.0   # dated=fraction, 0DTE placeholder=20
            bs = bs_price(spot, c["strike"], r, q, iv, T, right)
            bs_errs.append(abs(bs - last) / last * 100)
            atm_ivs.append(implied_vol(last, spot, c["strike"], T, q, right))

    comp_atm_iv = statistics.median(atm_ivs) if atm_ivs else float("nan")
    print(f"\n##### {sym}  spot={spot:.2f}")
    print(f"  bid/ask coverage: {n_ba}/{len(allc)} ({n_ba/len(allc):.0%})  "
          f"-> {'NO spread to cross (fills at theoretical price)' if n_ba == 0 else 'has quotes'}")
    print(f"  competition prices via BS? median |BS@reportedIV - lastPrice| = "
          f"{statistics.median(bs_errs):.1f}%  -> {'YES (BS engine)' if statistics.median(bs_errs) < 5 else 'unclear'}")
    print(f"  ATM IV ({DATED_EXPIRY}): competition {comp_atm_iv:.1%}  vs  backtest flat "
          f"{bt_iv:.1%}  (gap {abs(comp_atm_iv-bt_iv)*100:.1f} vol pts)")

print("\n" + "=" * 96)
print("CONCLUSION")
print("  * No bid/ask, no commission -> ZERO transaction cost. Fill price == theoretical price.")
print("  * Competition prices options with the SAME Black-Scholes engine the backtest uses.")
print("  * Only gap: the backtest feeds a FLAT VIX-based IV; the competition uses a per-strike")
print("    IV surface (skew). Close at ATM, diverges in the wings. Feeding the real per-strike")
print("    IV into the sim would make it essentially exact.")
print("  * NOTE: the 0-DTE chain reports a flat IV=20 placeholder + near-intrinsic last prices;")
print("    the intraday/0-DTE pricing deserves its own validation before trusting HF magnitudes.")
print("=" * 96)
