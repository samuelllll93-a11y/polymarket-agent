"""
config.py — Polymarket Bot Configuration

Central source of truth for all settings, thresholds, and constants.
All sensitive values are read from environment variables (never hardcoded).
This module is imported by every other module in the bot.
"""

import os
import logging
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# 1. Environment & Mode
# ---------------------------------------------------------------------------

# Master kill switch — NEVER set False without an explicit decision
DRY_RUN: bool = os.getenv("DRY_RUN", "True").lower() != "false"

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# ---------------------------------------------------------------------------
# 2. Polymarket API Settings
# ---------------------------------------------------------------------------

POLY_CLOB_API_URL: str = "https://clob.polymarket.com"
POLY_GAMMA_API_URL: str = "https://gamma-api.polymarket.com"
POLY_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/"

# Auth credentials — read from .env, never hardcoded
POLY_API_KEY: str = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET: str = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE: str = os.getenv("POLY_API_PASSPHRASE", "")
POLY_FUNDER_ADDRESS: str = os.getenv("POLY_FUNDER_ADDRESS", "")

# ---------------------------------------------------------------------------
# 3. Binance Settings
# ---------------------------------------------------------------------------

BINANCE_WS_URL: str = os.getenv(
    "BINANCE_WS_URL", "wss://stream.binance.com:9443/ws"
)
BINANCE_REST_URL: str = "https://api.binance.com/api/v3"
BINANCE_RECONNECT_ATTEMPTS: int = 5
BINANCE_RECONNECT_DELAY: int = 5  # seconds between reconnect attempts

# ---------------------------------------------------------------------------
# 4. Risk Parameters  *** CRITICAL — DO NOT CHANGE WITHOUT REVIEW ***
# ---------------------------------------------------------------------------

# Capital
TOTAL_CAPITAL_USD: float = float(os.getenv("TOTAL_CAPITAL_USD", "5000"))
MAX_CAPITAL_PER_TRADE_PCT: float = 0.001           # 0.1% per trade → $5 on $5k
MAX_CAPITAL_PER_TRADE_USD: float = TOTAL_CAPITAL_USD * MAX_CAPITAL_PER_TRADE_PCT

# Position limits
MAX_OPEN_POSITIONS: int = 10
MAX_EXPOSURE_PER_MARKET_PCT: float = 0.02          # 2% of capital per market

# Loss limits
DAILY_LOSS_LIMIT_USD: float = 100.0                # Kill switch triggers at -$100/day
DRAWDOWN_WARNING_PCT: float = 0.50                 # Telegram alert at 50% of daily limit
MAX_DRAWDOWN_PCT: float = 0.10                     # 10% portfolio drawdown → emergency exit

# Kelly Criterion
KELLY_FRACTION: float = 0.25                       # Quarter-Kelly for safety
MIN_KELLY_EDGE: float = 0.03                       # Minimum 3% edge required to trade

# Maker rebate
MAKER_REBATE_PCT: float = 0.0002                   # 0.02% rebate on maker orders
TARGET_SPREAD_CAPTURE_PCT: float = 0.01            # Aim to capture 1% of spread

# ---------------------------------------------------------------------------
# 5. Market Filtering
# ---------------------------------------------------------------------------

MIN_LIQUIDITY_USD: float = 10_000       # Skip illiquid markets
MAX_SPREAD_PCT: float = 0.05            # Skip markets with >5% spread
MIN_MARKET_VOLUME_24H: float = 5_000    # Minimum 24h volume in USD
MIN_TIME_TO_EXPIRY_HOURS: int = 2       # Don't trade markets expiring within 2h
MAX_TIME_TO_EXPIRY_DAYS: int = 180      # Don't trade far-future markets (6 months covers elections/geopolitics)

# ---------------------------------------------------------------------------
# 5b. Market Filtering (updated thresholds)
# ---------------------------------------------------------------------------

MIN_LIQUIDITY: float = 100_000          # NegRisk: minimum liquidity per market leg
MIN_PROBABILITY: float = 0.15           # Don't trade below 15% probability
MAX_PROBABILITY: float = 0.85           # Don't trade above 85% probability

