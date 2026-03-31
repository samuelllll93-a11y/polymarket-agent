"""
Tests for core/clob_client.py — 15 unit tests using unittest.mock.

Covers:
 1.  DRY_RUN place_order returns simulated response (not None)
 2.  DRY_RUN place_order never makes HTTP call
 3.  DRY_RUN cancel_order returns True without HTTP call
 4.  Live place_order returns None (not yet implemented)
 5.  get_markets returns empty on connection failure
 6.  get_markets returns list on success
 7.  Retry fires on 429 response
 8.  Retry fires on 503 response (ClientError)
 9.  After max retries, raises CLOBConnectionError
10.  DRY_RUN mode returns [] for get_open_orders (no credentials)
11.  DRY_RUN mode returns [] for get_positions (no credentials)
12.  Malformed JSON in response raises/handles gracefully
13.  Connection timeout raises CLOBConnectionError after retries
14.  Auth error (403) raises CLOBAuthError immediately
15.  Rate limiter: second call within interval is delayed
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from aiohttp import ClientResponseError, ClientError

from core.clob_client import CLOBClient, CLOBConnectionError, CLOBAuthError, CLOBRateLimitError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def dry_client():
    return CLOBClient(dry_run=True)


@pytest.fixture
def live_client():
    return CLOBClient(dry_run=False)


async def _connect(client):
    """Helper: connect client without network calls."""
    client._session = MagicMock()
    client._session.closed = False
    client._connected = True


# ---------------------------------------------------------------------------
# Test 1: DRY_RUN place_order returns simulated response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_place_order_returns_response(dry_client):
    """DRY_RUN place_order must return a dict with order_id, not None."""
    await _connect(dry_client)
    resp = await dry_client.place_order("tok1", "BUY", 10.0, 0.55)
    assert resp is not None
    assert "order_id" in resp
    assert resp["order_id"].startswith("DRY_")
    assert resp["dry_run"] is True


# ---------------------------------------------------------------------------
# Test 2: DRY_RUN place_order never makes HTTP call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_place_order_no_http_call(dry_client):
    """DRY_RUN place_order must not call the HTTP session."""
    await _connect(dry_client)
    dry_client._session.post = AsyncMock()
    await dry_client.place_order("tok1", "BUY", 10.0, 0.55)
    dry_client._session.post.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: DRY_RUN cancel_order returns True without HTTP call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_cancel_order_returns_true(dry_client):
    """DRY_RUN cancel_order must return True and not call the HTTP session."""
    await _connect(dry_client)
    dry_client._session.delete = AsyncMock()
    result = await dry_client.cancel_order("order_abc")
    assert result is True
    dry_client._session.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: Live place_order returns None (not implemented)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_live_place_order_returns_none(live_client):
    """Live place_order returns None when py-clob-client is not wired."""
    await _connect(live_client)
    result = await live_client.place_order("tok1", "BUY", 10.0, 0.55)
    assert result is None


# ---------------------------------------------------------------------------
# Test 5: get_markets returns empty list on connection failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_markets_returns_empty_on_error(dry_client):
    """get_markets should return empty dict (with 'data' key) on connection error."""
    await _connect(dry_client)

    async def _failing_get(*args, **kwargs):
        raise CLOBConnectionError("API unreachable")

    with patch.object(dry_client, "_get", side_effect=_failing_get):
        result = await dry_client.get_markets()

    assert result == {"data": [], "next_cursor": ""}


# ---------------------------------------------------------------------------
# Test 6: get_markets returns list on success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_markets_returns_data_on_success(dry_client):
    """get_markets should return the API response dict when request succeeds."""
    await _connect(dry_client)
    mock_response = {"data": [{"id": "m1"}, {"id": "m2"}], "next_cursor": ""}

    with patch.object(dry_client, "_get", AsyncMock(return_value=mock_response)):
        result = await dry_client.get_markets()

    assert result["data"] == mock_response["data"]


# ---------------------------------------------------------------------------
# Helper: build an async context-manager mock for aiohttp session.get()
# ---------------------------------------------------------------------------

def _make_ctx(resp):
    """Return a sync-callable that yields an async context manager wrapping resp."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# Test 7: Retry fires on 429 response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_on_429(dry_client):
    """_get should retry on CLOBRateLimitError (429)."""
    import aiohttp
    await _connect(dry_client)

    call_count = 0

    def mock_get_impl(url, params=None):
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        resp.status = 429
        resp.raise_for_status = MagicMock()
        return _make_ctx(resp)

    session = MagicMock()
    session.closed = False
    session.get = mock_get_impl
    dry_client._session = session

    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises((CLOBRateLimitError, CLOBConnectionError)):
            await dry_client._get("/markets")

    # Should have attempted at least 2 times (retry logic)
    assert call_count >= 2


