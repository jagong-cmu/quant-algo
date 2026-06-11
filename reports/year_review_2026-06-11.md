# Strategy Year-in-Review — 2025-06-11 to 2026-06-11

**Adaptive sizing layer applied throughout** (regime thresholds refit on a rolling 3-year window at each entry — no lookahead). Static baseline shown alongside.

> ⚠️ **Modeled, not live.** Real underlying & VIX history; option prices are Black-Scholes-modeled (no skew, mid fills, no commissions). Win/loss **direction** reflects the real price path; dollar **magnitudes** are approximate. `LIVE_TRADING` stayed `False`.

## Setup

- Period: **2025-06-11 → 2026-06-11**
- Starting equity: **$100,000** · caps: 3%/trade, 20%/book (hard, enforced after the adaptive layer)
- Universe: SPY, QQQ, IWM · structures: put credit spreads (primary), call debit spreads (risk-on only)
- Entry cadence: every 37 trading days · target 37 DTE

## How the money moved

**Adaptive: $100,000 → $129,382  ($29,382, +29.4%)**

| Month | End equity (adaptive) | Monthly P/L | Return |
|---|---:|---:|---:|
| 2025-06 | $104,127 | $4,127 | +4.1% |
| 2025-07 | $108,264 | $4,137 | +4.0% |
| 2025-08 | $112,018 | $3,754 | +3.5% |
| 2025-09 | $115,580 | $3,562 | +3.2% |
| 2025-10 | $124,970 | $9,390 | +8.1% |
| 2025-11 | $125,594 | $624 | +0.5% |
| 2025-12 | $125,594 | $0 | +0.0% |
| 2026-01 | $124,541 | $-1,053 | -0.8% |
| 2026-02 | $121,292 | $-3,249 | -2.6% |
| 2026-03 | $121,292 | $0 | +0.0% |
| 2026-04 | $121,303 | $11 | +0.0% |
| 2026-05 | $132,174 | $10,871 | +9.0% |
| 2026-06 | $129,382 | $-2,792 | -2.1% |

## Regime & sizing decisions (per entry cycle)

| Entry | Regime | Size mult | Short delta | Reduced vs base? |
|---|---|---:|---:|---|
| 2025-06-11 | NORMAL | 0.75 | 0.30 | YES |
| 2025-08-05 | ELEVATED | 0.50 | 0.25 | YES |
| 2025-09-26 | CALM | 1.00 | 0.30 | no |
| 2025-11-18 | STRESS | 0.00 | 0.20 | YES |
| 2026-01-13 | NORMAL | 0.75 | 0.30 | YES |
| 2026-03-09 | STRESS | 0.00 | 0.20 | YES |
| 2026-04-30 | NORMAL | 0.75 | 0.30 | YES |

Adaptive reduced exposure in **6/7** cycles.

## Trade log

