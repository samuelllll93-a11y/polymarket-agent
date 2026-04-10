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
from datetime import datetime, timedelta
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

    async def send_heartbeat(
        self,
        portfolio_value: float,
        open_positions: int,
        signals_processed: int = 0,
        signals_approved: int = 0,
        top_signal: Optional[object] = None,
    ) -> None:
        """Periodic alive ping with signal counts and top opportunity."""
        top_line = ""
        if top_signal is not None:
            q = getattr(top_signal, "market_question", "")
            edge = getattr(top_signal, "edge", 0.0)
            side = getattr(top_signal, "side", "")
            short_q = q[:60] + "…" if len(q) > 60 else q
            top_line = f"\n🔎 Top: {side} {short_q} (edge {edge:+.1%})"
        msg = (
            f"💓 Heartbeat | {datetime.utcnow().strftime('%H:%M')} UTC\n"
            f"Portfolio: ${portfolio_value:,.2f} | Positions: {open_positions}\n"
            f"Signals: {signals_processed} seen, {signals_approved} approved"
            f"{top_line}"
        )
        await self.send(msg)

    # ------------------------------------------------------------------
    # Command Handling (/home, etc.)
    # ------------------------------------------------------------------

    async def start_command_handler(self, bot_ref) -> None:
        """
        Poll Telegram for incoming commands from authorised chat IDs.

        Args:
            bot_ref: The PolymarketBot instance (used to read live state).
        """
        if not self._enabled:
            logger.info("Command handler skipped — Telegram not configured")
            return

        self._bot_ref = bot_ref
        self._update_offset: int = 0

        # Register /home in the bot's command list
        await self._set_bot_commands()

        logger.info(
            "Telegram command handler started | authorised_ids=%s",
            config.TELEGRAM_AUTHORIZED_CHAT_IDS,
        )

        while True:
            try:
                await self._poll_updates()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Command handler poll error: %s", exc)
            await asyncio.sleep(2)

    async def _set_bot_commands(self) -> None:
        """Register slash commands with Telegram so they appear in the menu."""
        url = f"https://api.telegram.org/bot{self.token}/setMyCommands"
        payload = {
            "commands": [
                {"command": "home", "description": "Dashboard snapshot"},
                {"command": "holdings", "description": "Show all open positions"},
                {"command": "summary", "description": "12-hour trade performance summary"},
            ]
        }
        session = await self._get_session()
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    logger.warning("setMyCommands failed: HTTP %d", resp.status)
        except Exception as exc:
            logger.warning("setMyCommands error: %s", exc)

    async def _poll_updates(self) -> None:
        """Long-poll getUpdates for new messages."""
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        params = {
            "offset": self._update_offset,
            "timeout": 30,
            "allowed_updates": '["message"]',
        }
        session = await self._get_session()
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=40)) as resp:
            if resp.status != 200:
                return
            data = await resp.json()

        for update in data.get("result", []):
            self._update_offset = update["update_id"] + 1
            msg = update.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = (msg.get("text") or "").strip()

            if chat_id not in config.TELEGRAM_AUTHORIZED_CHAT_IDS:
                continue

            if text == "/home" or text.startswith("/home@"):
                await self._handle_home(chat_id)
            elif text == "/holdings" or text.startswith("/holdings@"):
                await self._handle_holdings(chat_id)
            elif text == "/summary" or text.startswith("/summary@"):
                await self._handle_summary(chat_id)

    async def _handle_home(self, chat_id: str) -> None:
        """Build and send the /home dashboard snapshot."""
        bot = getattr(self, "_bot_ref", None)

        # Gather data from live components
        summary = bot.risk_manager.get_portfolio_summary() if bot and bot.risk_manager else {}
        capital = summary.get("capital_usd", config.TOTAL_CAPITAL_USD)
        open_pos = summary.get("open_positions", 0)
        daily_pnl = summary.get("daily_realised_pnl", 0.0)
        kill_active = summary.get("kill_switch_active", False)
        wins = summary.get("total_wins", 0)
        losses = summary.get("total_losses", 0)
        total_trades = wins + losses
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0

        pnl_sign = "+" if daily_pnl >= 0 else ""
        status_line = "\U0001f534 Status: ERROR" if kill_active else "\U0001f7e2 Status: RUNNING"

        msg = (
            "\U0001f3e0 POLYMARKET BOT \u2014 HOME\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\U0001f4b0 Capital: ${capital:,.2f}\n"
            f"\U0001f4ca Active Positions: {open_pos}\n"
            f"\U0001f4b3 Today's P&L: {pnl_sign}${daily_pnl:.2f}\n"
            f"{status_line}\n"
            f"\U0001f3af Max Per Trade: ${config.MAX_CAPITAL_PER_TRADE_USD:.2f}\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\u2705 Wins: {wins} | \u274c Losses: {losses} | "
            f"\U0001f4c8 Win Rate: {win_rate:.0f}%"
        )

        # Send directly to the requesting chat (may differ from default chat_id)
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": chat_id, "text": msg}
        session = await self._get_session()
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    logger.warning("/home send failed: HTTP %d", resp.status)
        except Exception as exc:
            logger.error("/home send error: %s", exc)

    async def _handle_holdings(self, chat_id: str) -> None:
        """Build and send the /holdings open-positions snapshot."""
        bot = getattr(self, "_bot_ref", None)
        if not bot or not bot.risk_manager:
            await self._reply(chat_id, "Holdings data not available yet.")
            return

        positions = bot.risk_manager.open_positions
        if not positions:
            await self._reply(chat_id, "No open positions currently")
            return

        divider = "\u2501" * 20
        lines = ["\U0001f4cb <b>OPEN POSITIONS</b>"]
        total_deployed = 0.0

        for market_id, position in positions.items():
            lines.append(divider)

            # Fetch live market data for question and current probability
            market_name = market_id[:50]
            current_prob = position.current_price
            if bot.clob_client:
                try:
                    market_data = await bot.clob_client.get_market(market_id)
                    if market_data:
                        market_name = market_data.get("question", market_name)[:80]
                        for token in market_data.get("tokens", []):
                            if token.get("outcome", "").lower() in ("yes", "y"):
                                current_prob = float(token.get("price", current_prob))
                                break
                except Exception as exc:
                    logger.warning("holdings: failed to fetch market %s: %s", market_id, exc)

            entry_prob = position.entry_price
            if entry_prob > 0:
                shares = position.size_usd / entry_prob
                if position.side == "NO":
                    pnl_usd = shares * (entry_prob - current_prob)
                else:
                    pnl_usd = shares * (current_prob - entry_prob)
            else:
                pnl_usd = 0.0
            pnl_pct = (pnl_usd / position.size_usd * 100) if position.size_usd > 0 else 0.0

            lines.append(f"<i>{market_name}</i>")
            lines.append(f"Entry probability: {entry_prob * 100:.0f}%")
            lines.append(f"Current probability: {current_prob * 100:.0f}%")
            lines.append(f"P&amp;L: {pnl_pct:+.2f}% | ${pnl_usd:+.2f}")
            total_deployed += position.size_usd

        lines.append(divider)
        lines.append(f"Total deployed: ${total_deployed:.2f}")

        await self._reply(chat_id, "\n".join(lines))

    async def _handle_summary(self, chat_id: str) -> None:
        """Build and send the 12-hour trade performance summary."""
        bot = getattr(self, "_bot_ref", None)
        if not bot or not bot.risk_manager:
            await self._reply(chat_id, "Summary data not available yet.")
            return

        rm = bot.risk_manager
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=12)

        # Closed trades in the last 12h (realized P&L)
        closed = rm.get_trades_since(cutoff)

        # Open positions entered in the last 12h (unrealized P&L as proxy)
        recent_open = [
            p for p in rm.open_positions.values()
            if p.opened_at >= cutoff
        ]

        all_pnls = [t["pnl_usd"] for t in closed]
        all_pnls += [p.unrealised_pnl for p in recent_open]

        total_trades = len(all_pnls)
        if total_trades == 0:
            await self._reply(chat_id, "No trades in the last 12 hours")
            return

        wins = [v for v in all_pnls if v >= 0]
        losses = [v for v in all_pnls if v < 0]
        total_pnl = sum(all_pnls)
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0

        win_pct = len(wins) / total_trades * 100
        loss_pct = len(losses) / total_trades * 100

        def _fmt(val: float) -> str:
            sign = "+" if val >= 0 else "-"
            return f"{sign}${abs(val):.2f}"

        divider = "\u2501" * 20
        lines = [
            "\U0001f4ca <b>12-HOUR SUMMARY</b>",
            divider,
            f"\U0001f550 Period: {cutoff.strftime('%H:%M')} \u2192 {now.strftime('%H:%M')} UTC",
            f"\U0001f4c8 Total Trades: {total_trades}",
            f"\u2705 Wins: {len(wins)} ({win_pct:.0f}%)",
            f"\u274c Losses: {len(losses)} ({loss_pct:.0f}%)",
            f"\U0001f4b0 Total P&amp;L: {_fmt(total_pnl)}",
            f"\U0001f4c9 Avg Win: {_fmt(avg_win)}",
            f"\U0001f4c8 Avg Loss: {_fmt(avg_loss)}",
            divider,
        ]
        await self._reply(chat_id, "\n".join(lines))

    async def _reply(self, chat_id: str, message: str, parse_mode: str = "HTML") -> None:
        """Send a message to a specific chat ID (used for command replies)."""
        if len(message) > _TELEGRAM_MAX_LEN:
            message = message[: _TELEGRAM_MAX_LEN - 20] + "\n...[truncated]"

        if not self._enabled:
            logger.info("[TELEGRAM-LOG] chat=%s | %s", chat_id, message)
            return

        if self.dry_run:
            logger.info("[TELEGRAM-DRY_RUN] chat=%s | %s", chat_id, message)
            return

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message, "parse_mode": parse_mode}
        session = await self._get_session()
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    logger.warning("_reply to %s failed: HTTP %d", chat_id, resp.status)
        except Exception as exc:
            logger.error("_reply to %s error: %s", chat_id, exc)

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
