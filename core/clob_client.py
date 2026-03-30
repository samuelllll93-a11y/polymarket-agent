"""
Polymarket CLOB API wrapper.

Provides an async interface over the py-clob-client library for all
interaction with Polymarket's Central Limit Order Book:
- Authentication via L1/L2 credentials
- Fetching open markets and order books
- Placing limit and market orders
- Cancelling orders
- Querying fills and positions

All methods respect DRY_RUN mode: when enabled, orders are logged but
never submitted to the live API.

STUB - full implementation built in a later task.
"""

import os
from typing import Optional


class CLOBClient:
    """Thin async wrapper around the Polymarket CLOB REST API."""

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.api_key = os.getenv("POLY_API_KEY")
        self.api_secret = os.getenv("POLY_API_SECRET")
        self.api_passphrase = os.getenv("POLY_API_PASSPHRASE")
        self.funder_address = os.getenv("POLY_FUNDER_ADDRESS")
        self._client = None  # underlying py-clob-client instance

    async def connect(self) -> None:
        """Initialize and authenticate the underlying CLOB client."""
        raise NotImplementedError

    async def get_markets(self, next_cursor: str = "") -> dict:
        """Fetch a paginated list of active Polymarket markets."""
        raise NotImplementedError

    async def get_order_book(self, token_id: str) -> dict:
        """Fetch the current order book for a given market token."""
        raise NotImplementedError

    async def place_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
    ) -> Optional[dict]:
        """
        Place a limit order. Returns order details or None in dry-run mode.

        Args:
            token_id: The YES/NO token identifier for the market.
            side:     'BUY' or 'SELL'.
            size:     Order size in USDC.
            price:    Limit price in [0, 1].
        """
        raise NotImplementedError

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by ID. Returns True if successful."""
        raise NotImplementedError

    async def get_positions(self) -> list[dict]:
        """Retrieve all open positions for the configured funder address."""
        raise NotImplementedError