# ---------------------------------------------------------------------------
# Test 8: Retry fires on network error (ClientError / 503-like)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_on_client_error(dry_client):
    """_get should retry on aiohttp.ClientError."""
    import aiohttp
    await _connect(dry_client)

    call_count = 0

    def mock_get_fail(url, params=None):
        nonlocal call_count
        call_count += 1
        raise aiohttp.ClientConnectionError("Connection refused")

    session = MagicMock()
    session.closed = False
    session.get = mock_get_fail
    dry_client._session = session

    # Patch asyncio.sleep to avoid real delays in tests
    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(CLOBConnectionError):
            await dry_client._get("/markets")

    assert call_count == 3  # API_RETRY_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Test 9: After max retries, CLOBConnectionError is raised
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_max_retries_raises_connection_error(dry_client):
    """After exhausting retries, CLOBConnectionError should be raised."""
    import aiohttp
    await _connect(dry_client)

    def always_fail(url, params=None):
        raise aiohttp.ClientConnectionError("always fails")

    session = MagicMock()
    session.closed = False
    session.get = always_fail
    dry_client._session = session

    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(CLOBConnectionError):
            await dry_client._get("/markets")


# ---------------------------------------------------------------------------
# Test 10: DRY_RUN get_open_orders returns [] (no credentials)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_get_open_orders_returns_empty(dry_client):
    """DRY_RUN without credentials: get_open_orders returns empty list."""
    await _connect(dry_client)
    result = await dry_client.get_open_orders()
    assert result == []


# ---------------------------------------------------------------------------
# Test 11: DRY_RUN get_positions returns [] (no credentials)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_get_positions_returns_empty(dry_client):
    """DRY_RUN without credentials: get_positions returns empty list."""
    await _connect(dry_client)
    result = await dry_client.get_positions()
    assert result == []


# ---------------------------------------------------------------------------
# Test 12: Malformed JSON response handled gracefully
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_malformed_json_raises_or_returns_empty(dry_client):
    """Malformed JSON from API should not crash the bot."""
    import aiohttp
    await _connect(dry_client)

    def bad_json_resp(url, params=None):
        resp = MagicMock()
        resp.status = 200
        resp.raise_for_status = MagicMock()

        async def _bad_json():
            raise aiohttp.ContentTypeError(
                MagicMock(), MagicMock()
            )

        resp.json = _bad_json
        return _make_ctx(resp)

    session = MagicMock()
    session.closed = False
    session.get = bad_json_resp
    dry_client._session = session

    # Should not raise — should return empty / handle error
    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await dry_client.get_markets()

    assert "data" in result


# ---------------------------------------------------------------------------
# Test 13: Connection timeout raises CLOBConnectionError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connection_timeout_raises_error(dry_client):
    """asyncio.TimeoutError during GET should result in CLOBConnectionError."""
    await _connect(dry_client)

    def timeout_get(url, params=None):
        raise asyncio.TimeoutError()

    session = MagicMock()
    session.closed = False
    session.get = timeout_get
    dry_client._session = session

    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(CLOBConnectionError):
            await dry_client._get("/markets")


# ---------------------------------------------------------------------------
# Test 14: Auth error (403) raises CLOBAuthError immediately
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auth_error_403_raises_immediately(dry_client):
    """HTTP 403 should immediately raise CLOBAuthError without retrying."""
    await _connect(dry_client)
    call_count = 0

    def auth_fail(url, params=None):
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        resp.status = 403
        resp.raise_for_status = MagicMock()
        return _make_ctx(resp)

    session = MagicMock()
    session.closed = False
    session.get = auth_fail
    dry_client._session = session

    with pytest.raises(CLOBAuthError):
        await dry_client._get("/markets")

    # Auth errors should not be retried
    assert call_count == 1


# ---------------------------------------------------------------------------
# Test 15: Rate limiter enforces minimum interval between calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limiter_enforces_interval():
    """RateLimiter.acquire() should call asyncio.sleep when calls are too close together."""
    import time
    from core.clob_client import _RateLimiter

    limiter = _RateLimiter(calls_per_second=2)  # 0.5s interval

    sleep_calls = []

    async def mock_sleep(n):
        sleep_calls.append(n)

    with patch("asyncio.sleep", mock_sleep):
        # Simulate a very recent last call so next acquire triggers sleep
        limiter._last_call = time.monotonic()  # "just called"
        await limiter.acquire()

    # asyncio.sleep should have been called with a positive delay
    assert len(sleep_calls) == 1
    assert sleep_calls[0] > 0
    assert sleep_calls[0] <= 0.5  # interval = 1/2 = 0.5s
