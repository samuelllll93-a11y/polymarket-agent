"""
Tests for agents/sports_agent.py — The Odds API Sports Signal Agent.

Test cases:
 1.  ODDS_API_KEY missing → agent disabled, fetch_odds returns []
 2.  implied_probability: positive odds (underdog) calculation
 3.  implied_probability: negative odds (favourite) calculation
 4.  consensus_probability: average across multiple bookmakers
 5.  consensus_probability: outcome not found → None
 6.  match_sport: NFL keywords → americanfootball_nfl
 7.  match_sport: NBA keyword → basketball_nba
 8.  match_sport: no sport keyword → None
 9.  find_matching_event: team name in question → returns (event, team)
10.  find_matching_event: no matching team → None
11.  evaluate_market: edge > MIN_EDGE → signal returned
12.  evaluate_market: edge < MIN_EDGE → None
13.  evaluate_market: low liquidity → None
14.  fetch_odds: cache hit returns cached data without HTTP call
15.  DRY_RUN does not call signal_callback
"""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.sports_agent import SportsAgent, SportsSignal
from core.market_scanner import Market


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

def make_market(question: str, yes_price: float = 0.50, liquidity: float = 20_000) -> Market:
    return Market(
        condition_id="cid_sports_test",
        question=question,
        yes_price=yes_price,
        no_price=round(1.0 - yes_price, 4),
        volume_24h=10_000.0,
        liquidity=liquidity,
        expiry_timestamp=None,
        category="sports",
    )


def make_event(
    home: str = "Los Angeles Lakers",
    away: str = "Boston Celtics",
    sport: str = "basketball_nba",
    home_price: int = -150,
    away_price: int = 130,
) -> dict:
    """Build a minimal Odds API event dict with one bookmaker."""
    return {
        "id": "evt123",
        "sport_key": sport,
        "home_team": home,
        "away_team": away,
        "commence_time": "2026-04-02T20:00:00Z",
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": home, "price": home_price},
                            {"name": away, "price": away_price},
                        ],
                    }
                ],
            }
        ],
    }


@pytest.fixture
def agent_no_key():
    """Agent with no ODDS_API_KEY — disabled mode."""
    a = SportsAgent(dry_run=True, portfolio_value=5000.0)
    a.odds_api_key = ""
    a._enabled = False
    return a


@pytest.fixture
def agent_with_key():
    """Agent with ODDS_API_KEY set — enabled mode."""
    a = SportsAgent(dry_run=True, portfolio_value=5000.0)
    a.odds_api_key = "fake_key_for_tests"
    a._enabled = True
    return a


# ---------------------------------------------------------------------------
# Test 1: no API key → disabled
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_returns_empty_odds(agent_no_key):
    """fetch_odds should return [] immediately when disabled."""
    result = await agent_no_key.fetch_odds("basketball_nba")
    assert result == []


# ---------------------------------------------------------------------------
# Test 2: implied_probability — positive odds (underdog)
# ---------------------------------------------------------------------------

def test_implied_probability_positive_odds(agent_with_key):
    """American odds +150 → 100/(150+100) = 0.4."""
    prob = agent_with_key.implied_probability(150)
    assert abs(prob - 100.0 / 250.0) < 0.001


# ---------------------------------------------------------------------------
# Test 3: implied_probability — negative odds (favourite)
# ---------------------------------------------------------------------------

def test_implied_probability_negative_odds(agent_with_key):
    """American odds -110 → 110/(110+100) = 0.5238."""
    prob = agent_with_key.implied_probability(-110)
    assert abs(prob - 110.0 / 210.0) < 0.001


# ---------------------------------------------------------------------------
# Test 4: consensus_probability — average across bookmakers
# ---------------------------------------------------------------------------

def test_consensus_probability_averages_bookmakers(agent_with_key):
    """Should average implied probability from two bookmakers."""
    event = {
        "bookmakers": [
            {
                "key": "bm1",
                "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Team A", "price": -200},  # 200/300 = 0.667
                    {"name": "Team B", "price": 150},
                ]}],
            },
            {
                "key": "bm2",
                "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Team A", "price": -180},  # 180/280 = 0.643
                    {"name": "Team B", "price": 160},
                ]}],
            },
        ]
    }
    prob = agent_with_key.consensus_probability(event, "Team A")
    expected = (200/300 + 180/280) / 2
    assert prob is not None
    assert abs(prob - expected) < 0.001


# ---------------------------------------------------------------------------
# Test 5: consensus_probability — outcome not found → None
# ---------------------------------------------------------------------------

def test_consensus_probability_missing_outcome(agent_with_key):
    event = make_event(home="Lakers", away="Celtics")
    prob = agent_with_key.consensus_probability(event, "Chicago Bulls")
    assert prob is None


