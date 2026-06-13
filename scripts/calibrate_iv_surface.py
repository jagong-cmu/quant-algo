#!/usr/bin/env python3
"""Calibrate the IV surface from the live PentPort competition chain and save it
to state/iv_surface.json (read-only on the chain; submits nothing).

Fits IV(k) = atm*(1 + beta*k + alpha*k^2) per symbol from OTM contracts at a
~35-DTE expiry, and anchors atm = f*VIX/100 via the observed ATM-IV/VIX ratio.
"""
import argparse
import datetime as dt
import math
from pp_options.envload import load_env
load_env()
from pp_options import ivsurface
from pp_options.broker import Broker

ap = argparse.ArgumentParser(description="Calibrate an IV surface from the live chain")
ap.add_argument("--expiry", default="2026-07-17", help="chain expiry to calibrate from")
ap.add_argument("--out", default=ivsurface.PATH, help="output JSON path")
args = ap.parse_args()

TODAY = dt.date(2026, 6, 12)
EXP = args.expiry
SYMS = ["SPY", "QQQ", "IWM", "DIA", "GLD", "EEM", "EFA", "TLT"]

try:
    import pp_options.marketdata as MD
    vix_now = MD.equity_series("^VIX").last
except Exception:
    vix_now = 18.0

b = Broker(); b.choose_account()
target_dte = (dt.date.fromisoformat(EXP) - TODAY).days   # desired tenor; pick nearest per symbol
sym_points = {}
sym_dte = {}
for sym in SYMS:
    # pick the symbol's available expiry nearest the target tenor (expiry dates vary by ticker)
    expiries = b.options_chain(sym)["chain"].get("allExpiries", [])
    dated = [(e, (dt.date.fromisoformat(e) - TODAY).days) for e in expiries]
    dated = [(e, d) for e, d in dated if d >= 7]
    if not dated:
        print(f"{sym}: no usable expiry"); continue
    exp, dte = min(dated, key=lambda x: abs(x[1] - target_dte))
    ch = b.options_chain(sym, expiry=exp)["chain"]
    S = ch["underlyingPrice"]
    pts = []
    sym_dte[sym] = dte
    for right, mp in (("P", ch["putExpDateMap"].get(exp, [])), ("C", ch["callExpDateMap"].get(exp, []))):
        for c in mp:
            K, iv = c["strike"], c.get("impliedVolatility")
            if iv is None:
                continue
            iv = iv if iv < 3 else iv / 100.0
            k = math.log(K / S)
            otm = (right == "P" and K <= S) or (right == "C" and K >= S)
            if otm and 0.03 < iv < 1.2 and abs(k) < 0.18:   # clean OTM band
                pts.append((k, iv))
    sym_points[sym] = pts
    print(f"{sym}: S={S:.2f}  exp={exp} ({dte} DTE)  collected {len(pts)} OTM IV points")

model = ivsurface.calibrate(sym_points, vix_now, target_dte)
ivsurface.save(model, args.out)

print(f"\nCalibrated against VIX={vix_now:.1f}, ~{target_dte} DTE per-symbol nearest. Saved {args.out}")
print(f"  {'sym':<5}{'ATM/VIX f':>10}{'beta':>9}{'alpha':>9}{'atm_iv':>9}")
for sym, p in model.coeffs.items():
    print(f"  {sym:<5}{p['f']:>10.3f}{p['beta']:>9.2f}{p['alpha']:>9.1f}{p['atm_iv']:>9.3f}")
# show the fitted skew at representative strikes
print("\n  fitted IV by moneyness (anchored to current VIX):")
print(f"  {'sym':<5}{'-10% (put)':>12}{'-5%':>8}{'ATM':>8}{'+5%':>8}{'+10% (call)':>12}")
for sym, p in model.coeffs.items():
    row = [model.iv(sym, 100.0, 100.0 * math.exp(k), vix_now) for k in (-0.10, -0.05, 0, 0.05, 0.10)]
    print(f"  {sym:<5}{row[0]:>12.1%}{row[1]:>8.1%}{row[2]:>8.1%}{row[3]:>8.1%}{row[4]:>12.1%}")
