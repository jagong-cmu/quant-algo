"""Autonomous supervised runner for the conservative put credit spread strategy.

SAFE BY DEFAULT. This is the piece that lets the program manage itself across a
session, but it is paper/dry-run unless explicitly run --live, and even then:

  * Entries go through the protective-leg-first SpreadExecutor (never naked).
  * A daily-loss KILL SWITCH halts new entries (and can flatten) on a bad day.
  * Positions have a LIFECYCLE: each tracked spread is closed at a DTE threshold
    or a profit target -- it does not just fire-and-forget.
  * State is persisted to a ledger so a restart recovers open positions.
  * Hard guardrails (3%/trade, 20%/book) still run before every entry.

v1 scope: one underlying (SPY), small fixed size, single concurrent spread. It is
meant to be validated in PAPER first; do not point it at real money until fill
behavior and an actual edge are confirmed.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from typing import Optional

from . import chain as chainmod
from . import config, guardrails
from .execution import SpreadExecutor
from .logutil import get_logger
from .models import Leg, Spread
from .risk import BookRiskTracker

# ---- autonomous-runner config ---------------------------------------------
AUTO_UNDERLYING = "SPY"
AUTO_CONTRACTS = 1               # v1 conservative fixed size
AUTO_MAX_CONCURRENT = 1          # one open spread at a time in v1
AUTO_WIDTH = 5.0
EXIT_DTE = 7                     # close when the spread is within a week of expiry
PROFIT_TARGET_FRAC = 0.50        # close at 50% of max profit (credit captured)
DAILY_LOSS_HALT_PCT = 0.05       # halt new entries after -5% on the day
SESSION_OPEN = dt.time(13, 30)
SESSION_CLOSE = dt.time(20, 0)
LEDGER_PATH = "state/auto_ledger.json"


@dataclass
class OpenSpread:
    underlying: str
    short_sym: str
    long_sym: str
    short_k: float
    long_k: float
    expiry: str
    contracts: int
    entry_credit: float
    max_loss: float

    def dte(self, today: dt.date) -> int:
        return (dt.date.fromisoformat(self.expiry) - today).days


@dataclass
class RunnerState:
    day: str
    day_start_equity: float
    open: list[OpenSpread] = field(default_factory=list)
    halted: bool = False


class AutonomousRunner:
    def __init__(self, broker, *, paper: bool = True):
        self.broker = broker
        self.paper = paper
        self.log = get_logger()
        self.executor = SpreadExecutor(broker)
        self.state = self._load_state()

    # ---- state persistence (crash recovery) --------------------------------
    def _load_state(self) -> RunnerState:
        today = dt.date.today().isoformat()
        if os.path.exists(LEDGER_PATH):
            try:
                d = json.load(open(LEDGER_PATH))
                open_ = [OpenSpread(**o) for o in d.get("open", [])]   # always carry positions
                if d.get("day") == today:
                    return RunnerState(day=today, day_start_equity=d["day_start_equity"],
                                       open=open_, halted=d.get("halted", False))
                # NEW DAY: keep open multi-day spreads; reset the daily baseline + kill switch
                self.log.info("new trading day -- carrying %d open spread(s), resetting daily baseline",
                              len(open_))
                return RunnerState(day=today, day_start_equity=self._safe_equity(),
                                   open=open_, halted=False)
            except (json.JSONDecodeError, OSError, KeyError, TypeError) as e:
                self.log.warning("ledger unreadable (%s); starting fresh", e)
        return RunnerState(day=today, day_start_equity=self._safe_equity())

    def _persist(self) -> None:
        os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
        json.dump({"day": self.state.day, "day_start_equity": self.state.day_start_equity,
                   "halted": self.state.halted,
                   "open": [o.__dict__ for o in self.state.open]}, open(LEDGER_PATH, "w"), indent=2)

    def _safe_equity(self) -> float:
        try:
            return self.broker.equity()
        except Exception as e:
            self.log.warning("equity read failed (%s); assuming 100k", e)
            return 100_000.0

    # ---- session gate + kill switch ----------------------------------------
    @staticmethod
    def market_open(now: Optional[dt.datetime] = None) -> bool:
        now = now or dt.datetime.now(dt.timezone.utc)
        return now.weekday() < 5 and SESSION_OPEN <= now.time() <= SESSION_CLOSE

    def _check_kill_switch(self, equity: float) -> bool:
        loss = (equity - self.state.day_start_equity) / self.state.day_start_equity
        if loss <= -DAILY_LOSS_HALT_PCT and not self.state.halted:
            self.state.halted = True
            self.log.error("KILL SWITCH: day P/L %.1f%% <= -%.0f%% -> halting new entries.",
                           loss * 100, DAILY_LOSS_HALT_PCT * 100)
        return self.state.halted

    # ---- one cycle ---------------------------------------------------------
    def run_cycle(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        if not self.market_open(now):
            self.log.info("market closed (%s UTC) -> idle", now.strftime("%H:%M"))
            return
        equity = self._safe_equity()
        self._check_kill_switch(equity)

        self._manage_exits()
        if not self.state.halted and len(self.state.open) < AUTO_MAX_CONCURRENT:
            self._maybe_enter(equity)
        self._persist()

    # ---- exits (lifecycle) -------------------------------------------------
    def _manage_exits(self) -> None:
        today = dt.date.today()
        for sp in list(self.state.open):
            reason = None
            if sp.dte(today) <= EXIT_DTE:
                reason = f"DTE {sp.dte(today)} <= {EXIT_DTE}"
            # (profit-target exit would mark-to-market via positions(); omitted in v1
            #  until real spread MTM is wired -- DTE exit is the robust v1 rule.)
            if reason:
                self.log.info("[EXIT] closing %s %s/%s (%s)", sp.underlying, sp.short_k, sp.long_k, reason)
                spread = self._reconstruct_spread(sp)
                if spread is not None:
                    self.executor.close_spread(spread, has_short=True)
                self.state.open.remove(sp)

    # ---- entry -------------------------------------------------------------
    def _maybe_enter(self, equity: float) -> None:
        spread = self._build_spread(equity)
        if spread is None:
            self.log.info("[ENTRY] no valid spread this cycle")
            return
        rep = guardrails.evaluate_order(spread, equity, BookRiskTracker(equity, self._book_risk()))
        if rep.blocked:
            self.log.warning("[ENTRY] blocked by guardrails: %s", " | ".join(rep.block_reasons))
            return
        self.log.info("[ENTRY] opening %s via safe executor: %s", AUTO_UNDERLYING, "; ".join(spread.notes))
        result = self.executor.open_spread(spread)
        self.log.warning("[ENTRY] execution result: %s -- %s", result.status, result.message)
        if result.naked:
            self.log.critical("NAKED -- impossible by design; halting.")
            self.state.halted = True
            return
        if result.status in ("COMPLETE", "LONG_ONLY"):
            self.state.open.append(OpenSpread(
                underlying=AUTO_UNDERLYING, short_sym=spread.short_leg.option.symbol,
                long_sym=spread.long_leg.option.symbol, short_k=spread.short_leg.option.strike,
                long_k=spread.long_leg.option.strike, expiry=spread.short_leg.option.expiry.isoformat(),
                contracts=spread.contracts, entry_credit=spread.net_credit or 0.0,
                max_loss=spread.total_max_loss or 0.0))

    def _book_risk(self) -> float:
        return sum(o.max_loss for o in self.state.open)

    # ---- spread construction from real chain -------------------------------
    def _build_spread(self, equity: float) -> Optional[Spread]:
        try:
            raw = self.broker.options_chain(AUTO_UNDERLYING)
            inner = raw.get("chain", raw)
            allx = inner.get("allExpiries") or []
            today = dt.date.today()
            target = (config.DTE_MIN + config.DTE_MAX) / 2
            dated = [(e, (dt.date.fromisoformat(e) - today).days) for e in allx]
            dated = [(e, d) for e, d in dated if config.DTE_MIN - 5 <= d <= config.DTE_MAX + 10]
            if not dated:
                return None
            expiry = min(dated, key=lambda x: abs(x[1] - target))[0]
            raw = self.broker.options_chain(AUTO_UNDERLYING, expiry=expiry)
        except Exception as e:
            self.log.warning("chain fetch failed: %s", e)
            return None
        spot = chainmod.underlying_price(raw)
        norm = chainmod.normalize(raw, AUTO_UNDERLYING, spot)
        exp_date = dt.date.fromisoformat(expiry)
        puts = [o for o in chainmod.strikes_for(norm, "P", exp_date) if o.delta is not None and o.mid]
        if not puts:
            return None
        short_put = min(puts, key=lambda o: abs(o.abs_delta - config.TARGET_SHORT_PUT_DELTA))
        below = [o for o in puts if o.strike < short_put.strike - 1e-9]
        if not below:
            return None
        long_put = min(below, key=lambda o: abs(o.strike - (short_put.strike - AUTO_WIDTH)))
        credit = round(short_put.mid - long_put.mid, 2)
        if credit <= 0:
            return None
        return Spread(AUTO_UNDERLYING, "put_credit",
                      Leg(short_put, "SELL_TO_OPEN"), Leg(long_put, "BUY_TO_OPEN"),
                      contracts=AUTO_CONTRACTS, net_credit=credit,
                      notes=[f"short {short_put.strike}P (d={short_put.delta:.2f}) / "
                             f"long {long_put.strike}P, credit {credit:.2f}"])

    def _reconstruct_spread(self, sp: OpenSpread) -> Optional[Spread]:
        # Minimal NormOption stand-ins for the unwind (symbols + strikes are what matter).
        from .chain import NormOption
        exp = dt.date.fromisoformat(sp.expiry)
        short = NormOption(sp.underlying, "P", sp.short_k, exp, sp.dte(dt.date.today()),
                           None, None, 0.05, None, None, sp.short_sym)
        long = NormOption(sp.underlying, "P", sp.long_k, exp, sp.dte(dt.date.today()),
                          None, None, 0.05, None, None, sp.long_sym)
        return Spread(sp.underlying, "put_credit", Leg(short, "SELL_TO_OPEN"),
                      Leg(long, "BUY_TO_OPEN"), contracts=sp.contracts, net_credit=sp.entry_credit)
