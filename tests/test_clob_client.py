"""
Tests for core/clob_client.py.

Covers:
- DRY_RUN mode: place_order() returns None without calling the API
- get_markets() paginates correctly across multiple pages
- get_order_book() returns expected bid/ask structure
- cancel_order() handles 404 (already filled) gracefully
- Authentication header construction

STUB - tests to be implemented alongside the full CLOBClient in a later task.
"""

import pytest
from unittest.mock import AsyncMock, patch
from core.clob_client import CLOBClient


@pytest.fixture
def dry_run_client():
    return CLOBClient(dry_run=True)


@pytest.fixture
def live_client():
    return CLOBClient(dry_run=False)


@pytest.mark.asyncio
async def test_place_order_dry_run_returns_none(dry_run_client):
    """In dry-run mode, place_order() must return None without API call."""
    raise NotImplementedError


@pytest.mark.asyncio
async def test_get_markets_paginates(live_client):
    """get_markets() should follow next_cursor until exhausted."""
    raise NotImplementedError


@pytest.mark.asyncio
async def test_get_order_book_structure(live_client):
    """Order book response should contain 'bids' and 'asks' lists."""
    raise NotImplementedError


@pytest.mark.asyncio
async def test_cancel_order_not_found(live_client):
    """cancel_order() should return False gracefully when order not found."""
    raise NotImplementedError
