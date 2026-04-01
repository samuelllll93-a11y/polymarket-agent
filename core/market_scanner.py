"""
core/market_scanner.py — Polymarket Market Discovery & Filtering

Fetches active markets from the Gamma API (no auth required), applies
liquidity/spread/expiry filters, categorizes by type, and caches results
for fast access by specialist agents.

Refresh cycle: every MARKET_SCAN_INTERVAL_SEC seconds (default 5 minutes).

In DRY_RUN: logs markets found; returns real data if API is reachable,
falls back to an empty list (with a warning) if not.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp
from dateutil import parser as dateutil_parser

import config

logger = logging.getLogger(__name__)

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

# Category keyword maps — order matters (first match wins)
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "btc":       ["bitcoin", "btc", "crypto btc"],
    "crypto":    ["ethereum", "eth", "crypto", "solana", "sol", "defi"],
    "weather":   ["hurricane", "tornado", "rainfall", "temperature", "storm", "flood"],
    "politics":  ["election", "president", "senate", "congress", "vote", "govern",
                  "biden", "trump", "democrat", "republican", "candidate"],
    "sports":    ["nfl", "nba", "mlb", "nhl", "soccer", "football", "basketball",
                  "baseball", "tennis", "golf", "ufc", "championship", "super bowl",
                  "world cup", "olympic"],
}


# ---------------------------------------------------------------------------
# Market dataclass
# ---------------------------------------------------------------------------

@dataclass
class Market:
    """Normalized representation of a Polymarket market."""

    condition_id: str
    question: str
    yes_price: float
    no_price: float
    volume_24h: float
    liquidity: float
    expiry_timestamp: Optional[float]   # UNIX timestamp, UTC
    category: str = "other"
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def spread(self) -> float:
        """Bid-ask spread as a fraction."""
        return abs(self.yes_price + self.no_price - 1.0)

    @property
    def hours_to_expiry(self) -> Optional[float]:
        if self.expiry_timestamp is None:
            return None
        return (self.expiry_timestamp - time.time()) / 3600.0

    def to_dict(self) -> dict:
        return {
            "condition_id": self.condition_id,
            "question": self.question,
            "yes_price": self.yes_price,
            "no_price": self.no_price,
            "volume_24h": self.volume_24h,
            "liquidity": self.liquidity,
            "expiry_timestamp": self.expiry_timestamp,
            "category": self.category,
            "spread": self.spread,
            "hours_to_expiry": self.hours_to_expiry,
        }


# ---------------------------------------------------------------------------
# MarketScanner
# ---------------------------------------------------------------------------

class MarketScanner:
    """
    Discovers, filters, categorizes, and caches tradeable Polymarket markets.

    Usage:
        scanner = MarketScanner()
        await scanner.scan()
        btc_markets = scanner.get_markets(category='btc')
    """

    def __init__(
        self,
        clob_client=None,                   # Optional legacy reference (unused)
        cache=None,                          # Optional external cache object
        min_liquidity: float = config.MIN_LIQUIDITY_USD,
        max_spread: float = config.MAX_SPREAD_PCT,
        min_hours_to_expiry: int = config.MIN_TIME_TO_EXPIRY_HOURS,
        max_days_to_expiry: int = config.MAX_TIME_TO_EXPIRY_DAYS,
        dry_run: bool = config.DRY_RUN,
    ):
        self.clob_client = clob_client
        self.cache = cache
        self.min_liquidity = min_liquidity
        self.max_spread = max_spread
        self.min_hours_to_expiry = min_hours_to_expiry
        self.max_days_to_expiry = max_days_to_expiry
        self.dry_run = dry_run

        self._markets_by_category: dict[str, list[Market]] = {}
        self._all_markets: list[Market] = []
        self._last_scan_time: float = 0.0
        self._session: Optional[aiohttp.ClientSession] = None

        logger.info(
            "MarketScanner initialised | min_liq=$%.0f | max_spread=%.1f%% | "
            "expiry=%d–%dd | dry_run=%s",
            self.min_liquidity,
            self.max_spread * 100,
            self.min_hours_to_expiry,
            self.max_days_to_expiry,
            self.dry_run,
        )

    # ------------------------------------------------------------------
    # HTTP Session
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10, connect=5),
                headers={"User-Agent": "polymarket-bot/1.0"},
            )
        return self._session

    async def _close_session(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    async def _fetch_page(
        self,
        session: aiohttp.ClientSession,
        offset: int,
        limit: int = 100,
    ) -> list[dict]:
        """Fetch one page of markets from the Gamma API."""
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
        }
        try:
            async with session.get(GAMMA_MARKETS_URL, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()
                if isinstance(data, list):
                    return data
                return data.get("data", data.get("markets", []))
        except aiohttp.ClientError as exc:
            logger.error("MarketScanner: Gamma API error (offset=%d): %s", offset, exc)
            return []
        except Exception as exc:
            logger.error("MarketScanner: Unexpected error (offset=%d): %s", offset, exc)
            return []

    async def _fetch_all_raw(
        self,
        limit: int = 100,
        batch_size: int = 5,
        batch_delay: float = 0.3,
    ) -> list[dict]:
        """
        Fetch all active markets using concurrent paginated requests.

        Strategy:
          1. Fetch page 0 to discover total count and seed results.
          2. Calculate remaining pages, fire them in batches of `batch_size`
             simultaneous requests using asyncio.gather().
          3. Merge results in offset order, drop empty pages to detect last page.

        This reduces wall-clock time from ~30s (sequential) to ~5s (concurrent).
        """
        t_start = time.monotonic()
        session = await self._get_session()

        # --- Seed: fetch first page to get initial data ---
        first_page = await self._fetch_page(session, offset=0, limit=limit)
        if not first_page:
            logger.debug("MarketScanner: first page empty — 0 markets")
            return []

        all_raw: list[dict] = list(first_page)

        # If first page is already the last, we're done
        if len(first_page) < limit:
            elapsed = time.monotonic() - t_start
            logger.info(
                "MarketScanner: Fetched %d markets in %.1fs (1 page)",
                len(all_raw),
                elapsed,
            )
            return all_raw

        # --- Concurrent fetch of remaining pages ---
        offset = limit  # Start from page 2

        while True:
            # Build a batch of offsets to fetch simultaneously
            batch_offsets = [offset + i * limit for i in range(batch_size)]

            pages = await asyncio.gather(
                *[self._fetch_page(session, off, limit) for off in batch_offsets],
                return_exceptions=True,
            )

            reached_end = False
            for page in pages:
                if isinstance(page, Exception):
                    logger.warning("MarketScanner: batch page error: %s", page)
                    reached_end = True
                    break
                if not page:
                    reached_end = True
                    break
                all_raw.extend(page)
                if len(page) < limit:
                    reached_end = True
                    break

            if reached_end:
                break

            offset += batch_size * limit
            # Brief pause between batches to respect API rate limits
            await asyncio.sleep(batch_delay)

        elapsed = time.monotonic() - t_start
        logger.info(
            "MarketScanner: Fetched %d markets in %.1fs (batch=%d, delay=%.1fs)",
            len(all_raw),
            elapsed,
            batch_size,
            batch_delay,
        )
        return all_raw

    # ------------------------------------------------------------------
    # Parsing & Filtering
    # ------------------------------------------------------------------

    def _parse_market(self, raw: dict) -> Optional[Market]:
        """Parse a raw Gamma API market dict into a Market object."""
        condition_id = raw.get("conditionId") or raw.get("condition_id") or raw.get("id", "")
        if not condition_id:
            return None

        question = raw.get("question", raw.get("title", ""))

        # Prices
        yes_price = self._extract_price(raw, "yes")
        no_price = self._extract_price(raw, "no")
        if yes_price is None or no_price is None:
            # Try outcome prices list
            outcome_prices = raw.get("outcomePrices")
            if isinstance(outcome_prices, (list, str)):
                import json
                if isinstance(outcome_prices, str):
                    try:
                        outcome_prices = json.loads(outcome_prices)
                    except json.JSONDecodeError:
                        outcome_prices = []
                if len(outcome_prices) >= 2:
                    try:
                        yes_price = float(outcome_prices[0])
                        no_price = float(outcome_prices[1])
                    except (ValueError, TypeError):
                        pass

        if yes_price is None:
            yes_price = 0.5
        if no_price is None:
            no_price = round(1.0 - yes_price, 6)

        # Volume and liquidity
        volume_24h = self._extract_float(raw, ["volume24hr", "volume_24h", "volume24h", "volume"])
        liquidity = self._extract_float(raw, ["liquidity", "liquidityNum", "liquidity_num"])

        # Expiry
        expiry_ts = self._parse_expiry_ts(raw)

        return Market(
            condition_id=str(condition_id),
            question=question,
            yes_price=round(yes_price, 6),
            no_price=round(no_price, 6),
            volume_24h=volume_24h or 0.0,
            liquidity=liquidity or 0.0,
            expiry_timestamp=expiry_ts,
            raw=raw,
        )

    def _extract_price(self, raw: dict, side: str) -> Optional[float]:
        """Extract YES or NO price from various field name conventions."""
        if side == "yes":
            keys = ["bestBid", "best_bid", "lastTradePrice", "last_trade_price"]
        else:
            keys = ["bestAsk", "best_ask"]

        for key in keys:
            val = raw.get(key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
        return None

    def _extract_float(self, raw: dict, keys: list[str]) -> Optional[float]:
        for key in keys:
            val = raw.get(key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
        return None

    def _parse_expiry_ts(self, raw: dict) -> Optional[float]:
        """Parse expiry from various date fields, return UNIX timestamp."""
        for key in ("end_date_iso", "endDate", "end_date", "expiration", "endDateIso"):
            val = raw.get(key)
            if val:
                try:
                    dt = dateutil_parser.parse(str(val))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.timestamp()
                except (ValueError, TypeError):
                    pass
        return None

    def _passes_filters(self, market: Market) -> bool:
        """Return True if the market passes all baseline filters."""
        # Liquidity
        if market.liquidity < self.min_liquidity:
            return False

        # Spread
        if market.spread > self.max_spread:
            return False

        # Expiry window
        hours = market.hours_to_expiry
        if hours is not None:
            if hours < self.min_hours_to_expiry:
                return False
            if hours > self.max_days_to_expiry * 24:
                return False

        return True

    # ------------------------------------------------------------------
    # Categorization
    # ------------------------------------------------------------------

    def categorize(self, markets: list[Market]) -> dict[str, list[Market]]:
        """
        Group markets by category using keyword matching on question text.

        Categories: btc, crypto, weather, politics, sports, other.

        Returns:
            Dict mapping category name to list of Market objects.
        """
        grouped: dict[str, list[Market]] = {cat: [] for cat in _CATEGORY_KEYWORDS}
        grouped["other"] = []

        for market in markets:
            question_lower = market.question.lower()
            assigned = False
            for category, keywords in _CATEGORY_KEYWORDS.items():
                if any(kw in question_lower for kw in keywords):
                    market.category = category
                    grouped[category].append(market)
                    assigned = True
                    break
            if not assigned:
                market.category = "other"
                grouped["other"].append(market)

        return grouped

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def scan(self) -> list[Market]:
        """
        Fetch all active markets from Polymarket and apply baseline filters.

        Returns:
            List of Market objects passing all filters.
        """
        raw_markets = await self._fetch_all_raw()

        parsed: list[Market] = []
        for raw in raw_markets:
            market = self._parse_market(raw)
            if market is not None and self._passes_filters(market):
                parsed.append(market)

        self._all_markets = parsed
        self._markets_by_category = self.categorize(parsed)
        self._last_scan_time = time.time()

        category_counts = {k: len(v) for k, v in self._markets_by_category.items() if v}

        logger.info(
            "MarketScanner: %d/%d markets passed filters | categories=%s",
            len(parsed),
            len(raw_markets),
            category_counts,
        )

        if self.dry_run:
            logger.info("DRY_RUN: MarketScanner returning %d tradeable markets", len(parsed))

        # Update external cache if provided
        if self.cache is not None:
            try:
                self.cache.set("markets_all", parsed)
                self.cache.set("markets_by_category", self._markets_by_category)
                self.cache.set("last_scan_time", self._last_scan_time)
            except Exception as exc:
                logger.warning("MarketScanner: cache update failed: %s", exc)

        return parsed

    def get_markets(self, category: Optional[str] = None) -> list[Market]:
        """
        Return cached markets, optionally filtered by category.

        Args:
            category: One of 'btc', 'crypto', 'weather', 'politics', 'sports', 'other'.
                      If None, returns all markets.
        """
        if category is None:
            return self._all_markets

        return self._markets_by_category.get(category.lower(), [])

    def get_markets_for_agent(self, agent_type: str) -> list[Market]:
        """Return cached markets relevant to the given agent type."""
        return self.get_markets(category=agent_type)

    def get_market_by_condition_id(self, condition_id: str) -> Optional[Market]:
        """Look up a single market by condition_id."""
        for market in self._all_markets:
            if market.condition_id == condition_id:
                return market
        return None

    @property
    def market_count(self) -> int:
        """Total number of cached tradeable markets."""
        return len(self._all_markets)

    @property
    def last_scan_age_seconds(self) -> float:
        """Seconds since the last successful scan."""
        if self._last_scan_time == 0.0:
            return float("inf")
        return time.time() - self._last_scan_time

    # ------------------------------------------------------------------
    # Background Run Loop
    # ------------------------------------------------------------------

    async def run(self, interval_seconds: int = config.MARKET_SCAN_INTERVAL_SEC) -> None:
        """
        Continuously scan and refresh the market cache on a fixed interval.

        Runs until cancelled.
        """
        logger.info("MarketScanner background loop starting | interval=%ds", interval_seconds)

        try:
            while True:
                try:
                    await self.scan()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("MarketScanner scan error: %s", exc)

                try:
                    await asyncio.sleep(interval_seconds)
                except asyncio.CancelledError:
                    break
        finally:
            logger.info("MarketScanner background loop stopped")
            await self._close_session()
