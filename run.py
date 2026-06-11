#!/usr/bin/env python3
"""Entry point for the PentPort premium-selling system.

Default behavior is a DRY-RUN: it runs the full strategy + guardrail logic and
logs the exact orders it WOULD submit, without calling trade_options(). Live
submission only happens when LIVE_TRADING is flipped to True in
pp_options/config.py (and even then every order must pass all guardrails).

Usage:
    python run.py --mock     # offline demo with synthetic data, no API key
    python run.py            # dry-run against live PentPort + stooq data
                             # (requires PENTPORT_API_KEY; still does NOT submit
                             #  unless config.LIVE_TRADING is True)

There is intentionally no --live flag. Going live is a deliberate code edit in
config.py, not a CLI switch.
"""

from __future__ import annotations

import argparse
import sys

from pp_options import config
from pp_options.engine import run_once
from pp_options.logutil import setup_logging


def main() -> int:
    parser = argparse.ArgumentParser(description="PentPort defined-risk premium-selling dry-run")
    parser.add_argument("--mock", action="store_true",
                        help="use synthetic offline data (no API key / network)")
    args = parser.parse_args()

    logger, path = setup_logging(tag="mock" if args.mock else "dryrun")
    if config.LIVE_TRADING:
        logger.warning("LIVE_TRADING is TRUE -- real orders will be submitted if they pass guardrails.")
    else:
        logger.info("LIVE_TRADING is False -- orders will be logged, not submitted.")

    if args.mock:
        from pp_options.mockdata import MockDataSource
        ds = MockDataSource()
    else:
        from pp_options.live import LiveDataSource
        try:
            ds = LiveDataSource()
        except Exception as e:
            logger.error("Could not initialize live data source: %s", e)
            logger.error("Set PENTPORT_API_KEY, or use --mock for an offline demo.")
            return 2

    try:
        run_once(ds)
    except Exception as e:
        logger.exception("Run failed: %s", e)
        return 1
    logger.info("Done. Full log: %s", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