# ---------------------------------------------------------------------------
# 5c. NegRisk Arbitrage Parameters
# ---------------------------------------------------------------------------

NEGRISK_ENABLED: bool = os.getenv("NEGRISK_ENABLED", "True").lower() != "false"
NEGRISK_MIN_EDGE: float = float(os.getenv("NEGRISK_MIN_EDGE", "0.03"))       # 3% minimum edge after fees
NEGRISK_MIN_LIQUIDITY: float = float(os.getenv("NEGRISK_MIN_LIQUIDITY", "100000"))  # $100k per leg
NEGRISK_MAX_POSITION_PCT: float = float(os.getenv("NEGRISK_MAX_POSITION_PCT", "0.05"))  # 5% of portfolio max per arb
NEGRISK_SCAN_INTERVAL: int = int(os.getenv("NEGRISK_SCAN_INTERVAL", "60"))   # Scan every 60s

# ---------------------------------------------------------------------------
# 5b. Weather Agent Settings
# ---------------------------------------------------------------------------

WEATHER_SCAN_INTERVAL: int = int(os.getenv("WEATHER_SCAN_INTERVAL", "300"))  # Scan every 5 min
WEATHER_MIN_EDGE: float = float(os.getenv("WEATHER_MIN_EDGE", "0.05"))       # 5% edge threshold
WEATHER_MIN_LIQUIDITY: float = float(os.getenv("WEATHER_MIN_LIQUIDITY", "5000"))  # $5k min liquidity

# NOAA gridpoint locations for major US cities
# Format: (office, grid_x, grid_y)
# Find yours at: https://api.weather.gov/points/{lat},{lon}
NOAA_LOCATIONS: dict = {
    "NYC":     ("OKX", 33, 35),
    "LA":      ("LOX", 149, 48),
    "Chicago": ("LOT", 74, 73),
    "Miami":   ("MFL", 110, 44),
    "Denver":  ("BOU", 57, 62),
}

# ---------------------------------------------------------------------------
# 5c. Politics Agent Settings
# ---------------------------------------------------------------------------

POLITICS_SCAN_INTERVAL: int = int(os.getenv("POLITICS_SCAN_INTERVAL", "600"))  # Scan every 10 min
POLITICS_MIN_EDGE: float = float(os.getenv("POLITICS_MIN_EDGE", "0.06"))        # 6% edge threshold
POLITICS_MIN_LIQUIDITY: float = float(os.getenv("POLITICS_MIN_LIQUIDITY", "10000"))  # $10k min
POLITICS_HEADLINES_COUNT: int = int(os.getenv("POLITICS_HEADLINES_COUNT", "10"))

# ---------------------------------------------------------------------------
# 6. Agent Enable / Disable Flags
# ---------------------------------------------------------------------------

AGENT_BTC_ENABLED: bool = os.getenv("AGENT_BTC_ENABLED", "True").lower() != "false"
AGENT_WEATHER_ENABLED: bool = os.getenv("AGENT_WEATHER_ENABLED", "True").lower() != "false"
AGENT_POLITICS_ENABLED: bool = os.getenv("AGENT_POLITICS_ENABLED", "True").lower() != "false"
AGENT_SPORTS_ENABLED: bool = os.getenv("AGENT_SPORTS_ENABLED", "True").lower() != "false"

# ---------------------------------------------------------------------------
# 7. External API Keys
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
NEWS_API_KEY: str = os.getenv("NEWS_API_KEY", "")
ODDS_API_KEY: str = os.getenv("ODDS_API_KEY", "")
ANTHROPIC_API_KEY: str = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY", "")

# Alchemy / Polygon RPC — falls back to public endpoint if not set
_ALCHEMY_RPC_URL: str = os.getenv("ALCHEMY_RPC_URL", "")
POLYGON_RPC_URL: str = _ALCHEMY_RPC_URL or "https://polygon-rpc.com"
ALCHEMY_RPC_CONFIGURED: bool = bool(_ALCHEMY_RPC_URL)

