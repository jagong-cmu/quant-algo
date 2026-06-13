"""Shared structures for a defined-risk vertical spread and its order payload."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .chain import NormOption


@dataclass
class Leg:
    option: NormOption
    instruction: str   # BUY_TO_OPEN | SELL_TO_OPEN

    def to_payload(self, contracts: int) -> dict:
        return {
            "symbol": self.option.symbol,
            "instruction": self.instruction,
            "quantity": contracts,
            "price": round(self.option.mid, 2) if self.option.mid is not None else None,
        }


@dataclass
class Spread:
    underlying: str
    kind: str                 # "put_credit" | "call_debit"
    short_leg: Leg
    long_leg: Leg
    contracts: int
    # signed net per share: credit>0 means we RECEIVE; debit>0 means we PAY.
    net_credit: Optional[float] = None   # set for put_credit
    net_debit: Optional[float] = None    # set for call_debit
    notes: list[str] = field(default_factory=list)

    @property
    def width(self) -> float:
        return abs(self.short_leg.option.strike - self.long_leg.option.strike)

    @property
    def per_contract_max_loss(self) -> Optional[float]:
        """Defined max loss for one spread (in dollars). None if not computable."""
        if self.kind == "put_credit":
            if self.net_credit is None:
                return None
            loss = (self.width - self.net_credit) * 100.0
            return loss if loss > 0 else None  # credit >= width is nonsensical -> reject
        if self.kind == "call_debit":
            if self.net_debit is None or self.net_debit <= 0:
                return None
            return self.net_debit * 100.0
        return None

    @property
    def total_max_loss(self) -> Optional[float]:
        pcm = self.per_contract_max_loss
        return None if pcm is None else pcm * self.contracts

    @property
    def net_price_magnitude(self) -> Optional[float]:
        """Positive net limit price (Schwab/TDA convention)."""
        if self.kind == "put_credit" and self.net_credit is not None:
            return round(self.net_credit, 2)
        if self.kind == "call_debit" and self.net_debit is not None:
            return round(self.net_debit, 2)
        return None

    @property
    def net_side(self) -> str:
        return "CREDIT" if self.kind == "put_credit" else "DEBIT"

    def to_payload(self) -> dict:
        return {
            "underlying": self.underlying,
            "structure": self.kind,
            "net_side": self.net_side,           # annotation
            "legs": [
                self.short_leg.to_payload(self.contracts),
                self.long_leg.to_payload(self.contracts),
            ],
            "order_type": "LIMIT",
            "price": self.net_price_magnitude,    # positive magnitude; see README sign note
            "duration": "DAY",
            "session_type": "NORMAL",
            "complex_order_strategy_type": "VERTICAL",
            "order_strategy_type": "SINGLE",
            # human-readable risk annotations (not sent to the API):
            "_width": self.width,
            "_contracts": self.contracts,
            "_per_contract_max_loss": self.per_contract_max_loss,
            "_total_max_loss": self.total_max_loss,
        }
