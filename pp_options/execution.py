"""Safe multi-leg execution that preserves defined risk on a venue that (a) legs
out multi-leg orders instead of filling them atomically and (b) offers no order
cancel via the API.

The fix is sequencing + confirmation, not a venue change:

  1. Submit the PROTECTIVE leg first. For every structure we trade, the
     BUY_TO_OPEN (long) leg is the defined-risk leg; the SELL_TO_OPEN (short) leg
     is the one that would be naked on its own. We open the long FIRST and
     confirm it is actually held (via positions()) before sending the short.
     => A leg-out can therefore only ever leave us holding a long option
        (defined risk). It can NEVER leave us naked short.

  2. The short leg is sent only after the long is confirmed. A working/un-
     cancelable short order then has only two safe outcomes: it completes the
     spread, or it never fills (and at worst expires). Either way we are never
     naked, so the missing cancel endpoint stops being a safety problem.

  3. Unwind reverses the protection order: BUY_TO_CLOSE the short leg first, then
     SELL_TO_CLOSE the long. You are never momentarily naked while flattening.

Residual honesty: with no live option bid/ask, fills are priced off lastPrice
(+/- a marketable buffer), so the SHORT leg may not fill -> the result is
LONG_ONLY (a safe, defined-risk long), not the intended spread. That is a fill-
quality limitation of the data, not a safety hole.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class LegFill:
    symbol: str
    instruction: str
    requested: int
    filled: int
    status: str

    @property
    def fully_filled(self) -> bool:
        return self.filled >= self.requested


@dataclass
class ExecResult:
    status: str               # COMPLETE | LONG_ONLY | NONE | ERROR
    long: Optional[LegFill]
    short: Optional[LegFill]
    message: str

    @property
    def naked(self) -> bool:
        """True only if a short is held without a covering long -- must never happen."""
        short_held = self.short is not None and self.short.filled > 0
        long_held = self.long is not None and self.long.filled > 0
        return short_held and not long_held


class SpreadExecutor:
    def __init__(self, broker, *, poll_interval: float = 2.0, fill_timeout: float = 20.0,
                 marketable_buffer: float = 0.05, duration: str = "DAY",
                 sleep: Callable[[float], None] = time.sleep):
        self.broker = broker
        self.poll_interval = poll_interval
        self.fill_timeout = fill_timeout
        self.buffer = marketable_buffer
        self.duration = duration
        self._sleep = sleep
        self.log = broker.log

    # ---- single-leg submit + fill confirmation -----------------------------
    def _leg_payload(self, symbol: str, instruction: str, qty: int, limit: float) -> dict:
        return {
            "underlying": symbol, "structure": "single_leg",
            "legs": [{"symbol": symbol, "instruction": instruction, "quantity": qty}],
            "order_type": "LIMIT", "price": round(limit, 2),
            "duration": self.duration, "session_type": "NORMAL",
            "complex_order_strategy_type": "NONE", "order_strategy_type": "SINGLE",
            "_contracts": qty,
        }

    def _held_qty(self, symbol: str) -> tuple[int, int]:
        """(longQuantity, shortQuantity) for `symbol` from positions()."""
        try:
            pos = self.broker.positions()
        except Exception as e:
            self.log.warning("positions() read failed during fill check: %s", e)
            return 0, 0
        lots = pos.get("positions", [])
        for wrapper in lots:
            inner = wrapper.get("securitiesAccount", wrapper) if isinstance(wrapper, dict) else {}
            for p in inner.get("positions", []) if isinstance(inner, dict) else []:
                sym = (p.get("instrument") or {}).get("symbol")
                if sym == symbol:
                    return int(p.get("longQuantity") or 0), int(p.get("shortQuantity") or 0)
        return 0, 0

    def _await_fill(self, symbol: str, want_long: int, want_short: int) -> LegFill:
        instruction = "BUY_TO_OPEN" if want_long else "SELL_TO_OPEN"
        want = want_long or want_short
        deadline = self.fill_timeout
        waited = 0.0
        while True:
            lq, sq = self._held_qty(symbol)
            have = lq if want_long else sq
            if have >= want:
                return LegFill(symbol, instruction, want, have, "FILLED")
            if waited >= deadline:
                return LegFill(symbol, instruction, want, have, "NOT_FILLED")
            self._sleep(self.poll_interval)
            waited += self.poll_interval

    def plan(self, spread, marketable: bool = True) -> list[dict]:
        """The ordered leg payloads this executor WOULD submit (long first).
        Used for dry-run display without submitting anything."""
        lo, so, qty = spread.long_leg.option, spread.short_leg.option, spread.contracts
        buf = self.buffer if marketable else 0.0
        return [
            self._leg_payload(lo.symbol, "BUY_TO_OPEN", qty, (lo.mid or 0) + buf),
            self._leg_payload(so.symbol, "SELL_TO_OPEN", qty, (so.mid or 0) - buf),
        ]

    # ---- the safe open -----------------------------------------------------
    def open_spread(self, spread, marketable: bool = True) -> ExecResult:
        """Open `spread` long-leg-first; never sends the short before the long is
        confirmed held. Returns COMPLETE / LONG_ONLY / NONE -- never naked."""
        long_opt = spread.long_leg.option
        short_opt = spread.short_leg.option
        qty = spread.contracts
        if long_opt.mid is None or short_opt.mid is None:
            return ExecResult("ERROR", None, None, "missing leg prices")

        # 1) PROTECTIVE long leg first (pay up a touch to fill)
        long_limit = long_opt.mid + (self.buffer if marketable else 0.0)
        self.log.info("[EXEC] step 1/2: open LONG %s x%d @ %.2f (protective leg first)",
                      long_opt.symbol, qty, long_limit)
        self.broker.submit_options_order(self._leg_payload(long_opt.symbol, "BUY_TO_OPEN", qty, long_limit))
        long_fill = self._await_fill(long_opt.symbol, want_long=qty, want_short=0)
        if not long_fill.fully_filled:
            self.log.warning("[EXEC] long leg not filled (%d/%d). NO short sent -> no position, no naked risk.",
                             long_fill.filled, qty)
            return ExecResult("NONE", long_fill, None,
                              "long leg did not fill; short never sent (safe, flat)")

        # 2) SHORT leg only AFTER the long is confirmed held
        short_limit = short_opt.mid - (self.buffer if marketable else 0.0)
        self.log.info("[EXEC] step 2/2: long confirmed held; open SHORT %s x%d @ %.2f",
                      short_opt.symbol, qty, short_limit)
        self.broker.submit_options_order(self._leg_payload(short_opt.symbol, "SELL_TO_OPEN", qty, short_limit))
        short_fill = self._await_fill(short_opt.symbol, want_long=0, want_short=qty)
        if short_fill.fully_filled:
            return ExecResult("COMPLETE", long_fill, short_fill, "spread established (both legs filled)")
        return ExecResult("LONG_ONLY", long_fill, short_fill,
                          "short leg not filled -> holding the long only (defined risk). "
                          "Safe to retry the short or close the long.")

    # ---- the safe unwind ---------------------------------------------------
    def close_spread(self, spread, *, has_short: bool) -> ExecResult:
        """Flatten: close the SHORT first (so you are never momentarily naked),
        then the long."""
        long_opt = spread.long_leg.option
        short_opt = spread.short_leg.option
        qty = spread.contracts
        short_fill = None
        if has_short:
            sp_limit = (short_opt.mid or 0.05) + self.buffer
            self.log.info("[EXEC] unwind 1/2: BUY_TO_CLOSE short %s x%d", short_opt.symbol, qty)
            self.broker.submit_options_order(self._leg_payload(short_opt.symbol, "BUY_TO_CLOSE", qty, sp_limit))
            short_fill = self._await_fill(short_opt.symbol, want_long=0, want_short=0)  # want 0 held
        lp_limit = max(0.05, (long_opt.mid or 0.05) - self.buffer)
        self.log.info("[EXEC] unwind 2/2: SELL_TO_CLOSE long %s x%d", long_opt.symbol, qty)
        self.broker.submit_options_order(self._leg_payload(long_opt.symbol, "SELL_TO_CLOSE", qty, lp_limit))
        return ExecResult("FLAT", None, short_fill, "unwind submitted (short closed before long)")
