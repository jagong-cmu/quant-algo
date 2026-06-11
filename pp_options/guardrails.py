"""Hard preconditions that BLOCK order submission.

These are not configurable away. The engine calls evaluate_order() on every
candidate and refuses to submit unless it returns blocked=False. Anything we
cannot verify (missing max loss, unknown VIX, unparseable symbol) BLOCKS.

Guardrails implemented:
  1. Per-trade risk cap   -- defined max loss per spread <= MAX_TRADE_RISK_PCT.
  2. Total-book risk cap  -- open + new defined max loss <= MAX_BOOK_RISK_PCT.
  3. Volatility regime     -- VIX gate for short premium; 200d SMA gate for calls.
  4. Order sanity checks   -- symbols parse, qty positive int, instructions match
                              the structure, net sign correct, max loss computable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import config
from .models import Spread
from .occ import is_valid_occ, parse_occ
from .risk import BookRiskTracker


@dataclass
class GuardrailReport:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, passed: bool, reason: str) -> None:
        self.checks.append((name, passed, reason))

    @property
    def blocked(self) -> bool:
        return any(not passed for _, passed, _ in self.checks)

    @property
    def block_reasons(self) -> list[str]:
        return [f"{name}: {reason}" for name, passed, reason in self.checks if not passed]

    def summary(self) -> str:
        return "; ".join(f"{name}={'OK' if p else 'BLOCK'}" for name, p, _ in self.checks)


# ---- guardrail 4: order sanity --------------------------------------------
def sanity_checks(spread: Spread) -> tuple[bool, list[str]]:
    issues: list[str] = []
    legs = [spread.short_leg, spread.long_leg]

    # symbols parse / round-trip
    for leg in legs:
        if not is_valid_occ(leg.option.symbol):
            issues.append(f"leg symbol does not parse as OCC: {leg.option.symbol!r}")

    # quantities are positive integers
    if not isinstance(spread.contracts, int) or spread.contracts <= 0:
        issues.append(f"contracts must be a positive int, got {spread.contracts!r}")

    # instructions correct for the intended structure
    if spread.kind == "put_credit":
        if spread.short_leg.instruction != "SELL_TO_OPEN":
            issues.append("put credit: short leg must be SELL_TO_OPEN")
        if spread.long_leg.instruction != "BUY_TO_OPEN":
            issues.append("put credit: long leg must be BUY_TO_OPEN")
        if spread.short_leg.option.right != "P" or spread.long_leg.option.right != "P":
            issues.append("put credit: both legs must be puts")
        # short strike must be ABOVE long strike (we sell the closer-to-money put)
        if not (spread.short_leg.option.strike > spread.long_leg.option.strike):
            issues.append("put credit: short strike must be > long strike")
    elif spread.kind == "call_debit":
        if spread.long_leg.instruction != "BUY_TO_OPEN":
            issues.append("call debit: long leg must be BUY_TO_OPEN")
        if spread.short_leg.instruction != "SELL_TO_OPEN":
            issues.append("call debit: short leg must be SELL_TO_OPEN")
        if spread.short_leg.option.right != "C" or spread.long_leg.option.right != "C":
            issues.append("call debit: both legs must be calls")
        # long strike must be BELOW short strike (we buy the closer-to-money call)
        if not (spread.long_leg.option.strike < spread.short_leg.option.strike):
            issues.append("call debit: long strike must be < short strike")
    else:
        issues.append(f"unknown structure {spread.kind!r}")

    # legs share the same expiry and underlying
    if spread.short_leg.option.expiry != spread.long_leg.option.expiry:
        issues.append("legs have different expiries")
    if spread.short_leg.option.underlying != spread.long_leg.option.underlying:
        issues.append("legs have different underlyings")

    # net debit/credit sign sanity
    if spread.kind == "put_credit":
        if spread.net_credit is None or spread.net_credit <= 0:
            issues.append(f"put credit must have a positive credit, got {spread.net_credit}")
    else:
        if spread.net_debit is None or spread.net_debit <= 0:
            issues.append(f"call debit must have a positive debit, got {spread.net_debit}")

    # max loss must be computable AND positive (defined risk)
    if spread.per_contract_max_loss is None or spread.per_contract_max_loss <= 0:
        issues.append("max loss is not computable/defined -- rejecting (no naked risk allowed)")

    return (len(issues) == 0), issues


# ---- guardrails 1 & 2: risk caps + 4: sanity, combined --------------------
def evaluate_order(spread: Spread, equity: float, tracker: BookRiskTracker) -> GuardrailReport:
    report = GuardrailReport()

    # 4. sanity first -- if it is malformed, nothing else is trustworthy
    ok, issues = sanity_checks(spread)
    report.add("sanity", ok, "ok" if ok else " | ".join(issues))

    tml = spread.total_max_loss
    if tml is None:
        report.add("per_trade_risk", False, "max loss not computable")
        report.add("book_risk", False, "max loss not computable")
        return report

    # 1. per-trade cap
    cap = config.MAX_TRADE_RISK_PCT * equity
    report.add(
        "per_trade_risk",
        tml <= cap + 1e-9,
        f"max loss ${tml:.2f} vs cap ${cap:.2f} ({config.MAX_TRADE_RISK_PCT:.0%} of ${equity:.2f})",
    )

    # 2. book cap
    breach = tracker.would_breach(tml)
    report.add(
        "book_risk",
        not breach,
        f"open ${tracker.open_risk:.2f} + new ${tml:.2f} vs cap ${tracker.cap_dollars:.2f} "
        f"({config.MAX_BOOK_RISK_PCT:.0%} of ${equity:.2f})",
    )
    return report


# ---- guardrail 3: volatility regime ---------------------------------------
@dataclass
class RegimeDecision:
    short_premium_allowed: bool
    risk_scale: float            # multiplies the per-trade risk budget (<=1)
    vix_level: Optional[float]
    reasons: list[str] = field(default_factory=list)


def vix_gate(vix_level: Optional[float], vix_prev: Optional[float]) -> RegimeDecision:
    """Decide whether (and how large) new short-premium entries may be."""
    reasons: list[str] = []
    if vix_level is None:
        return RegimeDecision(False, 0.0, None,
                              ["VIX unavailable -> fail closed: no new short premium"])

    if vix_level >= config.VIX_SKIP:
        return RegimeDecision(False, 0.0, vix_level,
                              [f"VIX {vix_level:.1f} >= skip threshold {config.VIX_SKIP} -> skip short premium"])

    if vix_prev is not None and vix_prev > 0:
        dod = (vix_level - vix_prev) / vix_prev
        if dod >= config.VIX_RISING_DOD_PCT:
            return RegimeDecision(False, 0.0, vix_level,
                                  [f"VIX rising sharply: +{dod:.0%} day-over-day "
                                   f">= {config.VIX_RISING_DOD_PCT:.0%} -> skip short premium"])

    if vix_level >= config.VIX_ELEVATED:
        reasons.append(f"VIX {vix_level:.1f} elevated (>= {config.VIX_ELEVATED}) "
                       f"-> scale risk x{config.SCALE_DOWN_FACTOR}")
        return RegimeDecision(True, config.SCALE_DOWN_FACTOR, vix_level, reasons)

    reasons.append(f"VIX {vix_level:.1f} benign -> full size")
    return RegimeDecision(True, 1.0, vix_level, reasons)


def trend_gate(spot: Optional[float], sma: Optional[float]) -> tuple[bool, str]:
    """Call debit spreads allowed only when underlying is above its SMA."""
    if spot is None or sma is None:
        return False, f"trend data unavailable (spot={spot}, sma={sma}) -> fail closed: no calls"
    if spot > sma:
        return True, f"spot {spot:.2f} > {config.TREND_SMA_DAYS}d SMA {sma:.2f} -> risk-on (calls allowed)"
    return False, f"spot {spot:.2f} <= {config.TREND_SMA_DAYS}d SMA {sma:.2f} -> not risk-on (no calls)"
