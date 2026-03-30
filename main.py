"""
Main entry point and agent orchestration for the Polymarket AI trading bot.

This module initializes all specialist agents (weather, BTC, politics, sports),
coordinates their execution loop, applies risk management gates before any order
is placed, and routes Telegram alerts. DRY_RUN=True means no real orders are sent.
"""

import asyncio
import logging
from config import DRY_RUN
from utils.logger import setup_logging


async def run_agents():
    """Initialize and run all specialist trading agents concurrently."""
    raise NotImplementedError("Agent orchestration loop to be implemented in a later task.")


async def main():
    """Bootstrap logging, load config, then start the agent loop."""
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("Starting Polymarket agent", extra={"dry_run": DRY_RUN})
    await run_agents()


if __name__ == "__main__":
    asyncio.run(main())
