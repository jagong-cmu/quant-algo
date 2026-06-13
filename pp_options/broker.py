"""Resilient wrapper around the PentPort SDK.

Responsibilities:
  * lazy client construction (reads PENTPORT_API_KEY)
  * client-side rate limiting (stay well under 60 req/min)
  * retry with backoff; honor Retry-After on 429s
  * read-only account/state helpers used before any trading decision
  * defensive extraction of the account equity value (field name unknown until
    confirmed against a live response) -- fail closed if it cannot be found
  * the SINGLE submit path, gated by config.LIVE_TRADING

Every public method funnels API calls through _call() so rate limiting and
error handling are uniform.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

import requests

from . import config
from .logutil import get_logger, log_payload

try:  # the SDK is only needed for live/dry-run-with-real-data, not for --mock
    from pentport import PentPort, PentPortAPIError, PentPortRateLimitError
except Exception:  # pragma: no cover - allows --mock without the package installed
    PentPort = None  # type: ignore
    class PentPortAPIError(Exception):
        ...
    class PentPortRateLimitError(PentPortAPIError):
        retry_after_seconds = None


# Candidate field names for "account equity" -- the docs never show the balance
# body. We try these in order; discover.py will reveal the real one so you can
# pin it. If none is present we FAIL CLOSED (cannot size risk -> no trading).
EQUITY_FIELD_CANDIDATES = [
    "equity", "account_equity", "total_equity",
    "net_liquidation", "net_liquidation_value", "net_liq", "liquidation_value",
    "liquidationValue", "netLiquidation", "marginEquity", "account_value",
    "portfolio_value", "value", "cash_balance",
]


class Broker:
    def __init__(self, sleep: Callable[[float], None] = time.sleep):
        self._client = None
        self._account: Optional[dict] = None
        self._account_hash: Optional[str] = None
        self._sleep = sleep
        self._req_times: list[float] = []
        self.log = get_logger()

    # ---- client / rate limiting -------------------------------------------
    @property
    def client(self):
        if self._client is None:
            if PentPort is None:
                raise RuntimeError(
                    "pentport SDK not importable; use --mock for an offline dry-run."
                )
            self._client = PentPort(timeout=60)  # reads PENTPORT_API_KEY; chain endpoint is slow
        return self._client

    def _throttle(self) -> None:
        now = time.monotonic()
        window_start = now - 60.0
        self._req_times = [t for t in self._req_times if t > window_start]
        if len(self._req_times) >= config.MAX_REQUESTS_PER_MIN:
            wait = self._req_times[0] + 60.0 - now
            if wait > 0:
                self.log.warning("Local rate limit reached; sleeping %.1fs", wait)
                self._sleep(wait)
        self._req_times.append(time.monotonic())

    def _call(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        """Invoke an SDK method with throttling, retries, and 429 handling.

        Pass _idempotent=False for non-idempotent calls (order submission): on an
        ambiguous failure (timeout / 5xx) it raises immediately instead of
        retrying, so a possibly-placed order is never double-submitted.
        """
        idempotent = kwargs.pop("_idempotent", True)
        last_exc: Optional[Exception] = None
        for attempt in range(1, config.REQUEST_RETRIES + 1):
            self._throttle()
            try:
                return fn(*args, **kwargs)
            except PentPortRateLimitError as e:
                last_exc = e
                retry_after = getattr(e, "retry_after_seconds", None) or config.RETRY_BACKOFF_SECONDS * attempt
                self.log.warning("429 rate limited; honoring Retry-After=%ss (attempt %d)", retry_after, attempt)
                self._sleep(float(retry_after))
            except PentPortAPIError as e:
                last_exc = e
                status = getattr(e, "status_code", None)
                # Don't retry client errors (4xx other than 429); they won't fix themselves.
                if status is not None and 400 <= status < 500:
                    self.log.error("API error %s (no retry): %s", status, e)
                    raise
                if not idempotent:
                    self.log.error("non-idempotent call failed (HTTP %s) -> NOT retrying; "
                                   "verify via orders()/positions()", status)
                    raise
                backoff = config.RETRY_BACKOFF_SECONDS * attempt
                self.log.warning("API error %s; backing off %.1fs (attempt %d)", status, backoff, attempt)
                self._sleep(backoff)
            except requests.exceptions.RequestException as e:
                # timeouts / connection drops are not PentPortAPIError
                last_exc = e
                if not idempotent:
                    self.log.error("non-idempotent call network error (%s) -> order status UNKNOWN; "
                                   "NOT retrying; verify via orders()/positions()", type(e).__name__)
                    raise
                backoff = config.RETRY_BACKOFF_SECONDS * attempt
                self.log.warning("network error (%s); backing off %.1fs (attempt %d)",
                                 type(e).__name__, backoff, attempt)
                self._sleep(backoff)
        assert last_exc is not None
        raise last_exc

    # ---- read-only state ---------------------------------------------------
    def choose_account(self) -> dict:
        acct = self._call(self.client.choose_account, config.ACCOUNT_PRODUCT)
        self._account = acct
        self._account_hash = acct.get("account_hash")
        self.log.info("Chose %s account: hash=%s", config.ACCOUNT_PRODUCT, self._account_hash)
        return acct

    @property
    def account_hash(self) -> str:
        if not self._account_hash:
            self.choose_account()
        assert self._account_hash
        return self._account_hash

    def accounts(self) -> list[dict]:
        return self._call(self.client.accounts)

    def account_balance(self) -> dict:
        return self._call(self.client.account_balance, account_hash=self.account_hash)

    def positions(self) -> dict:
        return self._call(self.client.positions, account_hash=self.account_hash)

    def orders(self) -> dict:
        return self._call(self.client.orders, account_hash=self.account_hash)

    def usage(self) -> dict:
        return self._call(self.client.usage)

    def quotes(self, symbols) -> dict:
        return self._call(self.client.quotes, symbols, account_hash=self.account_hash)

    def options_chain(self, symbol: str, expiry: Optional[str] = None) -> dict:
        return self._call(self.client.options_chain, symbol, expiry=expiry, account_hash=self.account_hash)

    # ---- derived helpers ---------------------------------------------------
    def equity(self) -> float:
        """Account equity, failing closed if it cannot be found.

        Prefers account_balance(); falls back to the account summary
        (account_value / cash_balance from choose_account) when the balance
        endpoint is unavailable -- it returns HTTP 500 for competition accounts.
        """
        val = None
        try:
            bal = self.account_balance()
            val = extract_equity(bal)
        except PentPortAPIError as e:
            self.log.warning("account_balance() unavailable (%s); falling back to account summary.", e)

        if val is None:
            acct = self._account or self.choose_account()
            val = extract_equity(acct)
            if val is not None:
                self.log.info("Equity sourced from account summary (account_value/cash_balance).")

        if val is None:
            raise RuntimeError(
                "Could not determine account equity from account_balance() or the "
                f"account summary. Tried {EQUITY_FIELD_CANDIDATES}. Run discover.py to "
                "see the real shape. Failing closed (no sizing -> no trading)."
            )
        self.log.info("Account equity = %.2f", val)
        return val

    # ---- the ONLY submit path ---------------------------------------------
    def submit_options_order(self, payload: dict) -> dict:
        """Submit a multi-leg option order -- or, if LIVE_TRADING is False, log
        the exact payload that WOULD be sent and return a simulated ack."""
        if not config.LIVE_TRADING:
            log_payload(self.log, "[DRY-RUN] WOULD SUBMIT trade_options payload:", payload)
            return {"ok": True, "dry_run": True, "simulated": True}

        log_payload(self.log, "[LIVE] SUBMITTING trade_options payload:", payload)
        result = self._call(
            self.client.trade_options,
            legs=payload["legs"],
            order_type=payload["order_type"],
            price=payload.get("price"),
            duration=payload.get("duration", "DAY"),
            session_type=payload.get("session_type", "NORMAL"),
            complex_order_strategy_type=payload.get("complex_order_strategy_type", "NONE"),
            order_strategy_type=payload.get("order_strategy_type", "SINGLE"),
            account_hash=self.account_hash,
            _idempotent=False,   # never blindly retry a possibly-placed order
        )
        log_payload(self.log, "[LIVE] trade_options response:", result if isinstance(result, dict) else {"result": result})
        return result


def extract_equity(balance: dict) -> Optional[float]:
    """Best-effort search for an equity/net-liquidation value in a balance dict.

    Searches top-level keys first (case-insensitive), then one level of nested
    dicts (e.g. {'balances': {...}} or {'securitiesAccount': {...}}).
    """
    if not isinstance(balance, dict):
        return None

    def _search(d: dict) -> Optional[float]:
        lowered = {str(k).lower(): v for k, v in d.items()}
        for cand in EQUITY_FIELD_CANDIDATES:
            if cand.lower() in lowered:
                v = lowered[cand.lower()]
                try:
                    fv = float(v)
                    if fv > 0:
                        return fv
                except (TypeError, ValueError):
                    continue
        return None

    top = _search(balance)
    if top is not None:
        return top
    for v in balance.values():
        if isinstance(v, dict):
            nested = _search(v)
            if nested is not None:
                return nested
    return None