# ---------------------------------------------------------------------------
# Test 6: match_sport — NFL keywords
# ---------------------------------------------------------------------------

def test_match_sport_nfl(agent_with_key):
    result = agent_with_key.match_sport("Will the Chiefs win the NFL championship?")
    assert result == "americanfootball_nfl"


# ---------------------------------------------------------------------------
# Test 7: match_sport — NBA keyword
# ---------------------------------------------------------------------------

def test_match_sport_nba(agent_with_key):
    result = agent_with_key.match_sport("Will the Lakers win the NBA finals?")
    assert result == "basketball_nba"


# ---------------------------------------------------------------------------
# Test 8: match_sport — no match → None
# ---------------------------------------------------------------------------

def test_match_sport_no_match(agent_with_key):
    result = agent_with_key.match_sport("Will it rain in NYC tomorrow?")
    assert result is None


# ---------------------------------------------------------------------------
# Test 9: find_matching_event — team name in question
# ---------------------------------------------------------------------------

def test_find_matching_event_home_team(agent_with_key):
    event = make_event(home="Los Angeles Lakers", away="Boston Celtics")
    market = make_market("Will the Los Angeles Lakers win the NBA finals?")
    result = agent_with_key.find_matching_event(market, [event])
    assert result is not None
    matched_event, outcome = result
    assert outcome == "Los Angeles Lakers"


# ---------------------------------------------------------------------------
# Test 10: find_matching_event — no matching team → None
# ---------------------------------------------------------------------------

def test_find_matching_event_no_match(agent_with_key):
    event = make_event(home="Green Bay Packers", away="Dallas Cowboys")
    market = make_market("Will the New York Yankees win the World Series?")
    result = agent_with_key.find_matching_event(market, [event])
    assert result is None


# ---------------------------------------------------------------------------
# Test 11: evaluate_market — strong edge → signal returned
# ---------------------------------------------------------------------------

def test_evaluate_market_returns_signal(agent_with_key):
    """Polymarket price 0.30, Vegas favourite at ~0.60 → big edge → signal."""
    # -150 favourite → 150/250 = 0.60
    event = make_event(home="Los Angeles Lakers", away="Boston Celtics", home_price=-150)
    market = make_market("Will the Los Angeles Lakers win?", yes_price=0.30, liquidity=30_000)
    signal = agent_with_key.evaluate_market(market, event, "Los Angeles Lakers")
    assert signal is not None
    assert signal.side == "YES"
    assert signal.edge >= agent_with_key.MIN_EDGE


# ---------------------------------------------------------------------------
# Test 12: evaluate_market — small edge → None
# ---------------------------------------------------------------------------

def test_evaluate_market_small_edge_returns_none(agent_with_key):
    """Polymarket price matches Vegas → no signal."""
    # -110 → 110/210 = 0.524; market at 0.52 → raw edge = 0.004 < MIN_EDGE
    event = make_event(home="Team A", away="Team B", home_price=-110)
    market = make_market("Will Team A win?", yes_price=0.52, liquidity=30_000)
    signal = agent_with_key.evaluate_market(market, event, "Team A")
    assert signal is None


# ---------------------------------------------------------------------------
# Test 13: evaluate_market — low liquidity → None
# ---------------------------------------------------------------------------

def test_evaluate_market_low_liquidity(agent_with_key):
    event = make_event(home="Lakers", away="Celtics", home_price=-300)
    market = make_market("Will the Lakers win?", yes_price=0.10, liquidity=100)
    signal = agent_with_key.evaluate_market(market, event, "Lakers")
    assert signal is None


# ---------------------------------------------------------------------------
# Test 14: fetch_odds cache hit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_odds_cache_hit(agent_with_key):
    """Second call should return cached data without making HTTP request."""
    fake_data = [make_event()]
    agent_with_key._odds_cache["basketball_nba"] = (time.time(), fake_data)

    result = await agent_with_key.fetch_odds("basketball_nba")
    assert result == fake_data


# ---------------------------------------------------------------------------
# Test 15: DRY_RUN does not call signal_callback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_does_not_call_callback():
    callback = MagicMock()
    agent = SportsAgent(dry_run=True, signal_callback=callback, portfolio_value=5000.0)
    agent.odds_api_key = "fake_key"
    agent._enabled = True

    # One sports market with a big edge
    market = make_market("Will the Los Angeles Lakers win?", yes_price=0.25, liquidity=30_000)
    mock_scanner = MagicMock()
    mock_scanner.get_markets_for_agent.return_value = [market]
    agent.market_scanner = mock_scanner

    # Return one event where Lakers are heavy favourite
    event = make_event(home="Los Angeles Lakers", away="Boston Celtics", home_price=-300)
    agent.fetch_odds = AsyncMock(return_value=[event])

    await agent._run_scan()
    callback.assert_not_called()
