"""
Order lifecycle manager: place, monitor, and cancel Polymarket orders.

Acts as the single gateway between agent signals and the CLOB client.
Responsibilities:
- Receive validated signals from agents (post risk-manager approval)
- Submit limit orders via CLOBClient (no-op in DRY_RUN mode)
- Track open orders and poll for fills
- Cancel stale unfilled orders after a configurable timeout
- Log every action and notify via Telegram
"""

import asyncio
from typing import Optional


class OrderManager:
    """Manages the full lifecycle of Polymarket orders."""

    def __init__(self, clob_client, risk_manager, alerter, dry_run: bool = True):
        self.clob_client = clob_client
        self.risk_manager = risk_manager
        self.alerter = alerter
        self.dry_run = dry_run
        self._open_orders: dict[str, dict] = {}

    async def submit_signal(self, signal: dict) -> Optional[str]:
        """
        Accept a trade signal, validate with risk manager, and place the order.

        Args:
            signal: Dict with keys: market_id, token_id, side, estimated_prob,
                    market_price, proposed_size_usdc.

        Returns:
            order_id string if placed, None if rejected or dry-run.
        """
        raise NotImplementedError

    async def poll_fills(self) -> list[dict]:
        """Check status of all open orders and process any new fills."""
        raise NotImplementedError

    async def cancel_stale_orders(self, max_age_seconds: int = 3600) -> int:
        """
        Cancel open orders older than max_age_seconds.

        Returns:
            Number of orders cancelled.
        """
        raise NotImplementedError

    async def run(self, poll_interval: int = 60) -> None:
        """Background loop: poll fills and cancel stale orders periodically."""
        raise NotImplementedError
