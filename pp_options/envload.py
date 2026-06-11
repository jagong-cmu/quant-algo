"""Minimal, zero-dependency .env loader.

The pentport SDK reads PENTPORT_API_KEY from the process environment, so we load
a local .env file into os.environ at startup. Existing environment variables
always win (so an explicitly exported key is never overridden by the file).
"""

from __future__ import annotations

import os


def load_env(path: str = ".env") -> bool:
    """Load KEY=VALUE lines from `path` into os.environ. Returns True if loaded."""
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    return True
