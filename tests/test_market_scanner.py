"""
Tests for core/market_scanner.py.

Covers:
- Liquidity filter
- Spread filter
- Expiry window filter (too soon / too far)
- Cache integration
- Empty market handling
- Malformed API response handling
- Categorization (BTC, politics)
- get_markets_for_agent returns correct subset
"""

import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

from core.market_scanner import MarketScanner, Market


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_raw_market(
    condition_id: str = "cid1",
    question: str = "Will BTC reach $100k?",
    liquidity: float = 500_000,
    yes_price: float = 0.55,
    no_price: float = 0.45,
    hours_to_expiry: float = 72,
    volume_24h: float = 50_000,
) -> dict:
    expiry_dt = datetime.now(timezone.utc) + timedelta(hours=hours_to_expiry)
    return {
        "conditionId": condition_id,
        "question": question,
        "liquidity": liquidity,
        "outcomePrices": [str(yes_price), str(no_price)],
        "volume24hr": volume_24h,
        "end_date_iso": expiry_dt.isoformat(),
    }


@pytest.fixture
def scanner():
    s = MarketScanner(
        min_liquidity=100_000,
        max_spread=0.05,
        min_hours_to_expiry=2,
        max_days_to_expiry=30,
        dry_run=True,
    )
    return s


# ---------------------------------------------------------------------------
# Test: liquidity filter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_filters_low_liquidity(scanner):
    """Markets below min_liquidity should be excluded."""
    raw = [
        make_raw_market("cid1", liquidity=500_000),   # passes
        make_raw_market("cid2", liquidity=50_000),    # fails (< $100k)
    ]
    scanner._fetch_all_raw = AsyncMock(return_value=raw)
    markets = await scanner.scan()
    assert len(markets) == 1
    assert markets[0].condition_id == "cid1"


# ---------------------------------------------------------------------------
# Test: spread filter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_filters_high_spread(scanner):
    """Markets with spread > max_spread should be excluded."""
    # Spread = abs(yes + no - 1.0)
    # yes=0.70, no=0.20 → spread = 0.10 > 0.05
    raw = [
        make_raw_market("cid_ok",  yes_price=0.55, no_price=0.45),   # spread = 0.0
        make_raw_market("cid_bad", yes_price=0.70, no_price=0.20),   # spread = 0.10
    ]
    scanner._fetch_all_raw = AsyncMock(return_value=raw)
    markets = await scanner.scan()
    ids = {m.condition_id for m in markets}
    assert "cid_ok" in ids
    assert "cid_bad" not in ids


# ---------------------------------------------------------------------------
# Test: expiry too soon
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_filters_expiring_soon(scanner):
    """Markets expiring within min_hours_to_expiry should be excluded."""
    raw = [
        make_raw_market("cid_soon",  hours_to_expiry=1),   # expires in 1h < 2h minimum
        make_raw_market("cid_later", hours_to_expiry=48),  # fine
    ]
    scanner._fetch_all_raw = AsyncMock(return_value=raw)
    markets = await scanner.scan()
    ids = {m.condition_id for m in markets}
    assert "cid_later" in ids
    assert "cid_soon" not in ids


# ---------------------------------------------------------------------------
# Test: expiry too far
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_filters_far_future(scanner):
    """Markets resolving > max_days_to_expiry in the future should be excluded."""
    raw = [
        make_raw_market("cid_far",   hours_to_expiry=31 * 24),  # 31 days > 30
        make_raw_market("cid_close", hours_to_expiry=72),       # 3 days
    ]
    scanner._fetch_all_raw = AsyncMock(return_value=raw)
    markets = await scanner.scan()
    ids = {m.condition_id for m in markets}
    assert "cid_close" in ids
    assert "cid_far" not in ids


# ---------------------------------------------------------------------------
# Test: categorization — BTC
# ---------------------------------------------------------------------------

