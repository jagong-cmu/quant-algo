"""
Central configuration for the PentPort premium-selling system.

================================================================================
 LIVE_TRADING is the master kill-switch. It is False by default.
 While False, the system NEVER calls trade_options(); it only logs the exact
 order payload it WOULD have sent. Flip it to True yourself, only after you have
 reviewed the logged would-be orders.
================================================================================

The guardrail thresholds in the "HARD RISK LIMITS" block are intentionally plain
module constants with no override hook. The guardrail engine (guardrails.py)
always runs and cannot be disabled by configuration. Do not add a flag that
bypasses it.
"""

from __future__ import annotations

# ----------------------------------------------------------------------------
# MASTER SWITCH
# ----------------------------------------------------------------------------
LIVE_TRADING: bool = False  # <-- keep False until you have reviewed logged orders

# ----------------------------------------------------------------------------
# Account selection
# ----------------------------------------------------------------------------
ACCOUNT_PRODUCT = "options"  # passed to choose_account()

# ----------------------------------------------------------------------------
# Universe & structure
# ----------------------------------------------------------------------------
UNIVERSE = ["SPY", "QQQ", "IWM"]

DTE_MIN = 30
DTE_MAX = 45

# Put credit spread (primary, short premium)
TARGET_SHORT_PUT_DELTA = 0.30        # sell ~30-delta put
# Candidate spread widths in dollars; the smallest viable width that fits the
# per-trade risk cap with >= 1 contract is chosen.
CANDIDATE_WIDTHS = [1.0, 2.0, 3.0, 5.0]
MIN_CREDIT_TO_WIDTH = 0.15           # reject spreads paying < 15% of width (junk credit)

# Call debit spread (secondary, only when regime is risk-on)
LONG_CALL_TARGET_DELTA = 0.55        # long leg ~ slightly ITM/ATM
SHORT_CALL_TARGET_DELTA = 0.30       # short leg caps the spread
CALL_DEBIT_WIDTHS = [1.0, 2.0, 3.0, 5.0]

# ----------------------------------------------------------------------------
# HARD RISK LIMITS  (do not make these bypassable)
# ----------------------------------------------------------------------------
MAX_TRADE_RISK_PCT = 0.03   # per-spread defined-risk max loss <= 3% of equity
MAX_BOOK_RISK_PCT = 0.20    # sum of open defined-risk max loss <= 20% of equity

# ----------------------------------------------------------------------------
# Volatility regime filter
# ----------------------------------------------------------------------------
VIX_ELEVATED = 22.0          # above this -> scale down new short premium
VIX_SKIP = 28.0              # above this -> skip new short premium entirely
VIX_RISING_DOD_PCT = 0.12    # VIX up >12% day-over-day == "rising sharply" -> skip
SCALE_DOWN_FACTOR = 0.5      # elevated-but-not-extreme: cut the per-trade risk budget

# Call debit spreads require the underlying above its N-day SMA (trend filter).
TREND_SMA_DAYS = 200

# ----------------------------------------------------------------------------
# Black-Scholes (used to compute/select delta when the chain omits greeks)
# ----------------------------------------------------------------------------
RISK_FREE_RATE = 0.04
# Rough dividend yields used only for delta estimation; conservative defaults.
DIVIDEND_YIELD = {"SPY": 0.013, "QQQ": 0.006, "IWM": 0.012}
DEFAULT_DIVIDEND_YIELD = 0.01

# ----------------------------------------------------------------------------
# Rate limiting / resilience (API allows 60/min; stay well under)
# ----------------------------------------------------------------------------
MAX_REQUESTS_PER_MIN = 45
REQUEST_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0

# ----------------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------------
LOG_DIR = "logs"
STATE_DIR = "state"
BOOK_LEDGER_PATH = "state/book_ledger.json"


def validate_config() -> None:
    """Fail fast if someone weakens a hard limit past a sane bound."""
    assert 0 < MAX_TRADE_RISK_PCT <= 0.05, "per-trade risk cap must stay <= 5%"
    assert 0 < MAX_BOOK_RISK_PCT <= 0.50, "book risk cap must stay <= 50%"
    assert MAX_TRADE_RISK_PCT <= MAX_BOOK_RISK_PCT
    assert DTE_MIN <= DTE_MAX
    assert VIX_ELEVATED < VIX_SKIP
