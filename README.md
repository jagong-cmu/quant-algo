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

## Tests

```bash
.venv/bin/python tests/test_system.py
```

Proves the guardrails **block** (not just pass): per-trade cap > 3%, book cap >
20%, VIX skip/rising/fail-closed, trend fail-closed, and every order-sanity
violation (wrong instruction, bad sign, undefined max loss, inverted strikes,
non-integer quantity), plus OCC round-tripping and Black-Scholes delta/IV.

## Project layout

```
run.py                  # entry point: dry-run (default) / --mock offline demo
discover.py             # READ-ONLY: dump real PentPort response shapes
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
tests/test_system.py
```
