"""
Market scanner: discovers and filters tradeable Polymarket markets.

Periodically pulls the full Polymarket market list via the CLOB client,
filters for markets that meet liquidity and time-to-resolution criteria,
and categorizes them by type (weather, BTC, politics, sports, other) so
specialist agents can quickly find their relevant markets without scanning
the entire catalogue each cycle.
"""

import asyncio
from typing import Optional


class MarketScanner:
    """Discovers, categorizes, and caches tradeable Polymarket markets."""

    def __init__(self, clob_client, cache):
        self.clob_client = clob_client
        self.cache = cache

    async def scan(self) -> list[dict]:
        """
        Fetch all active markets from Polymarket and apply baseline filters.

        Filters applied:
        - Minimum 24h volume threshold
        - Minimum days-to-resolution (avoids already-resolved markets)
        - Active status only

        Returns:
            List of market dicts passing all filters.
        """
        raise NotImplementedError

    def categorize(self, markets: list[dict]) -> dict[str, list[dict]]:
        """
        Group markets by category using keyword matching on market titles.

        Returns:
            Dict mapping category names to lists of market dicts.
            Categories: 'weather', 'btc', 'politics', 'sports', 'other'.
        """
        raise NotImplementedError

    def get_markets_for_agent(self, agent_type: str) -> list[dict]:
        """Return cached markets relevant to the given agent type."""
        raise NotImplementedError

    async def run(self, interval_seconds: int = 300) -> None:
        """Continuously scan and refresh the market cache on a fixed interval."""
        raise NotImplementedError
