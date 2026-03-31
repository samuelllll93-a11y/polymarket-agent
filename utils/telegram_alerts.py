"""
utils/telegram_alerts.py — Telegram Notification System

Sends real-time alerts via a Telegram bot. All alert types used by the
Polymarket bot are implemented here.

Alert types:
  - Bot startup / shutdown
  - Trade executed (market question, side, price, size, dry-run flag)
  - Daily PnL summary at midnight
  - Drawdown warning at 50% of daily limit
  - Kill switch triggered
  - API errors requiring attention
  - Morning briefing with overnight performance
  - Heartbeat (alive ping)

In DRY_RUN mode or when no token is configured, all alerts are logged
to the console instead of being sent to Telegram — bot never silently
fails to alert.

Requires: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in environment.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

import aiohttp

import config

logger = logging.getLogger(__name__)

# Maximum Telegram message length
_TELEGRAM_MAX_LEN = 4096


class TelegramAlerter:
    """
    Async Telegram notification client.

    Usage:
        alerter = TelegramAlerter()
        await alerter.send_startup()
        await alerter.send_trade(...)
        await alerter.close()
    """

    def __init__(
        self,
        token: Optional[str] = None,
        chat_id: Optional[str] = None,
        dry_run: bool = config.DRY_RUN,
    ):
        self.token = token or config.TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or config.TELEGRAM_CHAT_ID
        self.dry_run = dry_run
        self._session: Optional[aiohttp.ClientSession] = None
        self._enabled = bool(self.token and self.chat_id)

        if not self._enabled:
            logger.warning(
                "Telegram alerts disabled — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set"
            )
        else:
            logger.info("TelegramAlerter initialised | chat_id=%s", self.chat_id)

    # ------------------------------------------------------------------
    # Core send
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def send(self, message: str, parse_mode: str = "HTML") -> bool:
        """
        Send a message to the configured Telegram chat.

        Falls back to console logging if Telegram is not configured or
        if the send fails after retries.

        Returns:
            True if sent successfully (or logged in DRY_RUN), False on failure.
        """
        # Truncate if too long
        if len(message) > _TELEGRAM_MAX_LEN:
            message = message[: _TELEGRAM_MAX_LEN - 20] + "\n...[truncated]"

        if not self._enabled:
            logger.info("[TELEGRAM-LOG] %s", message)
            return True

        if self.dry_run:
            logger.info("[TELEGRAM-DRY_RUN] %s", message)
            return True

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": parse_mode,
        }

        session = await self._get_session()
        for attempt in range(1, config.API_RETRY_ATTEMPTS + 1):
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        return True
                    logger.warning(
                        "Telegram send attempt %d/%d failed: HTTP %d",
                        attempt, config.API_RETRY_ATTEMPTS, resp.status,
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "Telegram send attempt %d/%d error: %s",
                    attempt, config.API_RETRY_ATTEMPTS, exc,
                )
            if attempt < config.API_RETRY_ATTEMPTS:
                await asyncio.sleep(config.API_RETRY_BACKOFF_BASE ** attempt)

        logger.error("Telegram send failed after %d attempts — message lost", config.API_RETRY_ATTEMPTS)
        logger.info("[TELEGRAM-FALLBACK] %s", message)
        return False

    # ------------------------------------------------------------------
    # Structured Alert Methods
    # ------------------------------------------------------------------

    async def send_startup(self, agents_enabled: list[str]) -> None:
        """Bot startup alert with configuration summary."""
        if not config.ALERT_ON_STARTUP_SHUTDOWN:
            return
        mode = "DRY RUN" if self.dry_run else "LIVE"
        msg = (
            f"<b>Polymarket Bot Starting</b>\n"
            f"Mode: <code>{mode}</code>\n"
            f"Capital: <code>${config.TOTAL_CAPITAL_USD:,.0f}</code>\n"
            f"Daily loss limit: <code>${config.DAILY_LOSS_LIMIT_USD:.0f}</code>\n"
            f"Agents: <code>{', '.join(agents_enabled) or 'none'}</code>\n"
            f"Started: <code>{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</code>"
        )
        await self.send(msg)

    async def send_shutdown(self, reason: str = "SIGTERM") -> None:
        """Bot shutdown alert."""
        if not config.ALERT_ON_STARTUP_SHUTDOWN:
            return
        msg = (
            f"<b>Polymarket Bot Shutting Down</b>\n"
            f"Reason: <code>{reason}</code>\n"
            f"Time: <code>{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</code>"
        )
        await self.send(msg)

    async def send_trade(
        self,
        market_question: str,
        side: str,
        price: float,
        size_usd: float,
        market_id: str = "",
        order_id: str = "",
        edge: float = 0.0,
        agent: str = "",
    ) -> None:
        """Trade executed alert."""
        if not config.ALERT_ON_TRADE:
            return
        if size_usd < config.MIN_TRADE_SIZE_TO_ALERT_USD:
            return

        mode_tag = "DRY RUN" if self.dry_run else "LIVE"
        msg = (
            f"<b>Trade Executed [{mode_tag}]</b>\n"
            f"Market: <i>{market_question[:80]}</i>\n"
            f"Side: <code>{side}</code> | "
            f"Price: <code>{price:.4f}</code> | "
            f"Size: <code>${size_usd:.2f}</code>\n"
            f"Edge: <code>{edge:+.4f}</code>"
        )
        if agent:
            msg += f" | Agent: <code>{agent}</code>"
        if order_id:
            msg += f"\nOrder ID: <code>{order_id}</code>"
        await self.send(msg)

    async def send_daily_pnl(
        self,
        realised_pnl: float,
        unrealised_pnl: float,
        portfolio_value: float,
        open_positions: int,
        trades_today: int,
    ) -> None:
        """Daily PnL summary (sent at midnight)."""
        pnl_emoji = "+" if realised_pnl >= 0 else ""
        msg = (
            f"<b>Daily PnL Summary</b>\n"
            f"Realised: <code>{pnl_emoji}${realised_pnl:.2f}</code>\n"
            f"Unrealised: <code>${unrealised_pnl:.2f}</code>\n"
            f"Portfolio: <code>${portfolio_value:,.2f}</code>\n"
            f"Open positions: <code>{open_positions}</code>\n"
            f"Trades today: <code>{trades_today}</code>\n"
            f"Date: <code>{datetime.utcnow().strftime('%Y-%m-%d')}</code>"
        )
        await self.send(msg)

    async def send_drawdown_warning(
        self,
        daily_pnl: float,
        pct_of_limit: float,
    ) -> None:
        """Warning when daily PnL hits 50% of the daily loss limit."""
        if not config.ALERT_ON_DRAWDOWN_WARNING:
            return
        msg = (
            f"<b>Drawdown Warning</b>\n"
            f"Daily PnL: <code>-${abs(daily_pnl):.2f}</code> "
            f"(<code>{pct_of_limit:.0f}%</code> of daily limit)\n"
            f"Daily limit: <code>${config.DAILY_LOSS_LIMIT_USD:.0f}</code>"
        )
        await self.send(msg)

    async def send_kill_switch(self, reason: str, daily_pnl: float) -> None:
        """Kill switch triggered alert — highest priority."""
        if not config.ALERT_ON_KILL_SWITCH:
            return
        msg = (
            f"<b>KILL SWITCH ACTIVATED</b>\n"
            f"Reason: <code>{reason}</code>\n"
            f"Daily PnL: <code>-${abs(daily_pnl):.2f}</code>\n"
            f"All new orders blocked until manual reset.\n"
            f"Time: <code>{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</code>"
        )
        await self.send(msg)

    async def send_api_error(
        self,
        service: str,
        error: str,
        consecutive_failures: int = 1,
    ) -> None:
        """API error alert for errors requiring attention."""
        if not config.ALERT_ON_API_ERROR:
            return
        msg = (
            f"<b>API Error — {service}</b>\n"
            f"Error: <code>{str(error)[:200]}</code>\n"
            f"Consecutive failures: <code>{consecutive_failures}</code>\n"
            f"Time: <code>{datetime.utcnow().strftime('%H:%M:%S')} UTC</code>"
        )
        await self.send(msg)

    async def send_morning_briefing(
        self,
        overnight_pnl: float,
        trades_overnight: int,
        best_market: str,
        worst_market: str,
        uptime_pct: float,
        errors_overnight: int,
    ) -> None:
        """Morning briefing with overnight performance summary."""
        if not config.ALERT_ON_MORNING_BRIEFING:
            return
        pnl_sign = "+" if overnight_pnl >= 0 else ""
        mode = "DRY RUN" if self.dry_run else "LIVE"
        msg = (
            f"<b>Morning Briefing [{mode}]</b>\n"
            f"Overnight PnL: <code>{pnl_sign}${overnight_pnl:.2f}</code>\n"
            f"Trades: <code>{trades_overnight}</code>\n"
            f"Best market: <i>{best_market or 'N/A'}</i>\n"
            f"Worst market: <i>{worst_market or 'N/A'}</i>\n"
            f"Uptime: <code>{uptime_pct:.1f}%</code>\n"
            f"Errors: <code>{errors_overnight}</code>"
        )
        await self.send(msg)

    async def send_heartbeat(self, portfolio_value: float, open_positions: int) -> None:
        """Periodic alive ping (every 5 minutes by default)."""
        msg = (
            f"Heartbeat | "
            f"Portfolio: ${portfolio_value:,.2f} | "
            f"Positions: {open_positions} | "
            f"{datetime.utcnow().strftime('%H:%M')} UTC"
        )
        await self.send(msg)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("TelegramAlerter closed")

    async def __aenter__(self) -> "TelegramAlerter":
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()