# ---------------------------------------------------------------------------
# 8. Timing Intervals (seconds unless noted)
# ---------------------------------------------------------------------------

MARKET_SCAN_INTERVAL_SEC: int = 60       # Scan for new tradeable markets
SIGNAL_LOOP_INTERVAL_SEC: int = 10       # Main signal evaluation loop
ORDER_MONITOR_INTERVAL_SEC: int = 30     # Check status of open orders
POSITION_UPDATE_INTERVAL_SEC: int = 60  # Refresh portfolio valuation
DAILY_REPORT_HOUR: int = 0               # Midnight AEST (= 14:00 UTC)
HEARTBEAT_INTERVAL_SEC: int = 300        # Telegram alive-ping every 5 min

# ---------------------------------------------------------------------------
# 9. Telegram Alert Thresholds
# ---------------------------------------------------------------------------

ALERT_ON_TRADE: bool = True
ALERT_ON_DRAWDOWN_WARNING: bool = True
ALERT_ON_KILL_SWITCH: bool = True
ALERT_ON_API_ERROR: bool = True
ALERT_ON_STARTUP_SHUTDOWN: bool = True
ALERT_ON_MORNING_BRIEFING: bool = True
MIN_TRADE_SIZE_TO_ALERT_USD: float = 1.0  # Only alert trades above $1

# ---------------------------------------------------------------------------
# 10. Retry / Rate Limit Settings
# ---------------------------------------------------------------------------

API_RETRY_ATTEMPTS: int = 3
API_RETRY_BACKOFF_BASE: int = 2       # Exponential backoff base (seconds)
API_RATE_LIMIT_SLEEP: float = 0.2     # 200ms pause between API calls
CLOB_RATE_LIMIT_PER_SEC: int = 10     # Max CLOB API calls per second

# ---------------------------------------------------------------------------
# 11. Validation
# ---------------------------------------------------------------------------

def validate_config() -> dict:
    """
    Validate current configuration state.

    Logs warnings for missing credentials and unsafe settings.
    Never raises — bot should still start in DRY_RUN even with bad config.

    Returns:
        dict with keys: 'warnings' (list[str]), 'errors' (list[str]), 'valid' (bool)
    """
    logger = logging.getLogger(__name__)
    warnings: list = []
    errors: list = []

    if not DRY_RUN:
        warnings.append(
            "DRY_RUN=False — bot will place REAL orders on Polymarket!"
        )

    if DRY_RUN and not POLY_API_KEY:
        # In dry run with no creds is fine — just log info
        logger.info("DRY_RUN=True and no POLY_API_KEY set — API calls will be simulated")
    elif not DRY_RUN:
        for name, val in [
            ("POLY_API_KEY", POLY_API_KEY),
            ("POLY_API_SECRET", POLY_API_SECRET),
            ("POLY_API_PASSPHRASE", POLY_API_PASSPHRASE),
            ("POLY_FUNDER_ADDRESS", POLY_FUNDER_ADDRESS),
        ]:
            if not val:
                errors.append(f"{name} is empty — required for live trading")

    if not TELEGRAM_BOT_TOKEN:
        warnings.append("TELEGRAM_BOT_TOKEN not set — Telegram alerts disabled")
    if not TELEGRAM_CHAT_ID:
        warnings.append("TELEGRAM_CHAT_ID not set — Telegram alerts disabled")

    if AGENT_BTC_ENABLED and not BINANCE_WS_URL:
        errors.append("BTC agent enabled but BINANCE_WS_URL is empty")

    if MAX_CAPITAL_PER_TRADE_USD > TOTAL_CAPITAL_USD * 0.05:
        warnings.append(
            f"MAX_CAPITAL_PER_TRADE_USD=${MAX_CAPITAL_PER_TRADE_USD:.2f} "
            f"exceeds 5% of capital — review risk params"
        )

    for w in warnings:
        logger.warning("Config warning: %s", w)
    for e in errors:
        logger.error("Config error: %s", e)

    valid = len(errors) == 0
    return {"warnings": warnings, "errors": errors, "valid": valid}


