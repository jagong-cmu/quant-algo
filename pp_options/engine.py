"""Decision engine: read state -> regime -> build -> guardrails -> dry-run/submit.

The engine is data-source agnostic. A LiveDataSource talks to PentPort + Yahoo Finance;
a MockDataSource (mockdata.py) supplies synthetic data so the full pipeline,
including the logged would-be orders, can be exercised offline with no API key.

Order of operations matches the spec: account state is read first; a VIX quote is
pulled before any premium-selling order; every candidate passes the guardrail
engine before the single submit path (which only logs while LIVE_TRADING=False).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional, Protocol

from . import config, guardrails
from .chain import NormOption
from .logutil import get_logger, log_payload
from .models import Spread
from .risk import BookRiskTracker
from .strategy import build_call_debit_spread, build_put_credit_spread


class DataSource(Protocol):
    def equity(self) -> float: ...
    def positions(self) -> dict: ...
    def normalized_chain(self, underlying: str) -> tuple[list[NormOption], Optional[float]]: ...
    def spot(self, underlying: str) -> Optional[float]: ...
    def vix(self) -> tuple[Optional[float], Optional[float]]: ...
    def sma(self, underlying: str, days: int) -> Optional[float]: ...
    def submit(self, payload: dict) -> dict: ...


@dataclass
class Decision:
    underlying: str
    structure: str
    action: str               # "WOULD_PLACE" | "SKIP" | "BLOCK"
    detail: str
    payload: Optional[dict] = None
    total_max_loss: Optional[float] = None


def _consider(spread: Optional[Spread], reasons: list[str], underlying: str, structure: str,
              equity: float, tracker: BookRiskTracker, ds: DataSource,
              decisions: list[Decision]) -> None:
    log = get_logger()
    if spread is None:
        log.info("[%s] %s SKIP: %s", underlying, structure, " | ".join(reasons))
        decisions.append(Decision(underlying, structure, "SKIP", " | ".join(reasons)))
        return

    report = guardrails.evaluate_order(spread, equity, tracker)
    payload = spread.to_payload()
    if report.blocked:
        log.warning("[%s] %s BLOCKED by guardrails: %s", underlying, structure,
                    " | ".join(report.block_reasons))
        log.info("[%s] %s blocked candidate: %s", underlying, structure, "; ".join(spread.notes))
        decisions.append(Decision(underlying, structure, "BLOCK",
                                  " | ".join(report.block_reasons), payload,
                                  spread.total_max_loss))
        return

    log.info("[%s] %s PASSED guardrails (%s): %s", underlying, structure,
             report.summary(), "; ".join(spread.notes))
    result = ds.submit(payload)
    tracker.add(payload)
    decisions.append(Decision(underlying, structure, "WOULD_PLACE",
                              "; ".join(spread.notes), payload, spread.total_max_loss))
    if config.LIVE_TRADING and isinstance(result, dict) and not result.get("dry_run"):
        _persist_ledger(tracker)


def run_once(ds: DataSource) -> list[Decision]:
    config.validate_config()
    log = get_logger()
    decisions: list[Decision] = []

    log.info("=" * 70)
    log.info("PentPort premium-selling engine | LIVE_TRADING=%s", config.LIVE_TRADING)
    log.info("=" * 70)

    # 1. account state
    equity = ds.equity()
    positions = ds.positions()
    tracker = BookRiskTracker.load(equity)
    tracker.reconcile_with_positions(positions)
    log.info("Book risk budget: $%.2f (%.0f%% of $%.2f); already committed $%.2f",
             tracker.cap_dollars, config.MAX_BOOK_RISK_PCT * 100, equity, tracker.open_risk)

    # 2. volatility regime (VIX pulled BEFORE any premium-selling order)
    vix_level, vix_prev = ds.vix()
    regime = guardrails.vix_gate(vix_level, vix_prev)
    log.info("Regime: %s", " | ".join(regime.reasons))

    # 3. per-underlying
    for underlying in config.UNIVERSE:
        log.info("-" * 70)
        try:
            chain, spot = ds.normalized_chain(underlying)
        except Exception as e:  # network/parse failure on one name shouldn't kill the run
            log.error("[%s] chain fetch/normalize failed: %s -> skipping", underlying, e)
            decisions.append(Decision(underlying, "ALL", "SKIP", f"chain error: {e}"))
            continue
        spot = spot or ds.spot(underlying)
        sma = ds.sma(underlying, config.TREND_SMA_DAYS)
        log.info("[%s] spot=%s, %dd SMA=%s, contracts in chain=%d",
                 underlying, _fmt(spot), config.TREND_SMA_DAYS, _fmt(sma), len(chain))

        # PRIMARY: put credit spread (short premium -> gated by VIX)
        if not regime.short_premium_allowed:
            log.info("[%s] put_credit SKIP: regime blocks short premium (%s)",
                     underlying, " | ".join(regime.reasons))
            decisions.append(Decision(underlying, "put_credit", "SKIP",
                                      "regime: " + " | ".join(regime.reasons)))
        else:
            spread, reasons = build_put_credit_spread(chain, underlying, equity, regime.risk_scale)
            _consider(spread, reasons, underlying, "put_credit", equity, tracker, ds, decisions)

        # SECONDARY: call debit spread (long premium -> gated by 200d trend)
        allowed, why = guardrails.trend_gate(spot, sma)
        log.info("[%s] call_debit trend gate: %s", underlying, why)
        if not allowed:
            decisions.append(Decision(underlying, "call_debit", "SKIP", why))
        else:
            spread, reasons = build_call_debit_spread(chain, underlying, equity)
            _consider(spread, reasons, underlying, "call_debit", equity, tracker, ds, decisions)

    _print_summary(decisions, tracker, equity)
    return decisions


def _persist_ledger(tracker: BookRiskTracker) -> None:
    os.makedirs(config.STATE_DIR, exist_ok=True)
    with open(config.BOOK_LEDGER_PATH, "w") as f:
        json.dump({"open": tracker._entries}, f, indent=2)


def _fmt(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.2f}"


def _print_summary(decisions: list[Decision], tracker: BookRiskTracker, equity: float) -> None:
    log = get_logger()
    would = [d for d in decisions if d.action == "WOULD_PLACE"]
    blocked = [d for d in decisions if d.action == "BLOCK"]
    skipped = [d for d in decisions if d.action == "SKIP"]

    log.info("=" * 70)
    log.info("DRY-RUN SUMMARY (LIVE_TRADING=%s)", config.LIVE_TRADING)
    log.info("Would place: %d | Blocked: %d | Skipped: %d", len(would), len(blocked), len(skipped))
    for d in would:
        log.info("  WOULD PLACE  %-5s %-11s max_loss=$%.2f  | %s",
                 d.underlying, d.structure, d.total_max_loss or 0.0, d.detail)
    for d in blocked:
        log.info("  BLOCKED      %-5s %-11s | %s", d.underlying, d.structure, d.detail)
    log.info("Resulting book risk: $%.2f / $%.2f cap  (%.1f%% of $%.0f equity; cap %.0f%%)",
             tracker.open_risk, tracker.cap_dollars,
             100 * tracker.open_risk / equity if equity else 0.0,
             equity, config.MAX_BOOK_RISK_PCT * 100)
    log.info("=" * 70)
