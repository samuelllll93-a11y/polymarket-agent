"""
main.py — Polymarket Bot Entry Point & Agent Orchestration

Wires together all components and runs the bot. This is the file you
start with PM2 (or directly via `python3 main.py`).

Startup sequence:
  1. Set up structured logging
  2. Validate config and log summary
  3. Connect CLOBClient (read-only in DRY_RUN)
  4. Initialise RiskManager
  5. Start enabled agents as asyncio tasks
  6. Run heartbeat + daily PnL report loops
  7. Handle SIGTERM/SIGINT for graceful PM2 shutdown

In DRY_RUN mode (default):
  - No real orders are ever placed
  - Signals are logged to console (and Telegram if configured)
  - All API calls that require credentials are skipped

Usage:
  DRY_RUN=True python3 main.py
  # or via PM2:
  pm2 start main.py --interpreter python3 --name polymarket-bot
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import config
from config import validate_config, get_config_summary
from utils.logger import setup_logging
from core.clob_client import CLOBClient
from core.risk_manager import RiskManager, Position
from agents.btc_agent import BTCAgent, Signal
from utils.telegram_alerts import TelegramAlerter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signal Routing
# ---------------------------------------------------------------------------

class SignalRouter:
    """
    Routes signals from agents through the risk manager.

    In DRY_RUN: logs all signals, never places orders.
    In live mode: approved signals go to the CLOB client for order placement.
    """

    def __init__(
        self,
        risk_manager: RiskManager,
        clob_client: CLOBClient,
        alerter: TelegramAlerter,
        dry_run: bool = config.DRY_RUN,
    ):
        self.risk_manager = risk_manager
        self.clob_client = clob_client
        self.alerter = alerter
        self.dry_run = dry_run
        self.signals_processed: int = 0
        self.signals_approved: int = 0
        self.trades_executed: int = 0

    async def handle_signal(self, signal: Signal) -> None:
        """
        Process a signal through the risk manager and optionally place an order.
        """
        self.signals_processed += 1

        logger.info(
            "Signal received | %s | edge=%+.4f | conf=%.2f",
            signal,
            signal.edge,
            signal.confidence,
        )

        # Risk check
        decision = self.risk_manager.calculate_position_size(
            market_id=signal.market_id,
            win_probability=signal.fair_value,
            market_price=signal.market_price,
            edge=signal.edge,
        )

        if not decision.approved:
            logger.info("Signal REJECTED by risk manager: %s", decision.reason)
            return

        self.signals_approved += 1
        size_usd = decision.recommended_size_usd

        logger.info(
            "Signal APPROVED | size=%.2f | %s @ %.4f",
            size_usd,
            signal.side,
            signal.market_price,
        )

        if self.dry_run:
            logger.info(
                "DRY_RUN: would place %s order on %s | size=%.2f | price=%.4f",
                signal.side,
                signal.market_id[:20],
                size_usd,
                signal.market_price,
            )
        else:
            # Place real order
            resp = await self.clob_client.place_order(
                token_id=signal.market_id,
                side=signal.side,
                size=size_usd,
                price=signal.market_price,
            )
            if resp:
                self.trades_executed += 1
                # Register with risk manager
                self.risk_manager.open_position(
                    Position(
                        market_id=signal.market_id,
                        side=signal.side,
                        size_usd=size_usd,
                        entry_price=signal.market_price,
                        current_price=signal.market_price,
                    )
                )

        # Alert regardless of dry/live
        await self.alerter.send_trade(
            market_question=signal.market_question,
            side=signal.side,
            price=signal.market_price,
            size_usd=size_usd,
            market_id=signal.market_id,
            edge=signal.edge,
            agent=signal.agent,
        )


# ---------------------------------------------------------------------------
# Bot Orchestrator
# ---------------------------------------------------------------------------

class PolymarketBot:
    """
    Top-level orchestrator. Owns all components and manages the event loop.
    """

    def __init__(self):
        self.dry_run = config.DRY_RUN
        self._shutdown_event = asyncio.Event()
        self._start_time: float = 0.0
        self._tasks: list[asyncio.Task] = []

        # Components (initialised in start())
        self.clob_client: Optional[CLOBClient] = None
        self.risk_manager: Optional[RiskManager] = None
        self.alerter: Optional[TelegramAlerter] = None
        self.router: Optional[SignalRouter] = None
        self.btc_agent: Optional[BTCAgent] = None

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise all components and start the agent loop."""
        self._start_time = time.time()
        logger.info("=== Polymarket Bot Starting ===")
        logger.info(get_config_summary())

        # Validate config
        result = validate_config()
        if not result["valid"]:
            logger.error("Config validation failed: %s", result["errors"])
            if not self.dry_run:
                sys.exit(1)

        # Initialise components
        self.clob_client = CLOBClient(dry_run=self.dry_run)
        await self.clob_client.connect()

        self.risk_manager = RiskManager(capital_usd=config.TOTAL_CAPITAL_USD)
        self.alerter = TelegramAlerter(dry_run=self.dry_run)

        self.router = SignalRouter(
            risk_manager=self.risk_manager,
            clob_client=self.clob_client,
            alerter=self.alerter,
            dry_run=self.dry_run,
        )

        # Build enabled agents list
        enabled_agents = []

        if config.AGENT_BTC_ENABLED:
            self.btc_agent = BTCAgent(
                clob_client=self.clob_client,
                signal_callback=self.router.handle_signal,
                dry_run=self.dry_run,
            )
            enabled_agents.append("btc")

        # Startup alert
        await self.alerter.send_startup(enabled_agents)

        # Launch tasks
        if self.btc_agent:
            self._tasks.append(
                asyncio.create_task(self.btc_agent.run(), name="btc_agent")
            )

        self._tasks.append(
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
        )
        self._tasks.append(
            asyncio.create_task(self._daily_report_loop(), name="daily_report")
        )

        logger.info(
            "Bot started | agents=%s | DRY_RUN=%s",
            enabled_agents,
            self.dry_run,
        )

        # Wait for shutdown
        await self._shutdown_event.wait()

    # ------------------------------------------------------------------
    # Background Loops
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Send a periodic heartbeat alert to Telegram."""
        while not self._shutdown_event.is_set():
            await asyncio.sleep(config.HEARTBEAT_INTERVAL_SEC)
            if self._shutdown_event.is_set():
                break
            try:
                summary = self.risk_manager.get_portfolio_summary()
                await self.alerter.send_heartbeat(
                    portfolio_value=summary["portfolio_value_usd"],
                    open_positions=summary["open_positions"],
                )
            except Exception as exc:
                logger.error("Heartbeat loop error: %s", exc)

    async def _daily_report_loop(self) -> None:
        """Send a daily PnL report at the configured hour (UTC)."""
        while not self._shutdown_event.is_set():
            await asyncio.sleep(60)  # Check every minute
            if self._shutdown_event.is_set():
                break
            try:
                now = datetime.now(timezone.utc)
                if now.hour == config.DAILY_REPORT_HOUR and now.minute == 0:
                    summary = self.risk_manager.get_portfolio_summary()
                    await self.alerter.send_daily_pnl(
                        realised_pnl=summary["daily_realised_pnl"],
                        unrealised_pnl=summary["unrealised_pnl"],
                        portfolio_value=summary["portfolio_value_usd"],
                        open_positions=summary["open_positions"],
                        trades_today=self.router.trades_executed,
                    )
                    await asyncio.sleep(61)  # Avoid double-send within same minute
            except Exception as exc:
                logger.error("Daily report loop error: %s", exc)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self, reason: str = "SIGTERM") -> None:
        """Graceful shutdown — cancel tasks, send alert, close connections."""
        logger.info("Shutdown initiated: %s", reason)

        self._shutdown_event.set()

        # Stop agents
        if self.btc_agent:
            await self.btc_agent.stop()

        # Cancel background tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        # Final alert
        if self.alerter:
            try:
                await self.alerter.send_shutdown(reason)
            except Exception:
                pass

        # Close connections
        if self.clob_client:
            await self.clob_client.disconnect()
        if self.alerter:
            await self.alerter.close()

        uptime = time.time() - self._start_time
        logger.info("Bot stopped | uptime=%.0fs | reason=%s", uptime, reason)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

async def main() -> None:
    setup_logging(log_level=config.LOG_LEVEL)
    bot = PolymarketBot()

    loop = asyncio.get_running_loop()

    def _signal_handler(sig_name: str):
        logger.info("Signal received: %s", sig_name)
        asyncio.create_task(bot.shutdown(reason=sig_name))

    # Register OS signal handlers for PM2 graceful stop
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler, sig.name)

    try:
        await bot.start()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.exception("Unhandled exception in main: %s", exc)
        if bot.alerter:
            try:
                await bot.alerter.send_api_error("main", str(exc))
            except Exception:
                pass
        await bot.shutdown(reason=f"unhandled_exception: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
