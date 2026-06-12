"""Action-required alerts: a durable sink for things a human must act on.

The autonomous runner manages position lifecycle itself, but a few situations
need a human in the loop:

  * It DECIDED to close a spread but could not actually execute the close --
    because we are in paper/dry-run (nothing is really submitted) or a live
    submission errored. The spread is still open at the broker; YOU must close it.
  * The daily-loss kill switch tripped.

Alerts are written to state/alerts.json (read by the dashboard), logged at
CRITICAL, and -- if PP_ALERT_WEBHOOK is set -- POSTed to that URL (Slack/Discord/
email-bridge style payload). Alerts are de-duplicated by `key` so a condition
re-checked every cycle does not spawn a fresh alert each time; the outstanding
one is just refreshed until it is acknowledged.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime

import requests

from .logutil import get_logger

ALERTS_PATH = "state/alerts.json"


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Alert:
    id: str
    key: str            # dedup key, e.g. "close:SPY260717P00720000"
    kind: str           # "close_required" | "kill_switch" | "info"
    title: str
    detail: str
    legs: list = field(default_factory=list)   # [{symbol, instruction, quantity, limit}]
    created: str = ""
    updated: str = ""
    acknowledged: bool = False


class AlertStore:
    """File-backed alert list. Safe to construct in any process (runner reads/
    writes it; the dashboard reads it and acks via the runner command queue)."""

    def __init__(self, path: str = ALERTS_PATH):
        self.path = path
        self.log = get_logger()

    def _load(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        try:
            return json.load(open(self.path)).get("alerts", [])
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, alerts: list[dict]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        json.dump({"alerts": alerts}, open(self.path, "w"), indent=2)

    def push(self, *, key: str, kind: str, title: str, detail: str,
             legs: list | None = None) -> None:
        """Record an action-required alert (deduped by `key`)."""
        alerts = self._load()
        legs = legs or []
        for a in alerts:
            if a.get("key") == key and not a.get("acknowledged"):
                a["updated"], a["detail"], a["legs"] = _now(), detail, legs
                self._save(alerts)
                return  # already outstanding -> refresh, don't spam
        n = _now()
        new_id = f"{kind}-{len(alerts) + 1}-{n}"
        alerts.append(asdict(Alert(id=new_id, key=key, kind=kind, title=title,
                                   detail=detail, legs=legs, created=n, updated=n)))
        self._save(alerts)
        self.log.critical("[ALERT] %s -- %s", title, detail)
        self._webhook(title, detail)

    def list(self) -> list[dict]:
        return self._load()

    def ack(self, alert_id: str) -> bool:
        alerts = self._load()
        hit = False
        for a in alerts:
            if a.get("id") == alert_id:
                a["acknowledged"], a["updated"], hit = True, _now(), True
        self._save(alerts)
        return hit

    def ack_key(self, key: str) -> None:
        alerts = self._load()
        for a in alerts:
            if a.get("key") == key:
                a["acknowledged"], a["updated"] = True, _now()
        self._save(alerts)

    def _webhook(self, title: str, detail: str) -> None:
        url = os.environ.get("PP_ALERT_WEBHOOK")
        if not url:
            return
        try:
            requests.post(url, json={"text": f":rotating_light: {title}\n{detail}"}, timeout=5)
        except Exception as e:  # best-effort; never let alerting break the runner
            self.log.warning("alert webhook failed: %s", e)
