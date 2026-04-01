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
- Concurrent pagination: single page, multi-page, batch boundary, timing log
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


# ---------------------------------------------------------------------------
# Concurrent Pagination Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_single_page_returns_correct_data(scanner):
    """When only one page exists (< limit), fetch_all_raw returns it correctly."""
    page0 = [make_raw_market(f"cid{i}") for i in range(50)]  # 50 < 100 limit

    async def fake_fetch_page(session, offset, limit=100):
        if offset == 0:
            return page0
        return []

    scanner._fetch_page = fake_fetch_page
    result = await scanner._fetch_all_raw(limit=100)
    assert len(result) == 50
    ids = {m["conditionId"] for m in result}
    assert "cid0" in ids
    assert "cid49" in ids


@pytest.mark.asyncio
async def test_concurrent_multipage_all_results_collected(scanner):
    """With 3 full pages + 1 partial, all markets should be returned."""
    page_data = {
        0:   [make_raw_market(f"p0_{i}") for i in range(10)],
        10:  [make_raw_market(f"p1_{i}") for i in range(10)],
        20:  [make_raw_market(f"p2_{i}") for i in range(10)],
        30:  [make_raw_market(f"p3_{i}") for i in range(5)],   # partial — last page
    }

    async def fake_fetch_page(session, offset, limit=10):
        return page_data.get(offset, [])

    scanner._fetch_page = fake_fetch_page
    result = await scanner._fetch_all_raw(limit=10, batch_size=3)
    assert len(result) == 35
    # Verify first and last IDs present
    ids = {m["conditionId"] for m in result}
    assert "p0_0" in ids
    assert "p3_4" in ids


@pytest.mark.asyncio
async def test_concurrent_empty_page_stops_fetching(scanner):
    """An empty page response should halt pagination cleanly."""
    async def fake_fetch_page(session, offset, limit=100):
        if offset == 0:
            return [make_raw_market(f"cid{i}") for i in range(100)]  # full page
        return []  # second page empty → stop

    scanner._fetch_page = fake_fetch_page
    result = await scanner._fetch_all_raw(limit=100, batch_size=5)
    assert len(result) == 100


@pytest.mark.asyncio
async def test_concurrent_batch_boundary_no_duplicate(scanner):
    """Batch boundaries should not produce duplicate markets."""
    # Exactly 2 full pages + empty third page
    page_data = {
        0:  [make_raw_market(f"a{i}") for i in range(10)],
        10: [make_raw_market(f"b{i}") for i in range(10)],
        20: [],
    }

    async def fake_fetch_page(session, offset, limit=10):
        return page_data.get(offset, [])

    scanner._fetch_page = fake_fetch_page
    result = await scanner._fetch_all_raw(limit=10, batch_size=2)
    assert len(result) == 20
    # No duplicates
    ids = [m["conditionId"] for m in result]
    assert len(ids) == len(set(ids))
