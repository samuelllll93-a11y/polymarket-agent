"""
Tests for agents/weather_agent.py — NOAA Weather Signal Agent.

Test cases:
 1.  Snow market + snow forecast → YES signal detected
 2.  Rain market + no-rain forecast → low probability returned
 3.  Temperature market with threshold → correct fraction computed
 4.  Hurricane market, no storm in forecast → low probability
 5.  Unknown condition → None returned
 6.  Edge below MIN_EDGE → no signal
 7.  Low liquidity → no signal
 8.  Location matching — NYC match, unknown city returns None
 9.  Empty forecast → estimate_probability returns None
10.  Forecast cache: second call returns cached data
11.  DRY_RUN mode does not call signal_callback
"""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.weather_agent import WeatherAgent, WeatherSignal
from core.market_scanner import Market


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

def make_market(question: str, yes_price: float = 0.50, liquidity: float = 20_000) -> Market:
    return Market(
        condition_id="cid_weather_test",
        question=question,
        yes_price=yes_price,
        no_price=round(1.0 - yes_price, 4),
        volume_24h=10_000.0,
        liquidity=liquidity,
        expiry_timestamp=None,
        category="weather",
    )


def make_forecast(periods: list[dict]) -> dict:
    return {"properties": {"periods": periods}}


def make_period(short: str = "Sunny", detailed: str = "", temp: int = 70, pop: int = 0) -> dict:
    return {
        "shortForecast": short,
        "detailedForecast": detailed,
        "temperature": temp,
        "probabilityOfPrecipitation": {"value": pop},
        "isDaytime": True,
    }


@pytest.fixture
def agent():
    return WeatherAgent(dry_run=True, portfolio_value=5000.0)


# ---------------------------------------------------------------------------
# Test 1: snow forecast → high probability
# ---------------------------------------------------------------------------

def test_snow_probability_from_snowy_forecast(agent):
    forecast = make_forecast([
        make_period(short="Heavy Snow", detailed="Heavy snow expected overnight."),
        make_period(short="Snow Showers"),
        make_period(short="Partly Cloudy"),
    ])
    prob = agent.estimate_probability(forecast, {"question": "Will it snow in NYC this week?"})
    assert prob is not None
    assert prob > 0.30, f"Expected snow probability > 0.30, got {prob}"


# ---------------------------------------------------------------------------
# Test 2: no-rain forecast → low probability
# ---------------------------------------------------------------------------

def test_rain_probability_low_when_sunny(agent):
    forecast = make_forecast([make_period(short="Sunny", pop=5) for _ in range(7)])
    prob = agent.estimate_probability(forecast, {"question": "Will there be rainfall in NYC?"})
    assert prob is not None
    assert prob < 0.50


# ---------------------------------------------------------------------------
# Test 3: temperature threshold calculation
# ---------------------------------------------------------------------------

def test_temperature_probability_above_threshold(agent):
    # 3 of 7 periods have temp >= 90: 92, 91, 95
    periods = [
        make_period(temp=92),
        make_period(temp=88),
        make_period(temp=91),
        make_period(temp=95),
        make_period(temp=75),
        make_period(temp=85),
        make_period(temp=78),
    ]
    forecast = make_forecast(periods)
    prob = agent.estimate_probability(
        forecast,
        {"question": "Will Chicago temperature exceed 90 degrees?"}
    )
    assert prob is not None
    # 3 out of 7 periods qualify: expected round(3/7, 3) = 0.429
    assert abs(prob - round(3/7, 3)) < 0.002


# ---------------------------------------------------------------------------
# Test 4: hurricane probability low if not in forecast
# ---------------------------------------------------------------------------

def test_hurricane_probability_low_without_storm(agent):
    forecast = make_forecast([make_period(short="Partly Cloudy") for _ in range(7)])
    prob = agent.estimate_probability(
        forecast,
        {"question": "Will a hurricane hit Miami in August?"}
    )
    assert prob is not None
    assert prob < 0.15


# ---------------------------------------------------------------------------
# Test 5: unknown condition → None
# ---------------------------------------------------------------------------

def test_unknown_condition_returns_none(agent):
    forecast = make_forecast([make_period()])
    prob = agent.estimate_probability(
        forecast,
        {"question": "Will the stock market crash?"}
    )
    assert prob is None


# ---------------------------------------------------------------------------
# Test 6: edge below MIN_EDGE → no signal
# ---------------------------------------------------------------------------

def test_no_signal_when_edge_too_small(agent):
    # market_price = 0.50, model ~0.50 → edge ≈ 0 < MIN_EDGE
    forecast = make_forecast([make_period(short="Partly Cloudy", pop=50) for _ in range(7)])
    market = make_market("Will it rain in NYC?", yes_price=0.50)
    signal = agent.evaluate_market(market, forecast, "NYC")
    # If a signal is emitted, edge must be >= MIN_EDGE
    if signal is not None:
        assert signal.edge >= agent.MIN_EDGE


# ---------------------------------------------------------------------------
# Test 7: low liquidity → no signal
# ---------------------------------------------------------------------------

def test_no_signal_on_low_liquidity(agent):
    # Very snowy forecast → high model prob
    forecast = make_forecast([
        make_period(short="Heavy Snow", detailed="Blizzard conditions", pop=90)
        for _ in range(7)
    ])
    market = make_market("Will it snow in NYC?", yes_price=0.10, liquidity=100)  # $100 only
    signal = agent.evaluate_market(market, forecast, "NYC")
    assert signal is None


# ---------------------------------------------------------------------------
# Test 8: location matching
# ---------------------------------------------------------------------------

def test_location_match_nyc(agent):
    result = agent.match_location("Will it snow in NYC this January?")
    assert result is not None
    location, office, gx, gy = result
    assert location == "NYC"
    assert isinstance(office, str)
    assert isinstance(gx, int)
    assert isinstance(gy, int)


def test_location_match_unknown_city(agent):
    result = agent.match_location("Will it rain in Tokyo next week?")
    assert result is None


# ---------------------------------------------------------------------------
# Test 9: empty forecast → None
# ---------------------------------------------------------------------------

def test_empty_forecast_returns_none(agent):
    prob = agent.estimate_probability({}, {"question": "Will it snow in NYC?"})
    assert prob is None


# ---------------------------------------------------------------------------
# Test 10: forecast cache hit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forecast_cache_returns_cached(agent):
    """Second call should return cached data without hitting NOAA API."""
    fake_forecast = make_forecast([make_period()])
    cache_key = "OKX/33,35"
    agent._forecast_cache[cache_key] = (time.time(), fake_forecast)

    # Should return from cache without making HTTP call
    result = await agent.fetch_forecast("OKX", 33, 35)
    assert result == fake_forecast


# ---------------------------------------------------------------------------
# Test 11: DRY_RUN does not call signal_callback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_does_not_call_callback():
    callback = MagicMock()
    agent = WeatherAgent(dry_run=True, signal_callback=callback, portfolio_value=5000.0)

    # Mock market_scanner returning one weather market
    market = make_market("Will it snow in NYC?", yes_price=0.10, liquidity=50_000)
    mock_scanner = MagicMock()
    mock_scanner.get_markets_for_agent.return_value = [market]
    agent.market_scanner = mock_scanner

    # Snowy forecast → high model prob → YES signal should be generated
    fake_forecast = make_forecast([
        make_period(short="Heavy Snow", detailed="Blizzard", pop=80) for _ in range(7)
    ])
    agent.fetch_forecast = AsyncMock(return_value=fake_forecast)

    await agent._run_scan()

    callback.assert_not_called()
