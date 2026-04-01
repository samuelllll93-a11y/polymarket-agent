"""
Tests for agents/politics_agent.py — NewsAPI + Claude Politics Signal Agent.

Test cases:
 1.  _parse_claude_response: valid JSON → correct probability + reasoning
 2.  _parse_claude_response: no JSON, float in text → extracted
 3.  _parse_claude_response: garbage → returns (0.50, fallback message)
 4.  _heuristic_fallback: positive keywords → prob > 0.5
 5.  _heuristic_fallback: negative keywords → prob < 0.5
 6.  _heuristic_fallback: no keywords → 0.50
 7.  extract_search_query: removes stop words, returns key terms
 8.  evaluate_market: low liquidity → None
 9.  evaluate_market: edge below MIN_EDGE → None
10.  estimate_probability_with_claude: returns cached result on second call
11.  DRY_RUN does not call signal_callback
12.  fetch_headlines: returns empty list if NEWS_API_KEY not set
"""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.politics_agent import PoliticsAgent, PoliticsSignal
from core.market_scanner import Market


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

def make_market(question: str, yes_price: float = 0.50, liquidity: float = 20_000) -> Market:
    return Market(
        condition_id="cid_politics_test",
        question=question,
        yes_price=yes_price,
        no_price=round(1.0 - yes_price, 4),
        volume_24h=10_000.0,
        liquidity=liquidity,
        expiry_timestamp=None,
        category="politics",
    )


@pytest.fixture
def agent():
    a = PoliticsAgent(dry_run=True, portfolio_value=5000.0)
    a.anthropic_api_key = ""   # Force heuristic mode for unit tests
    return a


# ---------------------------------------------------------------------------
# Test 1: valid JSON response from Claude
# ---------------------------------------------------------------------------

def test_parse_claude_response_valid_json(agent):
    text = '{"probability": 0.72, "reasoning": "Candidate leads in polls."}'
    prob, reasoning = agent._parse_claude_response(text)
    assert abs(prob - 0.72) < 0.001
    assert "polls" in reasoning.lower()


# ---------------------------------------------------------------------------
# Test 2: no JSON, float in text
# ---------------------------------------------------------------------------

def test_parse_claude_response_float_fallback(agent):
    text = "Based on my analysis the probability is approximately 0.65 for YES."
    prob, reasoning = agent._parse_claude_response(text)
    assert abs(prob - 0.65) < 0.001


# ---------------------------------------------------------------------------
# Test 3: garbage → 0.50
# ---------------------------------------------------------------------------

def test_parse_claude_response_garbage(agent):
    prob, reasoning = agent._parse_claude_response("I cannot determine this.")
    assert prob == 0.50


# ---------------------------------------------------------------------------
# Test 4: heuristic fallback — positive keywords → > 0.5
# ---------------------------------------------------------------------------

def test_heuristic_fallback_positive(agent):
    headlines = [
        {"title": "Candidate surges ahead in latest polls", "description": "Polling shows a clear win"},
        {"title": "Strong lead maintained as popularity rises", "description": "Victory seems likely"},
    ]
    prob, reasoning = agent._heuristic_fallback("Will X win the election?", headlines)
    assert prob > 0.5, f"Expected prob > 0.5 with positive headlines, got {prob}"


# ---------------------------------------------------------------------------
# Test 5: heuristic fallback — negative keywords → < 0.5
# ---------------------------------------------------------------------------

def test_heuristic_fallback_negative(agent):
    headlines = [
        {"title": "Candidate trailing badly, facing defeat", "description": "Scandal causes drop"},
        {"title": "Arrest warrant issued, polls falling", "description": "Indicted politician unpopular"},
    ]
    prob, reasoning = agent._heuristic_fallback("Will X win the election?", headlines)
    assert prob < 0.5, f"Expected prob < 0.5 with negative headlines, got {prob}"


# ---------------------------------------------------------------------------
# Test 6: heuristic fallback — no keywords → 0.50
# ---------------------------------------------------------------------------

def test_heuristic_fallback_no_keywords(agent):
    headlines = [
        {"title": "Today the weather was nice", "description": "Sunny skies expected"},
    ]
    prob, reasoning = agent._heuristic_fallback("Will X win the election?", headlines)
    assert prob == 0.50


# ---------------------------------------------------------------------------
# Test 7: extract_search_query removes stop words
# ---------------------------------------------------------------------------

def test_extract_search_query_removes_stop_words(agent):
    query = agent.extract_search_query("Will Donald Trump win the 2024 election?")
    assert "will" not in query.lower()
    assert "the" not in query.lower()
    # Should contain meaningful terms
    assert len(query.strip()) > 0


# ---------------------------------------------------------------------------
# Test 8: evaluate_market — low liquidity → None
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_evaluate_market_low_liquidity(agent):
    market = make_market("Will X win?", liquidity=100)  # Below MIN_LIQUIDITY
    result = await agent.evaluate_market(market)
    assert result is None


# ---------------------------------------------------------------------------
# Test 9: evaluate_market — edge below MIN_EDGE → None
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_evaluate_market_small_edge(agent):
    market = make_market("Will X win?", yes_price=0.50, liquidity=50_000)
    # Make heuristic return 0.50 → |0.50 - 0.50| - fee = -0.02 < MIN_EDGE
    agent._heuristic_fallback = lambda q, h: (0.50, "No edge.")
    agent.fetch_headlines = AsyncMock(return_value=[])
    result = await agent.evaluate_market(market)
    assert result is None


# ---------------------------------------------------------------------------
# Test 10: estimate_probability_with_claude — cache hit on second call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_score_cache_returns_cached(agent):
    question = "Will the incumbent win?"
    # Pre-populate cache
    agent._score_cache[question] = (time.time(), 0.73, "Incumbent leads.")

    prob, reasoning = await agent.estimate_probability_with_claude(
        {"question": question}, []
    )
    assert abs(prob - 0.73) < 0.001
    assert "Incumbent" in reasoning


# ---------------------------------------------------------------------------
# Test 11: DRY_RUN does not call signal_callback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_does_not_call_callback():
    callback = MagicMock()
    agent = PoliticsAgent(dry_run=True, signal_callback=callback, portfolio_value=5000.0)
    agent.anthropic_api_key = ""

    market = make_market("Will candidate A win the senate seat?", yes_price=0.10, liquidity=50_000)
    mock_scanner = MagicMock()
    mock_scanner.get_markets_for_agent.return_value = [market]
    agent.market_scanner = mock_scanner

    # Force heuristic to return strong signal
    agent._heuristic_fallback = lambda q, h: (0.85, "Strong lead in polls.")
    agent.fetch_headlines = AsyncMock(return_value=[])

    await agent._run_scan()
    callback.assert_not_called()


# ---------------------------------------------------------------------------
# Test 12: fetch_headlines returns empty list when NEWS_API_KEY not set
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_headlines_empty_without_api_key(agent):
    agent.news_api_key = ""
    result = await agent.fetch_headlines("election")
    assert result == []
