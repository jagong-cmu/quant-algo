"""Book-risk tracking.

Total open defined-risk is tracked via a local ledger of spreads THIS system
opened (state/book_ledger.json). We also attempt to read positions() and warn
if it reports option positions our ledger does not know about (e.g. placed by
hand or another tool), so the 20% cap is never silently understated.

We cannot fully reconstruct an arbitrary positions() body into defined-risk
spreads until its shape is confirmed (run discover.py). Until then the ledger is
authoritative for OUR spreads, and unknown external option positions trigger a
loud warning. During a single dry-run, accepted candidates are added in-memory
so the batch is checked against the 20% cap cumulatively.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from . import config
from .logutil import get_logger


@dataclass
class BookRiskTracker:
    equity: float
    open_risk: float = 0.0           # dollars of defined max loss currently committed
    _entries: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, equity: float) -> "BookRiskTracker":
        log = get_logger()
        risk = 0.0
        entries: list[dict] = []
        if os.path.exists(config.BOOK_LEDGER_PATH):
            try:
                with open(config.BOOK_LEDGER_PATH) as f:
                    data = json.load(f)
                for e in data.get("open", []):
                    entries.append(e)
                    risk += float(e.get("total_max_loss", 0.0))
                log.info("Loaded book ledger: %d open spreads, $%.2f committed risk",
                         len(entries), risk)
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Could not read book ledger (%s); starting from 0.", e)
        return cls(equity=equity, open_risk=risk, _entries=entries)

    @property
    def cap_dollars(self) -> float:
        return config.MAX_BOOK_RISK_PCT * self.equity

    @property
    def remaining(self) -> float:
        return self.cap_dollars - self.open_risk

    def would_breach(self, additional_max_loss: float) -> bool:
        return (self.open_risk + additional_max_loss) > self.cap_dollars + 1e-9

    def add(self, payload: dict) -> None:
        """Record an accepted spread against the in-memory running total."""
        tml = float(payload.get("_total_max_loss") or 0.0)
        self.open_risk += tml
        self._entries.append({
            "underlying": payload.get("underlying"),
            "structure": payload.get("structure"),
            "total_max_loss": tml,
            "legs": payload.get("legs"),
        })

    def reconcile_with_positions(self, positions: dict) -> None:
        """Best-effort cross-check; warns on option positions we don't recognize."""
        log = get_logger()
        known = _count_option_positions(positions)
        if known is None:
            log.warning("positions() shape not recognized; cannot cross-check book "
                        "risk. Ledger remains authoritative. Run discover.py to pin it.")
            return
        if known > len(self._entries):
            log.warning("positions() reports %d option legs but ledger knows %d. "
                        "Untracked positions may exist; the 20%% cap could be "
                        "understated. Review before going live.", known, len(self._entries))


def _count_option_positions(positions: dict) -> "int | None":
    """Count option legs in a positions() body across plausible shapes."""
    if not isinstance(positions, dict):
        return None
    for key in ("positions", "data", "results"):
        lst = positions.get(key)
        if isinstance(lst, list):
            n = 0
            for p in lst:
                if not isinstance(p, dict):
                    continue
                atype = str(p.get("assetType") or p.get("asset_type") or p.get("type") or "").upper()
                sym = str(p.get("symbol") or p.get("occ") or "")
                if "OPTION" in atype or _looks_like_occ(sym):
                    n += 1
            return n
    return None


def _looks_like_occ(sym: str) -> bool:
    import re
    return bool(re.match(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$", sym.strip().upper()))
