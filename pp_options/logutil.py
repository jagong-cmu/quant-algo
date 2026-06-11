"""Timestamped, dual (file + console) logging.

Every decision the engine makes -- entry, skip, block reason, and the exact
order payload -- is written to a timestamped file under logs/ and echoed to the
console.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from . import config

_LOGGER_NAME = "pp_options"


def setup_logging(tag: str = "run") -> tuple[logging.Logger, str]:
    """Create a logger writing to logs/pp_options_<tag>_<UTC timestamp>.log."""
    os.makedirs(config.LOG_DIR, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%SZ")
    path = os.path.join(config.LOG_DIR, f"pp_options_{tag}_{stamp}.log")

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()  # avoid duplicate handlers on re-run within a process
    logger.propagate = False

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    logger.info("Logging to %s", path)
    return logger, path


def get_logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)


def log_payload(logger: logging.Logger, label: str, payload: dict) -> None:
    """Log a structured order payload as pretty JSON so it is easy to review."""
    logger.info("%s\n%s", label, json.dumps(payload, indent=2, default=str))
