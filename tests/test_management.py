#!/usr/bin/env python3
"""Tests for early trade management + the can't-close notification fallback.

Proves the runner:
  * closes at the 50% profit target, the 2x stop-loss, and the 21-DTE time stop
  * in LIVE mode submits the unwind (short BUY_TO_CLOSE first) and drops the book
  * when it CANNOT submit (paper, or a live error) keeps the position, flags it
    close_pending, and raises a deduped 'close_required' alert

Run:  .venv/bin/python tests/test_management.py
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, ".")

from pp_options import config
from pp_options import runner as R
from pp_options.notify import AlertStore
from pp_options.runner import AutonomousRunner, OpenSpread

PASSED = FAILED = 0


def check(name, cond):
    global PASSED, FAILED
    if cond:
        PASSED += 1; print(f"  PASS  {name}")
    else:
        FAILED += 1; print(f"  FAIL  {name}")


class FakeBroker:
    """Quotes return a configurable mid per symbol; submit/positions support the
    live unwind path. `fail_submit` makes submission raise (live-error case)."""

    def __init__(self, marks: dict, fail_submit: bool = False):
        self.marks = marks
        self.fail_submit = fail_submit
        self.submitted: list = []
        self.log = logging.getLogger("test_mgmt")

    def quotes(self, syms):
        return {"quotes": {s: {"bid": self.marks[s] - 0.02, "ask": self.marks[s] + 0.02}
                           for s in syms if s in self.marks}}

    def positions(self):
        return {"positions": []}   # nothing held -> close_spread's want=0 await returns at once

    def submit_options_order(self, payload):
        if self.fail_submit:
            raise RuntimeError("simulated API rejection")
        self.submitted.append((payload["legs"][0]["symbol"], payload["legs"][0]["instruction"]))
        return {"ok": True}

    def equity(self):
        return 100_000.0


def _runner(broker, live: bool):
    """Build a runner with isolated state/alerts/commands files."""
    tmp = tempfile.mkdtemp()
    R.LEDGER_PATH = os.path.join(tmp, "ledger.json")
    R.COMMANDS_PATH = os.path.join(tmp, "commands.json")
    config.LIVE_TRADING = live
    run = AutonomousRunner(broker, paper=not live)
    run.alerts = AlertStore(os.path.join(tmp, "alerts.json"))
    return run


def _spread(short_sym="SPY260717P00720000", long_sym="SPY260717P00715000",
            credit=1.20, dte=40, contracts=1):
    exp = (date.today() + timedelta(days=dte)).isoformat()
    return OpenSpread(underlying="SPY", short_sym=short_sym, long_sym=long_sym,
                      short_k=720.0, long_k=715.0, expiry=exp, contracts=contracts,
                      entry_credit=credit, max_loss=(5.0 - credit) * 100)


SHORT = "SPY260717P00720000"
LONG = "SPY260717P00715000"


def test_profit_target_live_closes():
    # credit 1.20, current spread value 0.30 -> captured 75% >= 50% target
    b = FakeBroker({SHORT: 0.40, LONG: 0.10})
    run = _runner(b, live=True)
    run.state.open = [_spread(credit=1.20, dte=40)]
    run._manage_exits()
    check("profit target removes the position", len(run.state.open) == 0)
    check("unwind submitted SHORT first (BUY_TO_CLOSE)",
          b.submitted and b.submitted[0] == (SHORT, "BUY_TO_CLOSE"))
    check("then long SELL_TO_CLOSE", (LONG, "SELL_TO_CLOSE") in b.submitted)


def test_time_stop():
    # no profit (value ~ credit), but DTE 18 <= 21 -> time stop fires
    b = FakeBroker({SHORT: 1.30, LONG: 0.10})  # value 1.20 == credit -> 0% profit
    run = _runner(b, live=True)
    run.state.open = [_spread(credit=1.20, dte=18)]
    run._manage_exits()
    check("21-DTE time stop closes the position", len(run.state.open) == 0)


def test_stop_loss():
    # credit 1.00, value 3.10 -> loss 2.10/sh >= 2x credit -> stop-loss
    b = FakeBroker({SHORT: 3.20, LONG: 0.10})
    run = _runner(b, live=True)
    run.state.open = [_spread(credit=1.00, dte=40)]
    run._manage_exits()
    check("2x stop-loss closes the position", len(run.state.open) == 0)


def test_no_trigger_holds():
    # mid-life, modest profit (40% < 50%), DTE 40 > 21 -> hold
    b = FakeBroker({SHORT: 0.80, LONG: 0.08})  # value 0.72, credit 1.20 -> 40%
    run = _runner(b, live=True)
    run.state.open = [_spread(credit=1.20, dte=40)]
    run._manage_exits()
    check("nothing fires -> position held", len(run.state.open) == 1)
    check("profit_frac marked (~40%)", abs(run.state.open[0].profit_frac - 0.40) < 0.02)


def test_paper_notifies_instead_of_closing():
    b = FakeBroker({SHORT: 0.40, LONG: 0.10})   # profit target hit
    run = _runner(b, live=False)                # PAPER
    run.state.open = [_spread(credit=1.20, dte=40)]
    run._manage_exits()
    check("paper keeps the position (can't really close)", len(run.state.open) == 1)
    check("position flagged close_pending", run.state.open[0].close_pending is True)
    check("nothing submitted to the broker", b.submitted == [])
    alerts = run.alerts.list()
    check("a close_required alert was raised",
          any(a["kind"] == "close_required" and not a["acknowledged"] for a in alerts))
    check("alert carries the unwind legs", bool(alerts and alerts[0]["legs"]))
    # re-running the same cycle must NOT spawn a duplicate alert (dedup by key)
    run._manage_exits()
    check("alert is deduped across cycles", len(run.alerts.list()) == 1)


def test_live_submit_failure_notifies():
    b = FakeBroker({SHORT: 0.40, LONG: 0.10}, fail_submit=True)
    run = _runner(b, live=True)
    run.state.open = [_spread(credit=1.20, dte=40)]
    run._manage_exits()
    check("live submit error keeps the position", len(run.state.open) == 1)
    check("position flagged close_pending on failure", run.state.open[0].close_pending is True)
    check("failure raised a close_required alert",
          any(a["kind"] == "close_required" for a in run.alerts.list()))


def main():
    tests = [test_profit_target_live_closes, test_time_stop, test_stop_loss,
             test_no_trigger_holds, test_paper_notifies_instead_of_closing,
             test_live_submit_failure_notifies]
    for fn in tests:
        print(f"\n{fn.__name__}:")
        try:
            fn()
        except Exception as e:  # a crash is a failure
            global FAILED; FAILED += 1
            print(f"  FAIL  {fn.__name__} raised {type(e).__name__}: {e}")
    print(f"\n{'=' * 50}\nPASSED={PASSED}  FAILED={FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