| Entry | Sym | Structure | Short/Long K | Qty | Net | Max risk | P/L | Status |
|---|---|---|---|---:|---:|---:|---:|---|
| 2025-06-11 | IWM | put_credit | 207/206 | 32 | +0.30 | $2,240 | $960 | realized |
| 2025-06-11 | QQQ | call_debit | 553/531 | 2 | -8.71 | $1,742 | $2,658 | realized |
| 2025-06-11 | QQQ | put_credit | 518/517 | 32 | +0.31 | $2,208 | $992 | realized |
| 2025-06-11 | SPY | call_debit | 622/600 | 2 | -8.69 | $1,738 | $2,662 | realized |
| 2025-06-11 | SPY | put_credit | 587/586 | 32 | +0.31 | $2,208 | $992 | realized |
| 2025-08-05 | IWM | call_debit | 230/220 | 3 | -3.99 | $1,197 | $1,803 | realized |
| 2025-08-05 | IWM | put_credit | 212/211 | 20 | +0.27 | $1,460 | $540 | realized |
| 2025-08-05 | QQQ | call_debit | 582/559 | 1 | -9.13 | $913 | $1,387 | realized |
| 2025-08-05 | QQQ | put_credit | 540/539 | 20 | +0.27 | $1,460 | $540 | realized |
| 2025-08-05 | SPY | call_debit | 650/626 | 1 | -9.56 | $956 | $1,444 | realized |
| 2025-08-05 | SPY | put_credit | 607/606 | 20 | +0.26 | $1,480 | $520 | realized |
| 2025-09-26 | IWM | call_debit | 250/241 | 8 | -3.57 | $2,856 | $696 | realized |
| 2025-09-26 | QQQ | call_debit | 616/595 | 3 | -8.39 | $2,517 | $3,783 | realized |
| 2025-09-26 | QQQ | put_credit | 582/581 | 43 | +0.31 | $2,967 | $1,333 | realized |
| 2025-09-26 | SPY | call_debit | 682/660 | 3 | -8.83 | $2,649 | $3,951 | realized |
| 2025-09-26 | SPY | put_credit | 648/647 | 43 | +0.31 | $2,967 | $1,333 | realized |
| 2026-01-13 | IWM | call_debit | 272/261 | 5 | -4.24 | $2,120 | $-320 | realized |
| 2026-01-13 | IWM | put_credit | 254/253 | 32 | +0.31 | $2,208 | $992 | realized |
| 2026-01-13 | QQQ | call_debit | 649/625 | 2 | -9.49 | $1,898 | $-1,898 | realized |
| 2026-01-13 | QQQ | put_credit | 611/610 | 33 | +0.32 | $2,244 | $-2,244 | realized |
| 2026-01-13 | SPY | call_debit | 715/692 | 2 | -9.28 | $1,856 | $-1,856 | realized |
| 2026-01-13 | SPY | put_credit | 678/677 | 32 | +0.32 | $2,176 | $1,024 | realized |
| 2026-04-30 | IWM | call_debit | 289/277 | 4 | -4.78 | $1,912 | $25 | OPEN/MTM |
| 2026-04-30 | IWM | put_credit | 270/269 | 33 | +0.32 | $2,244 | $1,053 | OPEN/MTM |
| 2026-04-30 | QQQ | call_debit | 693/666 | 2 | -10.72 | $2,144 | $3,187 | OPEN/MTM |
| 2026-04-30 | QQQ | put_credit | 650/649 | 32 | +0.31 | $2,208 | $992 | OPEN/MTM |
| 2026-04-30 | SPY | call_debit | 742/717 | 2 | -10.00 | $2,000 | $1,808 | OPEN/MTM |
| 2026-04-30 | SPY | put_credit | 702/701 | 32 | +0.32 | $2,176 | $1,024 | OPEN/MTM |

## Final analysis — adaptive vs static

| Metric | Adaptive | Static | 
|---|---:|---:|
| Net P/L | $29,382 | $56,973 |
| Return on equity | +29.4% | +57.0% |
| Max drawdown | +5.8% | +9.3% |
| Worst single trade | $-2,244 | $-2,992 |
| Worst single day | $-6,281 | $-6,281 |
| 95% trade loss | $-1,883 | $-2,790 |
| 99% trade loss | $-2,151 | $-2,937 |
| Hit rate | +85.7% | +89.7% |
| Avg win | $1,487 | $1,887 |
| Avg loss | $-1,579 | $-2,268 |
| Sharpe* (secondary) | 1.94 | 2.72 |
| Sortino* (secondary) | 2.91 | 4.39 |

*Sharpe/Sortino flatter short premium until the tail event — judge on drawdown and loss tail, not these.*

## Bottom line

- Over the year the adaptive book moved **$100,000 → $129,382** (+29.4%).
- vs static it gave up **$27,592** of P/L while reducing max drawdown by **3.4%** — the adaptive layer trades return for tail protection by design.
- It reduced exposure in **6/7** cycles; in the rest it matched static exactly (no drag when the regime was calm).
- Premium selling shows its usual signature here: a **high hit rate with larger losers than winners** — the tail is what the guardrails and adaptive layer exist to contain.

_Generated by `year_review.py`. Keep `LIVE_TRADING=False` until you have reviewed this and the walk-forward out-of-sample results._
