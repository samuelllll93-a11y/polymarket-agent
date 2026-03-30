"""
Tests for core/market_scanner.py.

Covers:
- Filtering markets by volume and resolution date
- Correct categorization of markets by type keyword
- Cache integration (scanner populates cache after scan)
- get_markets_for_agent() returns only relevant markets per agent type

STUB - tests to be implemented alongside the full MarketScanner in a later task.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from core.market_scanner import MarketScanner


@pytest.fixture
def scanner():
    clob_client = AsyncMock()
    cache = MagicMock()
    return MarketScanner(clob_client=clob_client, cache=cache)


@pytest.mark.asyncio
async def test_scan_filters_low_volume(scanner):
    """Markets below the minimum volume threshold should be excluded."""
    raise NotImplementedError


@pytest.mark.asyncio
async def test_scan_filters_expired_markets(scanner):
    """Already-resolved or expiring-soon markets should be excluded."""
    raise NotImplementedError


def test_categorize_btc_market(scanner):
    """Markets with 'bitcoin' or 'BTC' in the title should map to 'btc'."""
    raise NotImplementedError


def test_categorize_politics_market(scanner):
    """Markets with election-related keywords should map to 'politics'."""
    raise NotImplementedError


def test_get_markets_for_agent_returns_subset(scanner):
    """get_markets_for_agent('weather') should return only weather markets."""
    raise NotImplementedError
