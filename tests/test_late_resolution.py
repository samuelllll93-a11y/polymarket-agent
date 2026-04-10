"""
Tests for agents/late_resolution_agent.py — Late Resolution Sniper Agent.

Test cases:
 1.  Confidence scorer: endDate passed → score includes +90 time points
 2.  Confidence scorer: endDate within 1 hour → +70 points
 3.  Confidence scorer: endDate within 6 hours → +50 points
 4.  Confidence scorer: high price (0.990-0.995) → +30 price points
 5.  Confidence scorer: dispute keyword blocks risky markets (-50)
 6.  Confidence scorer: negRisk market penalised (-40)
 7.  Confidence scorer: tight spread + volume bonuses
 8.  Edge calculation: correct expected return and net edge
 9.  Edge calculation: low price yields too-low net edge
10.  Candidate filtering: market in price range included
11.  Candidate filtering: duplicate market_id blocked
12.  Position tracker prevents duplicate positions
13.  Price volatility: >3c drop in 1h → unsafe
14.  Price volatility: recovery from <0.80 in 24h → unsafe
15.  Price volatility: stable prices → safe
16.  Price volatility: 403 geo-block → safe (don't block)
17.  DRY_RUN: scan logs signal but doesn't crash
"""

import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

from agents.late_resolution_agent import (
    LateResolutionAgent,
    LateResSignal,
    score_resolution_confidence,
    calculate_edge,
    load_positions,
    save_positions,
    POSITIONS_FILE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_market(
    condition_id: str = "cid_test_123",
    question: str = "Will BTC hit $100k?",
    yes_price: float = 0.985,
    no_price: float = 0.015,
    end_date: str = "",
    neg_risk: bool = False,
    volume_24h: float = 15000,
    description: str = "",
) -> dict:
    """Build a minimal Gamma API market dict."""
    m = {
        "conditionId": condition_id,
        "question": question,
        "outcomePrices": json.dumps([str(yes_price), str(no_price)]),
        "volume24hr": volume_24h,
        "negRisk": neg_risk,
    }
    if end_date:
        m["endDate"] = end_date
    if description:
        m["description"] = description
    return m


def future_iso(hours: float) -> str:
    """Return an ISO timestamp hours from now."""
    dt = datetime.now(timezone.utc) + timedelta(hours=hours)
    return dt.isoformat()


def past_iso(hours: float) -> str:
    """Return an ISO timestamp hours ago."""
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.isoformat()


@pytest.fixture
def agent():
    return LateResolutionAgent(dry_run=True)


# ---------------------------------------------------------------------------
# Test 1: endDate passed → +90 time points
# ---------------------------------------------------------------------------

def test_confidence_end_date_passed():
    market = make_market(end_date=past_iso(2), yes_price=0.985)
    score = score_resolution_confidence(market, best_price=0.985, spread=0.003, volume_24h=15000)
    # +90 (past) +20 (price 0.980-0.989) +10 (volume>10k) +10 (spread<0.005) = 130 → clamped 100
    assert score >= 90


# ---------------------------------------------------------------------------
# Test 2: endDate within 1 hour → +70 time points
# ---------------------------------------------------------------------------

def test_confidence_end_date_within_1_hour():
    market = make_market(end_date=future_iso(0.5), yes_price=0.985)
    score = score_resolution_confidence(market, best_price=0.985, spread=0.003, volume_24h=15000)
    # +70 (1h) +20 (price) +10 (vol) +10 (spread) = 110 → clamped 100
    assert score >= 70


# ---------------------------------------------------------------------------
# Test 3: endDate within 6 hours → +50
# ---------------------------------------------------------------------------

def test_confidence_end_date_within_6_hours():
    market = make_market(end_date=future_iso(3), yes_price=0.975)
    score = score_resolution_confidence(market, best_price=0.975, spread=0.003, volume_24h=15000)
    # +50 (6h) +10 (price 0.970-0.979) +10 (vol) +10 (spread) = 80
    assert score >= 70


# ---------------------------------------------------------------------------
# Test 4: high price 0.990-0.995 → +30 price points
# ---------------------------------------------------------------------------

def test_confidence_high_price_bonus():
    market = make_market(end_date=future_iso(48), yes_price=0.993)  # 48h → +30 time bonus
    score = score_resolution_confidence(market, best_price=0.993, spread=0.003, volume_24h=15000)
    # +30 (time <=48h) +30 (price) +10 (vol) +10 (spread) = 80
    assert score == 80


# ---------------------------------------------------------------------------
# Test 5: dispute keyword → -50 penalty
# ---------------------------------------------------------------------------

def test_confidence_dispute_keyword_penalty():
    market = make_market(
        end_date=past_iso(1),
        question="Will the UMA dispute be resolved?",
        yes_price=0.990,
    )
    score = score_resolution_confidence(market, best_price=0.990, spread=0.003, volume_24h=15000)
    # +90 (past) +30 (price) +10 (vol) +10 (spread) -50 (dispute keyword "dispute") = 90
    # Also "uma" triggers but we break after first hit, so only -50 once
    assert score <= 90
    # Without keyword it would be 100 (clamped). With -50 it's <= 90
    # The question has both "dispute" and "UMA" but we break after first

    # Compare to same market without risky keywords
    clean_market = make_market(end_date=past_iso(1), question="Will BTC hit $100k?", yes_price=0.990)
    clean_score = score_resolution_confidence(clean_market, best_price=0.990, spread=0.003, volume_24h=15000)
    assert clean_score > score


# ---------------------------------------------------------------------------
# Test 6: negRisk → -40 penalty
# ---------------------------------------------------------------------------

def test_confidence_negrisk_penalty():
    # Use endDate within 6h so time=50, price=10, vol=10, spread=10 = 80
    # With negRisk: 80 - 40 = 40. Difference = 40.
    market = make_market(end_date=future_iso(3), yes_price=0.975, neg_risk=True)
    score = score_resolution_confidence(market, best_price=0.975, spread=0.003, volume_24h=15000)

    no_neg = make_market(end_date=future_iso(3), yes_price=0.975, neg_risk=False)
    score_clean = score_resolution_confidence(no_neg, best_price=0.975, spread=0.003, volume_24h=15000)

    assert score_clean - score == 40


# ---------------------------------------------------------------------------
# Test 7: tight spread + volume bonuses
# ---------------------------------------------------------------------------

def test_confidence_spread_and_volume_bonuses():
    base_market = make_market(end_date=future_iso(48), yes_price=0.975)

    # No volume, wide spread
    score_low = score_resolution_confidence(base_market, best_price=0.975, spread=0.01, volume_24h=5000)
    # +0 (time) +10 (price) = 10

    # High volume, tight spread
    score_high = score_resolution_confidence(base_market, best_price=0.975, spread=0.003, volume_24h=15000)
    # +0 (time) +10 (price) +10 (vol) +10 (spread) = 30

    assert score_high - score_low == 20


# ---------------------------------------------------------------------------
# Test 8: edge calculation correct
# ---------------------------------------------------------------------------

def test_edge_calculation():
    # Buy at 0.985 → expected return = 0.015/0.985 = 1.523%
    expected_return, net_edge = calculate_edge(0.985, taker_fee=0.005)
    assert abs(expected_return - 0.01523) < 0.001
    assert abs(net_edge - 0.01023) < 0.001
    assert net_edge > 0.005  # Above min threshold


# ---------------------------------------------------------------------------
# Test 9: edge too low at high price
# ---------------------------------------------------------------------------

def test_edge_too_low_at_very_high_price():
    # Buy at 0.997 → expected return = 0.003/0.997 = 0.301%
    expected_return, net_edge = calculate_edge(0.997, taker_fee=0.005)
    assert expected_return < 0.005
    assert net_edge < 0  # Negative after fees


# ---------------------------------------------------------------------------
# Test 10: candidate filtering — market in price range
# ---------------------------------------------------------------------------

def test_find_candidates_in_range(agent):
    markets = [
        make_market(condition_id="cid1", yes_price=0.985, no_price=0.015),
        make_market(condition_id="cid2", yes_price=0.50, no_price=0.50),   # Out of range
        make_market(condition_id="cid3", yes_price=0.015, no_price=0.985),  # NO side in range
    ]
    candidates = agent.find_candidates(markets)
    ids_and_sides = [(c["market_id"], c["side"]) for c in candidates]
    assert ("cid1", "YES") in ids_and_sides
    assert ("cid3", "NO") in ids_and_sides
    # cid2 should not be present
    assert all(c["market_id"] != "cid2" for c in candidates)


# ---------------------------------------------------------------------------
# Test 11: duplicate market_id blocked
# ---------------------------------------------------------------------------

def test_find_candidates_blocks_duplicate(agent):
    # Add an existing open position
    agent._positions = [{"market_id": "cid_dupe", "status": "open"}]

    markets = [
        make_market(condition_id="cid_dupe", yes_price=0.985),
        make_market(condition_id="cid_fresh", yes_price=0.990),
    ]
    candidates = agent.find_candidates(markets)
    assert all(c["market_id"] != "cid_dupe" for c in candidates)
    assert any(c["market_id"] == "cid_fresh" for c in candidates)


# ---------------------------------------------------------------------------
# Test 12: position tracker prevents duplicates
# ---------------------------------------------------------------------------

def test_position_tracker_add_and_prevent_duplicate(tmp_path):
    # Use a fresh agent to avoid state leaking from other tests
    fresh_agent = LateResolutionAgent(dry_run=True)
    fresh_agent._positions = []  # Ensure clean state

    # Redirect positions file to temp
    import agents.late_resolution_agent as mod
    orig = mod.POSITIONS_FILE
    mod.POSITIONS_FILE = tmp_path / "positions.json"

    try:
        signal = LateResSignal(
            market_id="cid_track",
            question="Test market",
            side="YES",
            entry_price=0.985,
            expected_return=0.015,
            net_edge=0.010,
            confidence_score=85,
            expected_resolution="2026-04-10T00:00:00+00:00",
            position_size_usd=50.0,
        )
        fresh_agent._add_position(signal)
        assert fresh_agent._open_position_count() == 1

        # Now check that find_candidates would block this market
        markets = [make_market(condition_id="cid_track", yes_price=0.985)]
        candidates = fresh_agent.find_candidates(markets)
        assert len(candidates) == 0
    finally:
        mod.POSITIONS_FILE = orig


# ---------------------------------------------------------------------------
# Test 13: price volatility >3c drop → unsafe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_volatility_big_drop_unsafe(agent):
    now = time.time()
    mock_history = [
        {"t": now - 1800, "p": 0.99},
        {"t": now - 900, "p": 0.95},   # 4c drop
        {"t": now - 60, "p": 0.96},
    ]

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=mock_history)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=AsyncContextManager(mock_resp))
    agent._get_session = AsyncMock(return_value=mock_session)

    safe = await agent.check_price_volatility("cid_volatile")
    assert safe is False