# ---------------------------------------------------------------------------
# 12. Config Summary (safe to log — no secrets)
# ---------------------------------------------------------------------------

def get_config_summary() -> str:
    """Return a human-readable config summary with no sensitive values."""
    enabled_agents = []
    if NEGRISK_ENABLED:
        enabled_agents.append("negrisk")
    if AGENT_BTC_ENABLED:
        enabled_agents.append("btc")
    if AGENT_WEATHER_ENABLED:
        enabled_agents.append("weather")
    if AGENT_POLITICS_ENABLED:
        enabled_agents.append("politics")
    if AGENT_SPORTS_ENABLED:
        enabled_agents.append("sports")

    creds_status = "SET" if POLY_API_KEY else "MISSING"
    telegram_status = "SET" if TELEGRAM_BOT_TOKEN else "MISSING"

    return (
        f"=== Polymarket Bot Config ===\n"
        f"  DRY_RUN          : {DRY_RUN}\n"
        f"  LOG_LEVEL        : {LOG_LEVEL}\n"
        f"  Capital          : ${TOTAL_CAPITAL_USD:,.2f}\n"
        f"  Max/trade        : ${MAX_CAPITAL_PER_TRADE_USD:.2f} "
        f"({MAX_CAPITAL_PER_TRADE_PCT*100:.2f}%)\n"
        f"  Daily loss limit : ${DAILY_LOSS_LIMIT_USD:.2f}\n"
        f"  Max positions    : {MAX_OPEN_POSITIONS}\n"
        f"  Agents enabled   : {', '.join(enabled_agents) or 'none'}\n"
        f"  API creds        : {creds_status}\n"
        f"  Telegram         : {telegram_status}\n"
        f"============================="
    )


# ---------------------------------------------------------------------------
# 13. Credential Health Check (key names only — never values)
# ---------------------------------------------------------------------------

def credential_health_check() -> list[tuple[str, str]]:
    """
    Return a table of credentials and their status for startup logging.

    Each entry is (key_name, status_string) where status is one of:
      PRESENT  — credential is set and non-empty
      MISSING — credential not set; fallback or degraded mode active
      FALLBACK — credential absent but a safe fallback is in use

    Never logs or returns actual credential values.
    """
    rows: list[tuple[str, str]] = []

    def _status(val: str, fallback_label: str = "") -> str:
        if val:
            return "PRESENT"
        return f"MISSING — {fallback_label}" if fallback_label else "MISSING"

    rows.append((
        "POLY_API_KEY",
        _status(POLY_API_KEY, "DRY_RUN forced — no live orders"),
    ))
    rows.append((
        "POLY_API_SECRET",
        _status(POLY_API_SECRET, "DRY_RUN forced — no live orders"),
    ))
    rows.append((
        "CLAUDE_API_KEY / ANTHROPIC_API_KEY",
        _status(ANTHROPIC_API_KEY, "PoliticsAgent uses keyword heuristic"),
    ))
    rows.append((
        "NEWS_API_KEY",
        _status(NEWS_API_KEY, "PoliticsAgent uses keyword heuristic"),
    ))
    rows.append((
        "ODDS_API_KEY",
        _status(ODDS_API_KEY, "SportsAgent disabled"),
    ))
    rows.append((
        "TELEGRAM_BOT_TOKEN",
        _status(TELEGRAM_BOT_TOKEN, "alerts logged to console"),
    ))
    rows.append((
        "TELEGRAM_CHAT_ID",
        _status(TELEGRAM_CHAT_ID, "alerts logged to console"),
    ))
    if ALCHEMY_RPC_CONFIGURED:
        rows.append(("ALCHEMY_RPC_URL", "PRESENT"))
    else:
        rows.append(("ALCHEMY_RPC_URL", "MISSING — using public Polygon RPC fallback"))

    return rows


def log_credential_health_check() -> None:
    """Log the credential health check table to stdout."""
    _logger = logging.getLogger(__name__)
    rows = credential_health_check()
    _logger.info("[CREDENTIALS] Startup credential status:")
    for key, status in rows:
        _logger.info("[CREDENTIALS] %-26s: %s", key, status)
