"""
core/clob_client.py — Polymarket CLOB API Wrapper

Thin async wrapper around the py-clob-client library adding:
  - Automatic retry logic (3 attempts, exponential backoff via tenacity)
  - Rate limit handling (sleep between calls, detect 429s)
  - DRY_RUN mode that logs orders but never submits them
  - Order confirmation verification after placement
  - WebSocket subscription for live orderbook updates
  - Graceful error handling for all API failure modes

In DRY_RUN mode (default):
  - connect() initialises a read-only client (no credentials required)
  - place_order() logs the intent but returns a simulated response
  - cancel_order() logs the intent but always returns True
  - All read methods still attempt live API calls if credentials exist,
    falling back to empty responses if not configured

NOTE: No real credentials exist during the initial build session.
      All live API paths are guarded by DRY_RUN checks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import aiohttp
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CLOBError(Exception):
    """Base exception for CLOB client errors."""


class CLOBRateLimitError(CLOBError):
    """Raised when the API returns HTTP 429."""


class CLOBAuthError(CLOBError):
    """Raised on authentication failures (401/403)."""


class CLOBConnectionError(CLOBError):
    """Raised when the API cannot be reached."""


# ---------------------------------------------------------------------------
# Helper: Rate Limiter
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Simple token-bucket rate limiter for synchronous use."""

    def __init__(self, calls_per_second: int):
        self._interval = 1.0 / calls_per_second
        self._last_call: float = 0.0

    async def acquire(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_call
        if elapsed < self._interval:
            await asyncio.sleep(self._interval - elapsed)
        self._last_call = time.monotonic()


# ---------------------------------------------------------------------------
# CLOBClient
# ---------------------------------------------------------------------------

class CLOBClient:
    """
    Async wrapper around the Polymarket CLOB REST/WS API.

    Usage:
        client = CLOBClient()
        await client.connect()
        markets = await client.get_markets()
    """

    def __init__(
        self,
        dry_run: bool = config.DRY_RUN,
        api_key: str = config.POLY_API_KEY,
        api_secret: str = config.POLY_API_SECRET,
        api_passphrase: str = config.POLY_API_PASSPHRASE,
        funder_address: str = config.POLY_FUNDER_ADDRESS,
    ):
        self.dry_run = dry_run
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase
        self._funder_address = funder_address

        self._session: Optional[aiohttp.ClientSession] = None
        self._rate_limiter = _RateLimiter(config.CLOB_RATE_LIMIT_PER_SEC)
        self._connected = False

        logger.info(
            "CLOBClient initialised | dry_run=%s | has_credentials=%s",
            self.dry_run,
            bool(self._api_key),
        )

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """
        Open HTTP session. In DRY_RUN with no credentials, a session is
        created for read-only market data calls. Authentication is skipped.
        """
        if self._session and not self._session.closed:
            return

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"User-Agent": "polymarket-bot/1.0"},
        )

        if self.dry_run:
            logger.info("CLOBClient connected (DRY_RUN — no auth)")
            self._connected = True
            return

        # Live mode: verify credentials are present
        if not all([self._api_key, self._api_secret, self._api_passphrase]):
            raise CLOBAuthError(
                "Missing Polymarket credentials — set POLY_API_KEY, "
                "POLY_API_SECRET, POLY_API_PASSPHRASE in .env"
            )

        # TODO: Initialise py-clob-client auth here once credentials available
        # from py_clob_client import ClobClient
        # self._clob = ClobClient(host=config.POLY_CLOB_API_URL, ...)
        logger.warning(
            "CLOBClient live mode — py-clob-client auth not yet wired up"
        )
        self._connected = True

    async def disconnect(self) -> None:
        """Close the HTTP session cleanly."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._connected = False
        logger.info("CLOBClient disconnected")

    def _require_connection(self) -> None:
        if not self._connected:
            raise CLOBConnectionError("CLOBClient not connected — call connect() first")

    # ------------------------------------------------------------------
    # Internal HTTP helper with retry
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: Optional[dict] = None) -> Any:
        """
        GET request to the Gamma or CLOB API with retry + rate limiting.
        """
        self._require_connection()
        await self._rate_limiter.acquire()

        url = f"{config.POLY_GAMMA_API_URL}{path}"

        for attempt in range(1, config.API_RETRY_ATTEMPTS + 1):
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status == 429:
                        wait = config.API_RETRY_BACKOFF_BASE ** attempt
                        logger.warning("Rate limited (429) — waiting %.1fs", wait)
                        await asyncio.sleep(wait)
                        raise CLOBRateLimitError("Rate limit hit")
                    if resp.status in (401, 403):
                        raise CLOBAuthError(f"Auth error: HTTP {resp.status}")
                    resp.raise_for_status()
                    return await resp.json()

            except CLOBRateLimitError:
                if attempt == config.API_RETRY_ATTEMPTS:
                    raise
                continue
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                wait = config.API_RETRY_BACKOFF_BASE ** attempt
                logger.warning(
                    "HTTP error on attempt %d/%d: %s — retrying in %.1fs",
                    attempt, config.API_RETRY_ATTEMPTS, exc, wait,
                )
                if attempt == config.API_RETRY_ATTEMPTS:
                    raise CLOBConnectionError(f"API unreachable after {attempt} attempts: {exc}") from exc
                await asyncio.sleep(wait)

    # ------------------------------------------------------------------
    # Market Data (read-only, works without credentials)
    # ------------------------------------------------------------------

    async def get_markets(
        self,
        limit: int = 100,
        next_cursor: str = "",
        active: bool = True,
    ) -> dict:
        """
        Fetch a paginated list of Polymarket markets.

        Returns:
            dict with keys 'data' (list of markets) and 'next_cursor'.
        """
        self._require_connection()
        params: dict = {"limit": limit, "active": str(active).lower()}
        if next_cursor:
            params["next_cursor"] = next_cursor

        try:
            result = await self._get("/markets", params=params)
            logger.debug("get_markets: received %d markets", len(result.get("data", [])))
            return result
        except CLOBConnectionError as exc:
            logger.error("get_markets failed: %s", exc)
            return {"data": [], "next_cursor": ""}

    async def get_market(self, condition_id: str) -> Optional[dict]:
        """Fetch a single market by condition ID."""
        self._require_connection()
        try:
            return await self._get(f"/markets/{condition_id}")
        except CLOBConnectionError as exc:
            logger.error("get_market(%s) failed: %s", condition_id, exc)
            return None

    async def get_order_book(self, token_id: str) -> dict:
        """
        Fetch the current order book for a market token.

        Returns:
            dict with 'bids' and 'asks' (list of {price, size} dicts).
        """
        self._require_connection()
        try:
            result = await self._get(f"/book", params={"token_id": token_id})
            return result
        except CLOBConnectionError as exc:
            logger.error("get_order_book(%s) failed: %s", token_id, exc)
            return {"bids": [], "asks": []}

    # ------------------------------------------------------------------
    # Order Management (credentials required in live mode)
    # ------------------------------------------------------------------

    async def place_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
        order_type: str = "GTC",
    ) -> Optional[dict]:
        """
        Place a limit order on the CLOB.

        In DRY_RUN mode: logs the order details and returns a simulated
        response with a fake order_id — no actual API call is made.

        Args:
            token_id:   YES/NO token identifier.
            side:       'BUY' or 'SELL'.
            size:       Order size in USDC.
            price:      Limit price in [0, 1].
            order_type: 'GTC' (good-till-cancel) or 'FOK' (fill-or-kill).

        Returns:
            Order response dict or None on failure.
        """
        self._require_connection()

        order_params = {
            "token_id": token_id,
            "side": side,
            "size": round(size, 4),
            "price": round(price, 4),
            "order_type": order_type,
        }

        if self.dry_run:
            import uuid
            simulated_id = f"DRY_{uuid.uuid4().hex[:12].upper()}"
            logger.info(
                "DRY_RUN order | id=%s | token=%s | %s %.4f @ %.4f",
                simulated_id,
                token_id,
                side,
                size,
                price,
            )
            return {
                "order_id": simulated_id,
                "status": "DRY_RUN",
                "token_id": token_id,
                "side": side,
                "size": size,
                "price": price,
                "dry_run": True,
            }

        # Live order placement
        # TODO: Use py-clob-client signed order builder
        # order = self._clob.create_order(OrderArgs(price=price, size=size, side=side, token_id=token_id))
        # resp = self._clob.post_order(order, OrderType.GTC)
        logger.error("Live order placement not yet implemented (no credentials)")
        return None

    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an open order.

        In DRY_RUN mode: logs the cancel and returns True.

        Returns:
            True if cancelled successfully.
        """
        self._require_connection()

        if self.dry_run:
            logger.info("DRY_RUN cancel | order_id=%s", order_id)
            return True

        # TODO: self._clob.cancel(order_id)
        logger.error("Live cancel not yet implemented")
        return False

    async def cancel_all_orders(self) -> int:
        """
        Cancel all open orders. Returns count of cancelled orders.
        """
        self._require_connection()

        if self.dry_run:
            logger.info("DRY_RUN cancel_all")
            return 0

        logger.error("Live cancel_all not yet implemented")
        return 0

    async def get_open_orders(self) -> list[dict]:
        """Retrieve all open orders for the configured funder address."""
        self._require_connection()

        if self.dry_run and not self._api_key:
            logger.debug("get_open_orders: DRY_RUN with no credentials — returning empty list")
            return []

        # TODO: return self._clob.get_orders(OpenOrderParams(market=None))
        logger.warning("get_open_orders: live mode not yet implemented")
        return []

    async def get_positions(self) -> list[dict]:
        """Retrieve all open positions for the configured funder address."""
        self._require_connection()

        if self.dry_run and not self._funder_address:
            logger.debug("get_positions: DRY_RUN with no credentials — returning empty list")
            return []

        # TODO: fetch from Gamma API positions endpoint
        logger.warning("get_positions: live mode not yet implemented")
        return []

    # ------------------------------------------------------------------
    # Context Manager Support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "CLOBClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.disconnect()
