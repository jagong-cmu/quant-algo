#!/usr/bin/env python3
"""Entry point for the autonomous supervised runner.

SAFE BY DEFAULT: paper/dry-run unless --live. With --live it enables real
submission only inside market hours and routes every entry through the
protective-leg-first executor with the daily-loss kill switch active.

Usage:
    python autorun.py                 # one paper cycle (dry-run, nothing submitted)
    python autorun.py --loop          # run the session in paper, polling
    python autorun.py --live --loop   # REAL autonomous session (market hours only)
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time

from pp_options import config
from pp_options.envload import load_env
from pp_options.logutil import setup_logging
from pp_options.runner import AutonomousRunner


def main() -> int:
    ap = argparse.ArgumentParser(description="Autonomous supervised runner")
    ap.add_argument("--live", action="store_true", help="enable REAL submission (market hours only)")
    ap.add_argument("--loop", action="store_true", help="run the session loop until close")
    ap.add_argument("--interval", type=int, default=300, help="poll seconds")
    ap.add_argument("--force", action="store_true", help="skip the market-hours guard (live)")
    args = ap.parse_args()

    load_env()
    log, path = setup_logging(tag="autorun")

    if args.live:
        now = dt.datetime.now(dt.timezone.utc)
        if not AutonomousRunner.market_open(now) and not args.force:
            log.error("Market closed (%s UTC). Refusing --live. Use --force only if you mean it.",
                      now.strftime("%a %H:%M"))
            return 2
        config.LIVE_TRADING = True
        log.warning("AUTONOMOUS LIVE TRADING ENABLED. Kill switch at -%.0f%%/day. Ctrl-C to stop.",
                    __import__("pp_options.runner", fromlist=["DAILY_LOSS_HALT_PCT"]).DAILY_LOSS_HALT_PCT * 100)
    else:
        log.info("PAPER mode (LIVE_TRADING=False) -- entries logged, not submitted.")

    from pp_options.broker import Broker
    runner = AutonomousRunner(Broker(), paper=not args.live)

    try:
        if args.loop:
            while AutonomousRunner.market_open():
                runner.run_cycle()
                time.sleep(args.interval)
            log.info("Session closed -- runner idle. Log: %s", path)
        else:
            runner.run_cycle()
            log.info("Single cycle done. Log: %s", path)
    except KeyboardInterrupt:
        log.warning("Interrupted by user -- stopping. Open positions persist in the ledger.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
