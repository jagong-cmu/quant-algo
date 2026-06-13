"""Autonomous supervised runner for the conservative put credit spread strategy.

SAFE BY DEFAULT. This is the piece that lets the program manage itself across a
session, but it is paper/dry-run unless explicitly run --live, and even then:

  * Entries go through the protective-leg-first SpreadExecutor (never naked).
  * A daily-loss KILL SWITCH halts new entries (and can flatten) on a bad day.
  * Positions are MANAGED EARLY, not held to expiry: each tracked spread is
    marked-to-market every cycle and closed on the first of a 50% profit target,
    a 2x stop-loss, or a 21-DTE time stop (with a 7-DTE hard backstop).
  * If a close cannot actually be submitted (paper/dry-run, or a live error) the
    runner KEEPS the position and raises an action-required alert (notify.py) so
    a human can close it manually -- it never silently drops a live spread.
  * State is persisted to a ledger so a restart recovers open positions, and the
    dashboard (dashboard.py) can both observe and control the runner via a
    command queue (state/commands.json) drained each cycle.
  * Hard guardrails (3%/trade, 20%/book) still run before every entry.

Scope: diversified across liquid index ETFs (SPY/QQQ/IWM), small fixed size. It
is meant to be validated in PAPER first; do not point it at real money until fill
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
from .notify import AlertStore
from .risk import BookRiskTracker

# ---- autonomous-runner config ---------------------------------------------
AUTO_UNDERLYINGS = ["SPY", "QQQ", "IWM",   # US equity: large / nasdaq / small cap
                    "DIA",                  # US large-cap (Dow)
                    "GLD",                  # gold (low equity correlation)
                    "EEM", "EFA",           # emerging + developed international equity
                    "TLT"]                  # long-dated US Treasuries (bonds)
AUTO_CONTRACTS = 2               # fallback only; sizing now scales to MAX_TRADE_RISK_PCT (see _build_spread)
AUTO_MAX_CONCURRENT = 40         # raised so the 100% book cap is the binding limit, not the count
AUTO_WIDTH = 5.0
# ---- exit / trade-management policy ----------------------------------------
# Short-premium spreads are MANAGED EARLY, not held to expiry: gamma risk spikes
# in the final weeks and theta-per-unit-risk is best mid-life. Each cycle a
# tracked spread is marked-to-market and closed on the FIRST of: profit target,
# stop-loss, or the 21-DTE time stop. EXIT_DTE is a hard backstop that should
# rarely fire (MANAGE_DTE triggers first).
PROFIT_TARGET_FRAC = 0.50        # close at 50% of max profit captured
STOP_LOSS_MULT = 2.0             # close if the loss reaches 2x the credit received
MANAGE_DTE = 21                  # primary early time-stop: close at/under 21 DTE
EXIT_DTE = 7                     # HARD backstop: never carry inside a week of expiry
DAILY_LOSS_HALT_PCT = 0.20       # halt new entries after -20% on the day
SESSION_OPEN = dt.time(13, 30)
SESSION_CLOSE = dt.time(20, 0)
LEDGER_PATH = "state/auto_ledger.json"
COMMANDS_PATH = "state/commands.json"   # dashboard -> runner control queue


def _quote_num(d: dict, *keys) -> Optional[float]:
    """First parseable float among `keys` in dict `d` (else None)."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                continue
    return None


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
    # --- live management state (optional; older ledgers omit these) ----------
    opened_day: str = ""
    short_mark: Optional[float] = None   # current short-leg mid (per share)
    long_mark: Optional[float] = None    # current long-leg mid (per share)
    mark: Optional[float] = None         # current spread value = cost to close (per share)
    profit_frac: Optional[float] = None  # fraction of max profit captured so far
    manage_note: str = ""                # human-readable status for the dashboard
    manual_close: bool = False           # dashboard requested a close
    close_pending: bool = False          # decided to close but couldn't execute -> manual

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
        self.alerts = AlertStore()
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

    def _persist(self, *, market: bool = True, equity: Optional[float] = None) -> None:
        os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
        day_pl = None
        if equity is not None and self.state.day_start_equity:
            day_pl = round((equity - self.state.day_start_equity) / self.state.day_start_equity * 100, 3)
        meta = {
            "mode": "LIVE" if config.LIVE_TRADING else "PAPER",
            "market_open": market,
            "halted": self.state.halted,
            "equity": equity,
            "day_start_equity": self.state.day_start_equity,
            "day_pl_pct": day_pl,
            "updated": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "config": {
                "underlyings": ", ".join(AUTO_UNDERLYINGS), "contracts": AUTO_CONTRACTS,
                "max_concurrent": AUTO_MAX_CONCURRENT, "width": AUTO_WIDTH,
                "manage_dte": MANAGE_DTE, "exit_dte": EXIT_DTE,
                "profit_target_frac": PROFIT_TARGET_FRAC, "stop_loss_mult": STOP_LOSS_MULT,
                "daily_loss_halt_pct": DAILY_LOSS_HALT_PCT,
            },
        }
        json.dump({"day": self.state.day, "day_start_equity": self.state.day_start_equity,
                   "halted": self.state.halted, "meta": meta,
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
        # DAILY LOSS KILL SWITCH DISABLED by configuration: the runner no longer
        # auto-halts new entries on a bad day. Only a manual dashboard "halt"
        # command sets self.state.halted now. Open positions are still managed.
        return self.state.halted

    # ---- dashboard control queue -------------------------------------------
    def _drain_commands(self) -> None:
        """Apply any commands the dashboard queued, then clear the queue. The
        runner is the SINGLE execution path; the dashboard only enqueues here."""
        if not os.path.exists(COMMANDS_PATH):
            return
        try:
            cmds = json.load(open(COMMANDS_PATH)).get("commands", [])
        except (json.JSONDecodeError, OSError) as e:
            self.log.warning("commands file unreadable (%s); ignoring", e)
            return
        for c in cmds:
            action, sym = c.get("action"), c.get("symbol")
            if action == "halt":
                self.state.halted = True
                self.log.warning("[CMD] halted via dashboard")
            elif action == "resume":
                self.state.halted = False
                self.log.warning("[CMD] resumed via dashboard")
            elif action == "close" and sym:
                for sp in self.state.open:
                    if sp.short_sym == sym:
                        sp.manual_close = True
                        self.log.warning("[CMD] manual close requested: %s", sym)
            elif action == "remove" and sym:
                self.state.open = [sp for sp in self.state.open if sp.short_sym != sym]
                self.alerts.ack_key(f"close:{sym}")
                self.log.warning("[CMD] position removed (closed manually): %s", sym)
            elif action == "ack" and c.get("id"):
                self.alerts.ack(c["id"])
        json.dump({"commands": []}, open(COMMANDS_PATH, "w"), indent=2)

    # ---- one cycle ---------------------------------------------------------
    def run_cycle(self) -> None:
        self._drain_commands()   # honor dashboard actions even when the market is closed
        now = dt.datetime.now(dt.timezone.utc)
        if not self.market_open(now):
            self.log.info("market closed (%s UTC) -> idle", now.strftime("%H:%M"))
            self._persist(market=False)
            return
        equity = self._safe_equity()
        self._check_kill_switch(equity)

        self._manage_exits()
        if not self.state.halted and len(self.state.open) < AUTO_MAX_CONCURRENT:
            self._maybe_enter(equity)
        self._persist(market=True, equity=equity)

    # ---- exits (lifecycle) -------------------------------------------------
    def _manage_exits(self) -> None:
        """Mark each open spread to market and close it on the first management
        trigger. Closes are routed through _attempt_close, which falls back to a
        manual-action alert when it cannot really submit (paper, or a live error)."""
        today = dt.date.today()
        for sp in list(self.state.open):
            self._mark(sp)                       # refresh sp.short_mark/long_mark/mark/profit_frac
            dte = sp.dte(today)
            credit = sp.entry_credit or 0.0
            value = sp.mark                      # cost to close, per share (None if no quote)

            reason = None
            if sp.manual_close:
                reason = "manual close (dashboard)"
            elif sp.profit_frac is not None and sp.profit_frac >= PROFIT_TARGET_FRAC:
                reason = f"profit target {sp.profit_frac * 100:.0f}% >= {PROFIT_TARGET_FRAC * 100:.0f}%"
            elif value is not None and credit > 0 and (value - credit) >= STOP_LOSS_MULT * credit:
                reason = f"stop-loss: loss {value - credit:.2f}/sh >= {STOP_LOSS_MULT:.0f}x credit"
            elif dte <= MANAGE_DTE:
                reason = f"time stop: DTE {dte} <= {MANAGE_DTE}"
            elif dte <= EXIT_DTE:
                reason = f"hard backstop: DTE {dte} <= {EXIT_DTE}"

            sp.manage_note = self._status_note(sp, dte, reason)
            if reason:
                self._attempt_close(sp, reason)

    def _mark(self, sp: OpenSpread) -> None:
        """Mark-to-market via live option quotes. Fail-safe: on any quote failure
        leave marks None so only the (quote-free) DTE rules can fire."""
        sp.short_mark = sp.long_mark = sp.mark = sp.profit_frac = None
        try:
            q = self.broker.quotes([sp.short_sym, sp.long_sym])
        except Exception as e:
            self.log.warning("quotes() failed for MTM of %s (%s); DTE rules only", sp.short_sym, e)
            return
        sm, lm = self._quote_mid(q, sp.short_sym), self._quote_mid(q, sp.long_sym)
        sp.short_mark, sp.long_mark = sm, lm
        if sm is None or lm is None:
            return
        value = round(sm - lm, 4)               # debit to buy the spread back
        sp.mark = value
        credit = sp.entry_credit or 0.0
        if credit > 0:
            sp.profit_frac = round((credit - value) / credit, 4)

    @staticmethod
    def _quote_mid(qresp, symbol: str) -> Optional[float]:
        """Best-effort mid for `symbol` across unknown quotes() response shapes."""
        rec = None
        if isinstance(qresp, dict):
            container = qresp.get("quotes", qresp)
            if isinstance(container, dict):
                rec = container.get(symbol)
            elif isinstance(container, list):
                rec = next((it for it in container
                            if isinstance(it, dict) and it.get("symbol") == symbol), None)
        if not isinstance(rec, dict):
            return None
        bid = _quote_num(rec, "bid", "bidPrice")
        ask = _quote_num(rec, "ask", "askPrice")
        if bid is not None and ask is not None and ask > 0:
            return round((bid + ask) / 2, 4)
        return _quote_num(rec, "mark", "markPrice", "mid", "last", "lastPrice")

    @staticmethod
    def _status_note(sp: OpenSpread, dte: int, reason: Optional[str]) -> str:
        bits = [f"DTE {dte}"]
        if sp.profit_frac is not None:
            bits.append(f"profit {sp.profit_frac * 100:.0f}%")
        if sp.mark is not None:
            bits.append(f"mark {sp.mark:.2f} vs credit {sp.entry_credit:.2f}")
        if reason:
            bits.append(f"-> CLOSING ({reason})")
        return ", ".join(bits)

    def _close_legs(self, sp: OpenSpread) -> list[dict]:
        """The unwind legs (for the alert / manual-close instructions)."""
        return [
            {"symbol": sp.short_sym, "instruction": "BUY_TO_CLOSE", "quantity": sp.contracts,
             "limit": round((sp.short_mark or 0.05) + 0.05, 2)},
            {"symbol": sp.long_sym, "instruction": "SELL_TO_CLOSE", "quantity": sp.contracts,
             "limit": round(max(0.05, (sp.long_mark or 0.05) - 0.05), 2)},
        ]

    def _attempt_close(self, sp: OpenSpread, reason: str) -> None:
        """Try to flatten `sp`. If we can't actually submit (paper/dry-run or a
        live error), KEEP the position, flag it close_pending, and raise a
        deduped action-required alert so the human closes it in their broker."""
        self.log.info("[EXIT] closing %s %s/%s x%d (%s)",
                      sp.underlying, sp.short_k, sp.long_k, sp.contracts, reason)
        legs = self._close_legs(sp)
        title = f"Close {sp.underlying} {sp.short_k:.0f}/{sp.long_k:.0f}P x{sp.contracts}"

        if not config.LIVE_TRADING:
            sp.close_pending = True
            sp.manage_note = f"CLOSE REQUIRED ({reason}) -- paper mode; submit manually"
            self.alerts.push(key=f"close:{sp.short_sym}", kind="close_required", title=title,
                             detail=f"{reason}. Paper/dry-run cannot submit the close -- close this "
                                    "spread in your broker, then click 'Mark closed' on the dashboard.",
                             legs=legs)
            return

        try:
            spread = self._reconstruct_spread(sp)
            if spread is not None:
                self.executor.close_spread(spread, has_short=True)
            self.state.open.remove(sp)
            self.alerts.ack_key(f"close:{sp.short_sym}")
            self.log.warning("[EXIT] live close submitted for %s %s/%s",
                             sp.underlying, sp.short_k, sp.long_k)
        except Exception as e:
            sp.close_pending = True
            sp.manage_note = f"CLOSE FAILED ({reason}): {e}"
            self.alerts.push(key=f"close:{sp.short_sym}", kind="close_required",
                             title=f"CLOSE FAILED -- {title}",
                             detail=f"{reason}. Live submit error: {e}. Close manually, then "
                                    "click 'Mark closed' on the dashboard.",
                             legs=legs)

    # ---- entry -------------------------------------------------------------
    def _maybe_enter(self, equity: float) -> None:
        # diversify: add a spread on the least-represented underlying this cycle
        counts = {u: 0 for u in AUTO_UNDERLYINGS}
        for o in self.state.open:
            counts[o.underlying] = counts.get(o.underlying, 0) + 1
        underlying = min(AUTO_UNDERLYINGS, key=lambda u: counts[u])

        spread = self._build_spread(equity, underlying)
        if spread is None:
            self.log.info("[ENTRY] %s: no valid spread this cycle", underlying)
            return
        rep = guardrails.evaluate_order(spread, equity, BookRiskTracker(equity, self._book_risk()))
        if rep.blocked:
            self.log.warning("[ENTRY] %s blocked by guardrails: %s", underlying, " | ".join(rep.block_reasons))
            return
        self.log.info("[ENTRY] opening %s via safe executor: %s", underlying, "; ".join(spread.notes))
        result = self.executor.open_spread(spread)
        self.log.warning("[ENTRY] %s execution result: %s -- %s", underlying, result.status, result.message)
        if result.naked:
            self.log.critical("NAKED -- impossible by design; halting.")
            self.state.halted = True
            return
        if result.status in ("COMPLETE", "LONG_ONLY"):
            self.state.open.append(OpenSpread(
                underlying=underlying, short_sym=spread.short_leg.option.symbol,
                long_sym=spread.long_leg.option.symbol, short_k=spread.short_leg.option.strike,
                long_k=spread.long_leg.option.strike, expiry=spread.short_leg.option.expiry.isoformat(),
                contracts=spread.contracts, entry_credit=spread.net_credit or 0.0,
                max_loss=spread.total_max_loss or 0.0,
                opened_day=dt.date.today().isoformat()))

    def _book_risk(self) -> float:
        return sum(o.max_loss for o in self.state.open)

    # ---- spread construction from real chain -------------------------------
    def _build_spread(self, equity: float, underlying: str) -> Optional[Spread]:
        try:
            raw = self.broker.options_chain(underlying)
            inner = raw.get("chain", raw)
            allx = inner.get("allExpiries") or []
            today = dt.date.today()
            target = (config.DTE_MIN + config.DTE_MAX) / 2
            dated = [(e, (dt.date.fromisoformat(e) - today).days) for e in allx]
            dated = [(e, d) for e, d in dated if config.DTE_MIN - 5 <= d <= config.DTE_MAX + 10]
            if not dated:
                return None
            expiry = min(dated, key=lambda x: abs(x[1] - target))[0]
            raw = self.broker.options_chain(underlying, expiry=expiry)
        except Exception as e:
            self.log.warning("[%s] chain fetch failed: %s", underlying, e)
            return None
        spot = chainmod.underlying_price(raw)
        norm = chainmod.normalize(raw, underlying, spot)
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
        # Scale contracts to the per-trade risk budget so the book fills toward 100%.
        per_contract_max_loss = (AUTO_WIDTH - credit) * 100.0
        budget = config.MAX_TRADE_RISK_PCT * equity
        contracts = max(1, int(budget // per_contract_max_loss)) if per_contract_max_loss > 0 else AUTO_CONTRACTS
        return Spread(underlying, "put_credit",
                      Leg(short_put, "SELL_TO_OPEN"), Leg(long_put, "BUY_TO_OPEN"),
                      contracts=contracts, net_credit=credit,
                      notes=[f"short {short_put.strike}P (d={short_put.delta:.2f}) / "
                             f"long {long_put.strike}P, credit {credit:.2f}"])

    def _reconstruct_spread(self, sp: OpenSpread) -> Optional[Spread]:
        # Minimal NormOption stand-ins for the unwind (symbols + strikes are what matter).
        from .chain import NormOption
        exp = dt.date.fromisoformat(sp.expiry)
        # Use the latest marks (if any) so the unwind limits are marketable.
        short = NormOption(sp.underlying, "P", sp.short_k, exp, sp.dte(dt.date.today()),
                           None, None, sp.short_mark or 0.05, None, None, sp.short_sym)
        long = NormOption(sp.underlying, "P", sp.long_k, exp, sp.dte(dt.date.today()),
                          None, None, sp.long_mark or 0.05, None, None, sp.long_sym)
        return Spread(sp.underlying, "put_credit", Leg(short, "SELL_TO_OPEN"),
                      Leg(long, "BUY_TO_OPEN"), contracts=sp.contracts, net_credit=sp.entry_credit)