# ---------------------------------------------------------------------------
# Test 14: recovery from <0.80 in 24h → unsafe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_volatility_recovery_from_low_unsafe(agent):
    now = time.time()
    mock_history = [
        {"t": now - 7200, "p": 0.75},   # Was below 0.80
        {"t": now - 3600, "p": 0.90},
        {"t": now - 60, "p": 0.985},
    ]

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=mock_history)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=AsyncContextManager(mock_resp))
    agent._get_session = AsyncMock(return_value=mock_session)

    safe = await agent.check_price_volatility("cid_recovered")
    assert safe is False


# ---------------------------------------------------------------------------
# Test 15: stable prices → safe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_volatility_stable_safe(agent):
    now = time.time()
    mock_history = [
        {"t": now - 1800, "p": 0.984},
        {"t": now - 900, "p": 0.985},
        {"t": now - 60, "p": 0.986},
    ]

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=mock_history)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=AsyncContextManager(mock_resp))
    agent._get_session = AsyncMock(return_value=mock_session)

    safe = await agent.check_price_volatility("cid_stable")
    assert safe is True


# ---------------------------------------------------------------------------
# Test 16: 403 geo-block → safe (don't block trading)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_volatility_403_allows_trade(agent):
    mock_resp = AsyncMock()
    mock_resp.status = 403

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=AsyncContextManager(mock_resp))
    agent._get_session = AsyncMock(return_value=mock_session)

    safe = await agent.check_price_volatility("cid_geoblock")
    assert safe is True


# ---------------------------------------------------------------------------
# Test 17: dry run scan doesn't crash
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_scan_completes(agent):
    market = make_market(
        condition_id="cid_scan_test",
        question="Will event happen?",
        yes_price=0.985,
        end_date=past_iso(1),
        volume_24h=20000,
    )

    agent.fetch_active_markets = AsyncMock(return_value=[market])
    agent.check_price_volatility = AsyncMock(return_value=True)

    await agent._run_scan()

    assert agent._scans_completed == 1


# ---------------------------------------------------------------------------
# Async context manager helper for mocking aiohttp
# ---------------------------------------------------------------------------

class AsyncContextManager:
    """Helper to mock async context managers (aiohttp response)."""

    def __init__(self, return_value):
        self._return_value = return_value

    async def __aenter__(self):
        return self._return_value

    async def __aexit__(self, *args):
        pass