def test_categorize_btc_market(scanner):
    """Markets with 'bitcoin' or 'BTC' in title should map to 'btc'."""
    markets = [
        Market("c1", "Will BTC reach $100k?", 0.55, 0.45, 0, 200_000, None),
        Market("c2", "Will Bitcoin exceed $90k?", 0.40, 0.60, 0, 200_000, None),
        Market("c3", "Will it rain in Paris?", 0.30, 0.70, 0, 200_000, None),
    ]
    grouped = scanner.categorize(markets)
    btc_ids = {m.condition_id for m in grouped["btc"]}
    assert "c1" in btc_ids
    assert "c2" in btc_ids
    assert "c3" not in btc_ids


# ---------------------------------------------------------------------------
# Test: categorization — politics
# ---------------------------------------------------------------------------

def test_categorize_politics_market(scanner):
    """Markets with election-related keywords should map to 'politics'."""
    markets = [
        Market("c1", "Will the Democrat win the election?", 0.55, 0.45, 0, 200_000, None),
        Market("c2", "Who will win the senate race?", 0.50, 0.50, 0, 200_000, None),
        Market("c3", "NBA championship winner?", 0.30, 0.70, 0, 200_000, None),
    ]
    grouped = scanner.categorize(markets)
    pol_ids = {m.condition_id for m in grouped["politics"]}
    assert "c1" in pol_ids
    assert "c2" in pol_ids
    assert "c3" not in pol_ids


# ---------------------------------------------------------------------------
# Test: get_markets_for_agent returns correct subset
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_markets_for_agent_returns_subset(scanner):
    """get_markets_for_agent('btc') should return only BTC-categorized markets."""
    raw = [
        make_raw_market("btc1", question="Will BTC reach $100k?"),
        make_raw_market("pol1", question="Will the Democrat win the election?"),
        make_raw_market("spt1", question="NBA championship winner?"),
    ]
    scanner._fetch_all_raw = AsyncMock(return_value=raw)
    await scanner.scan()

    btc_markets = scanner.get_markets_for_agent("btc")
    assert len(btc_markets) == 1
    assert btc_markets[0].condition_id == "btc1"

    pol_markets = scanner.get_markets_for_agent("politics")
    assert any(m.condition_id == "pol1" for m in pol_markets)


# ---------------------------------------------------------------------------
# Test: empty market response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_market_response(scanner):
    """Scanner should handle an empty API response gracefully."""
    scanner._fetch_all_raw = AsyncMock(return_value=[])
    markets = await scanner.scan()
    assert markets == []
    assert scanner.market_count == 0


# ---------------------------------------------------------------------------
# Test: malformed market data ignored
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_malformed_market_ignored(scanner):
    """Markets with missing condition_id should be silently skipped."""
    raw = [
        {"question": "No ID here", "liquidity": 500_000},       # no conditionId
        make_raw_market("valid1"),                               # valid
    ]
    scanner._fetch_all_raw = AsyncMock(return_value=raw)
    markets = await scanner.scan()
    assert len(markets) == 1
    assert markets[0].condition_id == "valid1"


# ---------------------------------------------------------------------------
# Test: cache is updated after scan
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_updated_after_scan(scanner):
    """External cache object should receive markets and metadata after scan."""
    cache = MagicMock()
    scanner.cache = cache

    raw = [make_raw_market("cid1")]
    scanner._fetch_all_raw = AsyncMock(return_value=raw)
    await scanner.scan()

    cache.set.assert_any_call("markets_all", scanner._all_markets)
    cache.set.assert_any_call("markets_by_category", scanner._markets_by_category)


# ---------------------------------------------------------------------------
# Test: market_count property
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_market_count(scanner):
    """market_count should reflect the number of filtered markets."""
    raw = [make_raw_market(f"cid{i}") for i in range(5)]
    scanner._fetch_all_raw = AsyncMock(return_value=raw)
    await scanner.scan()
    assert scanner.market_count == 5
