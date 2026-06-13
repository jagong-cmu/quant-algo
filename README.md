# PentPort Defined-Risk Premium-Selling System

An options-trading system built on the [`pentport`](https://pypi.org/project/pentport/)
SDK that sells premium on liquid index ETFs (**SPY, QQQ, IWM**) using **only
defined-risk structures**. Every position has a computable maximum loss; the
system refuses to submit anything whose max loss it cannot compute.

> **Safety posture:** the system is **dry-run by default**. It will not place a
> live order until you flip `LIVE_TRADING = True` in `pp_options/config.py`
> yourself, and even then every order must pass all four guardrails first.

---

## Strategy

| | Structure | Entry logic |
|---|---|---|
| **Primary** | Put credit spread | Sell ~30-delta short put, buy a further-OTM long put to cap loss. Target **30–45 DTE**. |
| **Secondary** | Call debit spread | Buy ~ATM call, sell a further-OTM call. Only when the regime is **risk-on** (underlying above its 200-day SMA). |

There are **no naked/uncovered short options anywhere**. A spread whose max loss
cannot be computed is rejected before it is ever sized.

---

## Guardrails (hard preconditions that BLOCK submission)

All four run on every candidate in `pp_options/guardrails.py`. They are **not
configurable away** — there is no flag that disables them, and anything that
cannot be *verified* (missing price, unknown VIX, unparseable symbol) **blocks**
(fail-closed).

1. **Per-trade risk cap** — defined max loss per spread **≤ 3% of account
   equity** (`MAX_TRADE_RISK_PCT`).
2. **Total-book risk cap** — sum of all open defined-risk max loss **≤ 20% of
   equity** (`MAX_BOOK_RISK_PCT`). A new order that would breach this is blocked.
   Book risk is tracked in a local ledger (`state/book_ledger.json`) of spreads
   this system opened, cross-checked against `positions()` (it warns loudly if
   the broker reports option legs the ledger doesn't know about).
3. **Volatility regime filter** — a **VIX quote is pulled before any
   premium-selling order**:
   - VIX ≥ `VIX_SKIP` (28) → **skip** new short premium.
   - VIX up ≥ `VIX_RISING_DOD_PCT` (12%) day-over-day → **skip** (rising sharply).
   - VIX ≥ `VIX_ELEVATED` (22) → **scale down** size (× `SCALE_DOWN_FACTOR`).
   - VIX unavailable → **fail closed** (no short premium).
   - Call debit spreads additionally require the underlying **above its 200-day
     SMA**; otherwise they are skipped.
4. **Order sanity checks** — on every order, before submit: leg symbols parse as
   valid OCC and round-trip; quantities are positive integers; buy/sell
   instructions match the intended structure (e.g. put credit = short put
   `SELL_TO_OPEN` above long put `BUY_TO_OPEN`); the net debit/credit has the
   expected sign; and max loss is computable. **Abort + log if anything is off.**

---

## How to run — dry-run first

```bash
# 1. Install (needs Python >= 3.10)
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Offline demo — synthetic data, NO API key, NO network to PentPort.
#    Exercises the entire pipeline and prints the orders it WOULD place.
.venv/bin/python run.py --mock

# 3. Read-only discovery against the real API (confirms response field names).
export PENTPORT_API_KEY="pp_live_..."
.venv/bin/python discover.py

# 4. Dry-run against LIVE market data (still does NOT submit — LIVE_TRADING is False).
.venv/bin/python run.py
```

Each run writes a timestamped log to `logs/pp_options_<tag>_<UTC>.log` recording
**every decision**: entries, skips with reasons, guardrail blocks with reasons,
and the exact order payload that would be sent.

### Going live (deliberate, manual)

There is **no `--live` CLI flag** on purpose. After you have reviewed the logged
would-be orders:

1. Open `pp_options/config.py` and set `LIVE_TRADING = True`.
2. Run `python run.py`. Orders that pass all guardrails are submitted via
   `trade_options()`; everything else is still logged and skipped/blocked.

---

## Trade management (close early, never hold to expiry)

The autonomous runner (`autorun.py`) does **not** hold spreads to expiration.
Every cycle each tracked spread is marked-to-market from live option quotes and
closed on the **first** of:

| Trigger | Default | Why |
|---|---|---|
| Profit target | 50% of max profit captured | best risk-adjusted exit for short premium |
| Stop-loss | loss ≥ 2× credit received | caps the downside before expiry |
| Time stop | DTE ≤ 21 | exits before the high-gamma final weeks |
| Hard backstop | DTE ≤ 7 | never carry inside a week of expiry |

Thresholds live at the top of `pp_options/runner.py`
(`PROFIT_TARGET_FRAC`, `STOP_LOSS_MULT`, `MANAGE_DTE`, `EXIT_DTE`). Closes unwind
the **short leg first** (never momentarily naked).

**Can't close → you get notified.** In paper/dry-run nothing is really
submitted, and a live close can error. In either case the runner *keeps* the
position, flags it `close_pending`, and raises an **action-required alert**
(`pp_options/notify.py` → `state/alerts.json`, logged at CRITICAL, and POSTed to
`PP_ALERT_WEBHOOK` if set) listing the exact legs to close manually. It never
silently drops a live spread.

## Dashboard (observe + control)

A dependency-free local UI:

```bash
python dashboard.py            # http://127.0.0.1:8787
# PP_DASH_PORT=9000 python dashboard.py
# PP_ROOT=/path/to/checkout python dashboard.py   # watch a runner in another checkout
```

It shows mode (PAPER/LIVE), market state, equity, day P/L, the kill switch, open
positions (with live mark + profit %), action-required alerts, a tail of the
latest runner log, and the live config. Buttons let you **Halt/Resume** entries,
**Close** a position, **Mark closed** (drop a manually-closed spread), and
**Dismiss** alerts. The dashboard never touches the broker — it enqueues into
`state/commands.json`, which the runner drains each cycle (the runner stays the
single execution path), so it's safe to run alongside a live session.

---

## What was verified against the real package/docs

- The SDK is class-based: `pp = PentPort()` then `accounts()`,
  `choose_account("options")`, `account_balance()`, `positions()`, `orders()`,
  `quotes()`, `options_chain()`, `trade_options()`. Exceptions
  `PentPortAPIError` / `PentPortRateLimitError` (the 429 carries
  `retry_after_seconds`). Rate limit **60/min** — we throttle to 45/min and
  honor `Retry-After`.
- `trade_options` legs use OCC symbols and `instruction` =
  `BUY_TO_OPEN` / `SELL_TO_OPEN`, with `complex_order_strategy_type="VERTICAL"`
  and a net limit `price`. The order field names match the **Schwab / TD
  Ameritrade Trader API**, so the payload + the positive-magnitude net-price
  convention follow that lineage.

## Known unknowns / things to confirm with `discover.py`

The PentPort docs do not show response bodies, so a few field names are resolved
defensively at runtime. Run `discover.py` (read-only) to see the real shapes and
pin them:

- **Account equity field** in `account_balance()` — searched via a candidate
  list in `broker.EQUITY_FIELD_CANDIDATES`. If none is found, the system **fails
  closed** (no equity → no sizing → no trading).
- **Options chain greeks** — if `options_chain()` does not return `delta`, the
  system computes a **Black-Scholes delta** (implying IV from the contract mid
  when no IV is given) to target ~0.30. If neither delta nor a usable price/IV
  exists, that contract is skipped (fail-closed).
- **Credit-spread net-price sign** — sent as a positive magnitude (Schwab/TDA
  convention). Confirm against a real order response before going live.

## External data dependency

PentPort has **no equity price-history endpoint**, so the 200-day SMA (trend
filter) and the VIX series come from **Yahoo Finance's public chart endpoint**
(free, no API key, `requests` only) in `pp_options/marketdata.py`. It is
read-only and never used for trading. Swap `_fetch()` there for any other
provider — the rest of the system only depends on the `DailySeries` contract.

## Adaptive sizing layer (risk management, not alpha)

An optional layer that adjusts sizing/strike-delta to manage **risk** — it can
only ever make the book **equal-or-more conservative** than the base rules, and
the 3%/20% hard caps are checked *after* it and override it.

- **Interpretable regime classifier** (`pp_options/regime_model.py`): a
  transparent rules-based label (`CALM/NORMAL/ELEVATED/STRESS`) from VIX level,
  VIX term structure (VIX/VIX3M backwardation), and trailing realized vol. The
  only *fitted* parameters are VIX/realized-vol **quantile thresholds** estimated
  on a past window; the regime→policy map is a **fixed defensive table**, never
  optimized against P/L. It predicts *regime*, never returns.
- **What it may adjust (within hard limits only):** a size multiplier in
  `[0,1]` (never > 1.0), short-leg delta in `[0.20, 0.30]` (never closer than
  0.30), and a skip decision. `clamp_to_base()` guarantees the output is ≤ the
  base rule on every axis — proven over 2,000 randomized inputs in tests.

### Walk-forward validation (required before trusting it)

```bash
python walkforward.py                       # rolling 3y train / 6mo test / 6mo step
python walkforward.py --expanding --range 10y --cadence 21   # stress-test windows
```

Trains thresholds on a past window, tests on **strictly future unseen** data,
rolls forward (no lookahead in fitting or regime labels). Reports, per-fold and
aggregated, **adaptive vs static (model-off) side-by-side** for every metric:
CAGR, max drawdown, worst trade, worst day, 95%/99% loss, hit rate, avg win/loss,
and Sharpe/Sortino (flagged *secondary* — they flatter short premium until the
tail). Folds with **<30 trades are labeled LOW-CONFIDENCE** and excluded from
means. The adaptive-minus-static return difference gets a **bootstrap 95% CI +
permutation p-value**; the verdict is allowed to say *"no demonstrable edge."*
**Model-decay detection** flags persistent vol > forecast regime, a pinned
multiplier, or OOS drawdown beyond the worst prior fold, and prints retrain
cadence + triggers.

Current result (modeled options, 2018–2026): de-risked in 7/10 folds, **cut the
2022 bear-market drawdown from 32.4% → 24.9%**, matched static exactly in calm
uptrends; mean return is lower (the expected cost of de-risking) and there is **no
statistically demonstrable risk-adjusted edge at this sample size** — by design,
the call on whether the insurance is worth it is the operator's.

## Tests

```bash
.venv/bin/python tests/test_system.py      # guardrails + OCC + Black-Scholes
.venv/bin/python tests/test_adaptive.py    # adaptive-layer invariants
```

`test_system` proves the guardrails **block** (not just pass): per-trade cap >
3%, book cap > 20%, VIX skip/rising/fail-closed, trend fail-closed, and every
order-sanity violation, plus OCC round-tripping and Black-Scholes delta/IV.
`test_adaptive` proves the layer can **only de-risk** (multiplier ∈ [0,1], delta
∈ [0.20,0.30], never above base) and that the hard caps still override it.

## Project layout

```
run.py                  # entry point: dry-run (default) / --mock offline demo
discover.py             # READ-ONLY: dump real PentPort response shapes
backtest.py             # one-shot historical simulation ("what if a month ago?")
walkforward.py          # walk-forward validation of the adaptive sizing layer
requirements.txt
pp_options/
  config.py             # LIVE_TRADING switch + hard risk limits + universe
  broker.py             # PentPort wrapper: throttle, retry, 429s, gated submit
  live.py               # live DataSource (PentPort + Yahoo)
  mockdata.py           # synthetic DataSource for the offline demo
  marketdata.py         # Yahoo daily closes -> 200d SMA + VIX
  chain.py              # normalize options_chain, BS-delta strike selection
  bsm.py                # Black-Scholes price/delta + implied-vol solver
  occ.py                # OCC symbol build/parse/validate
  models.py             # Leg / Spread + order-payload builder
  strategy.py           # build put-credit / call-debit spreads, size by risk
  guardrails.py         # the 4 hard preconditions (fail-closed)
  risk.py               # book-risk tracker + ledger
  engine.py             # orchestration: state -> regime -> build -> guard -> submit
  regime_model.py       # adaptive sizing layer: regime classifier + clamp-to-base
  simulate.py           # BS backtest engine (pluggable sizer, daily mark-to-model)
  metrics.py            # CAGR / drawdown / tail-loss / hit-rate / Sharpe-Sortino
  stats.py              # bootstrap CI + permutation p-value
tests/test_system.py    # guardrail / OCC / BSM tests
tests/test_adaptive.py  # adaptive-layer invariant tests
```
