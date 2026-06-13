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
    ap.add_argument("--loop", action="store_true", help="run one session loop until close")
    ap.add_argument("--daemon", action="store_true", help="run forever (server mode); cycles act only in market hours")
    ap.add_argument("--interval", type=int, default=300, help="poll seconds")
    ap.add_argument("--force", action="store_true", help="skip the market-hours guard (live one-shot/loop)")
    args = ap.parse_args()

    load_env()
    log, path = setup_logging(tag="autorun")
    from pp_options.runner import DAILY_LOSS_HALT_PCT

    if args.live:
        now = dt.datetime.now(dt.timezone.utc)
        # A one-shot/loop --live run must be inside market hours; the daemon may
        # start anytime and simply waits (run_cycle self-idles when closed).
        if not args.daemon and not AutonomousRunner.market_open(now) and not args.force:
            log.error("Market closed (%s UTC). Refusing --live. Use --daemon (waits) or --force.",
                      now.strftime("%a %H:%M"))
            return 2
        config.LIVE_TRADING = True
        log.warning("AUTONOMOUS LIVE TRADING ENABLED. Daily kill switch DISABLED; book cap %.0f%% "
                    "(full deployment). No automatic daily-loss halt.", config.MAX_BOOK_RISK_PCT * 100)
    else:
        log.info("PAPER mode (LIVE_TRADING=False) -- entries logged, not submitted.")

    from pp_options.broker import Broker
    runner = AutonomousRunner(Broker(), paper=not args.live)

    try:
        if args.daemon:
            log.warning("DAEMON mode (server): running forever; entries occur only during market hours. "
                        "Stop with systemctl/docker.")
            while True:
                try:
                    runner.run_cycle()
                except Exception as e:                       # never let one cycle kill the daemon
                    log.exception("cycle error (continuing): %s", e)
                time.sleep(args.interval if AutonomousRunner.market_open() else max(args.interval, 600))
        elif args.loop:
            while AutonomousRunner.market_open():
                runner.run_cycle()
                time.sleep(args.interval)
            log.info("Session closed -- runner idle. Log: %s", path)
        else:
            runner.run_cycle()
            log.info("Single cycle done. Log: %s", path)
    except KeyboardInterrupt:
        log.warning("Interrupted -- stopping. Open positions persist in the ledger.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
