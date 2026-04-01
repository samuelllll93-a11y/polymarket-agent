"""
agents/weather_agent.py — NOAA Weather Signal Agent

Polls NOAA's public weather API (api.weather.gov, no API key required) for
7-day gridpoint forecasts, then compares modelled probabilities against open
Polymarket weather markets.

Strategy:
  - Scan market_scanner for markets in the 'weather' category
  - Match market question keywords to a NOAA location + condition
  - Query NOAA /gridpoints/{office}/{x},{y}/forecast for the location
  - Derive a probability estimate from the forecast periods
  - Emit a BUY signal when |estimate - market_price| > WEATHER_MIN_EDGE

Supported conditions (keyword-matched from market question):
  - "hurricane" / "tropical storm"
  - "snow" / "snowfall"
  - "temperature above/below X"
  - "rain" / "rainfall" / "precipitation"

DRY_RUN=True always — all signals are logged but no orders placed.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Callable, Any

import aiohttp

import config
from core.market_scanner import Market

logger = logging.getLogger(__name__)

NOAA_API_BASE = "https://api.weather.gov"


# ---------------------------------------------------------------------------
# WeatherSignal dataclass
# ---------------------------------------------------------------------------

@dataclass
class WeatherSignal:
    """A trade signal derived from a NOAA forecast vs. Polymarket odds."""

    market_question: str
    condition_id: str
    side: str                    # 'YES' or 'NO'
    market_price: float          # Current Polymarket YES price
    model_probability: float     # NOAA-derived probability estimate
    edge: float                  # |model - market| - fee
    size_usd: float
    location: str
    forecast_summary: str
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self) -> str:
        return (
            f"WeatherSignal({self.side} {self.condition_id[:16]} | "
            f"market={self.market_price:.3f} model={self.model_probability:.3f} "
            f"edge={self.edge:.2%} | ${self.size_usd:.2f})"
        )


# ---------------------------------------------------------------------------
# WeatherAgent
# ---------------------------------------------------------------------------

class WeatherAgent:
    """
    Fetches NOAA gridpoint forecasts and generates signals for weather
    prediction markets on Polymarket.

    Usage:
        scanner = MarketScanner(...)
        agent = WeatherAgent(market_scanner=scanner, signal_callback=router.handle_signal)
        await agent.run()
    """

    FEE_RATE: float = 0.02
    MIN_EDGE: float = config.WEATHER_MIN_EDGE
    MIN_LIQUIDITY: float = config.WEATHER_MIN_LIQUIDITY
    SCAN_INTERVAL: int = config.WEATHER_SCAN_INTERVAL

    def __init__(
        self,
        market_scanner=None,
        signal_callback: Optional[Callable[[WeatherSignal], Any]] = None,
        dry_run: bool = config.DRY_RUN,
        portfolio_value: float = config.TOTAL_CAPITAL_USD,
    ):
        self.market_scanner = market_scanner
        self.signal_callback = signal_callback
        self.dry_run = dry_run
        self.portfolio_value = portfolio_value

        self._running: bool = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._signals_found: int = 0
        self._scans_completed: int = 0
        # Cache forecasts: location -> (timestamp, forecast_data)
        self._forecast_cache: dict[str, tuple[float, dict]] = {}
        self._cache_ttl: int = 3600  # NOAA forecasts update hourly

        logger.info(
            "WeatherAgent initialised | dry_run=%s | min_edge=%.1f%% | scan_interval=%ds",
            self.dry_run,
            self.MIN_EDGE * 100,
            self.SCAN_INTERVAL,
        )

    # ------------------------------------------------------------------
    # HTTP Session
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15, connect=5),
                headers={
                    "User-Agent": "polymarket-bot/1.0 (contact: bot@example.com)",
                    "Accept": "application/geo+json",
                },
            )
        return self._session

    async def _close_session(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # NOAA API
    # ------------------------------------------------------------------

    async def fetch_forecast(self, office: str, grid_x: int, grid_y: int) -> dict:
        """
        Retrieve a 7-day gridpoint forecast from the NOAA Weather API.

        Args:
            office:  NWS forecast office code (e.g., "OKX" for NYC).
            grid_x:  Grid X coordinate.
            grid_y:  Grid Y coordinate.

        Returns:
            Parsed forecast JSON (GeoJSON Feature), or empty dict on error.
        """
        cache_key = f"{office}/{grid_x},{grid_y}"
        cached_at, cached_data = self._forecast_cache.get(cache_key, (0.0, {}))
        if cached_data and (time.time() - cached_at) < self._cache_ttl:
            logger.debug("WeatherAgent: cache hit for %s", cache_key)
            return cached_data

        url = f"{NOAA_API_BASE}/gridpoints/{office}/{grid_x},{grid_y}/forecast"
        session = await self._get_session()
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    self._forecast_cache[cache_key] = (time.time(), data)
                    logger.info("WeatherAgent: fetched forecast for %s", cache_key)
                    return data
                else:
                    logger.warning(
                        "WeatherAgent: NOAA API HTTP %d for %s", resp.status, cache_key
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error("WeatherAgent: NOAA API error for %s: %s", cache_key, exc)
        return {}

    # ------------------------------------------------------------------
    # Probability Estimation
    # ------------------------------------------------------------------

    def estimate_probability(self, forecast: dict, market: dict) -> Optional[float]:
        """
        Convert a NOAA forecast into a probability estimate for a Polymarket
        weather market.

        Looks at forecast periods and matches conditions to the market question
        using keyword heuristics.

        Args:
            forecast:  NOAA GeoJSON forecast response.
            market:    Market dict with at least 'question' key.

        Returns:
            Float in [0, 1] as estimated YES probability, or None if not applicable.
        """
        periods = forecast.get("properties", {}).get("periods", [])
        if not periods:
            return None

        question = market.get("question", "").lower()

        # --- Snow / precipitation detection ---
        if any(kw in question for kw in ["snow", "snowfall", "blizzard"]):
            return self._estimate_snow_probability(periods, question)

        if any(kw in question for kw in ["rain", "rainfall", "precipitation", "flood"]):
            return self._estimate_rain_probability(periods, question)

        if any(kw in question for kw in ["hurricane", "tropical storm", "cyclone"]):
            return self._estimate_storm_probability(periods, question)

        if any(kw in question for kw in ["temperature", "temp", "degrees", "high", "heat"]):
            return self._estimate_temperature_probability(periods, question)

        return None

    def _estimate_snow_probability(self, periods: list[dict], question: str) -> float:
        """Estimate probability of significant snow from forecast periods."""
        snow_mentions = 0
        total_periods = min(len(periods), 14)  # One week of periods

        for period in periods[:total_periods]:
            forecast_text = (period.get("detailedForecast", "") + " " +
                             period.get("shortForecast", "")).lower()
            if any(kw in forecast_text for kw in ["snow", "blizzard", "winter storm", "flurries"]):
                snow_mentions += 1

        base_prob = snow_mentions / max(total_periods, 1)

        # Extract probability of precipitation if available from last period
        pop = periods[-1].get("probabilityOfPrecipitation", {})
        if isinstance(pop, dict) and pop.get("value") is not None:
            pop_val = float(pop["value"]) / 100.0
            # Weight: 60% forecast-text based, 40% PoP
            return round(base_prob * 0.6 + pop_val * 0.4, 3)

        return round(min(base_prob * 1.5, 0.95), 3)

    def _estimate_rain_probability(self, periods: list[dict], question: str) -> float:
        """Estimate probability of significant rainfall."""
        rain_pop_values = []

        for period in periods[:14]:
            pop = period.get("probabilityOfPrecipitation", {})
            if isinstance(pop, dict) and pop.get("value") is not None:
                rain_pop_values.append(float(pop["value"]) / 100.0)
            else:
                forecast_text = (period.get("shortForecast", "") + " " +
                                 period.get("detailedForecast", "")).lower()
                if any(kw in forecast_text for kw in ["rain", "shower", "thunderstorm"]):
                    rain_pop_values.append(0.6)
                else:
                    rain_pop_values.append(0.05)

        if not rain_pop_values:
            return 0.3  # Prior

        # For "will it rain at all" style questions: P(at least one rainy period)
        prob_no_rain = 1.0
        for p in rain_pop_values:
            prob_no_rain *= (1.0 - p)
        return round(min(1.0 - prob_no_rain, 0.95), 3)

    def _estimate_storm_probability(self, periods: list[dict], question: str) -> float:
        """Estimate hurricane / tropical storm probability (rare events, low prior)."""
        for period in periods[:14]:
            text = (period.get("shortForecast", "") + " " +
                    period.get("detailedForecast", "")).lower()
            if any(kw in text for kw in ["hurricane", "tropical storm", "cyclone", "typhoon"]):
                return 0.55
        return 0.05  # Very unlikely if not in 7-day forecast

    def _estimate_temperature_probability(self, periods: list[dict], question: str) -> Optional[float]:
        """
        Estimate probability that temperature exceeds / falls below a threshold.

        Extracts temperature values from forecast periods and computes the
        fraction of periods where the condition holds.
        """
        # Try to extract temperature threshold from question
        # e.g. "Will NYC hit 90°F in July?" or "temperature above 95"
        threshold = None
        above = True
        m = re.search(r"(above|below|over|under|exceed)\s+(\d+)", question)
        if m:
            above = m.group(1) in ("above", "over", "exceed")
            threshold = float(m.group(2))

        if threshold is None:
            return None  # Can't determine without a threshold

        qualifying = 0
        total = 0
        for period in periods[:14]:
            temp = period.get("temperature")
            if temp is not None:
                total += 1
                temp_val = float(temp)
                if above and temp_val >= threshold:
                    qualifying += 1
                elif not above and temp_val <= threshold:
                    qualifying += 1

        if total == 0:
            return 0.3  # Uninformative prior

        return round(qualifying / total, 3)

    # ------------------------------------------------------------------
    # Market Matching
    # ------------------------------------------------------------------

    def match_location(self, question: str) -> Optional[tuple]:
        """
        Match a market question to a NOAA location.

        Returns:
            (location_name, office, grid_x, grid_y) or None if no match.
        """
        question_lower = question.lower()
        for location, (office, gx, gy) in config.NOAA_LOCATIONS.items():
            if location.lower() in question_lower:
                return (location, office, gx, gy)
        return None

    # ------------------------------------------------------------------
    # Signal Generation
    # ------------------------------------------------------------------

    def evaluate_market(
        self,
        market: Market,
        forecast: dict,
        location: str,
    ) -> Optional[WeatherSignal]:
        """
        Evaluate a single weather market against a NOAA forecast.

        Returns a WeatherSignal if edge > MIN_EDGE, else None.
        """
        market_dict = {"question": market.question}
        model_prob = self.estimate_probability(forecast, market_dict)
        if model_prob is None:
            return None

        market_price = market.yes_price
        raw_edge = abs(model_prob - market_price)
        edge = raw_edge - self.FEE_RATE
        if edge < self.MIN_EDGE:
            return None

        if market.liquidity < self.MIN_LIQUIDITY:
            return None

        # Determine side: buy YES if model > market, buy NO if model < market
        side = "YES" if model_prob > market_price else "NO"

        # Size: 1% of portfolio, capped by liquidity
        size_usd = min(
            self.portfolio_value * 0.01,
            market.liquidity / 20.0,
        )

        # Get a human-readable forecast summary
        periods = forecast.get("properties", {}).get("periods", [])
        summary = periods[0].get("shortForecast", "N/A") if periods else "N/A"

        return WeatherSignal(
            market_question=market.question,
            condition_id=market.condition_id,
            side=side,
            market_price=market_price,
            model_probability=model_prob,
            edge=round(edge, 4),
            size_usd=round(size_usd, 2),
            location=location,
            forecast_summary=summary,
        )

    # ------------------------------------------------------------------
    # Run Loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main scan loop. Runs every SCAN_INTERVAL seconds."""
        self._running = True
        logger.info(
            "WeatherAgent starting | DRY_RUN=%s | scan_interval=%ds",
            self.dry_run,
            self.SCAN_INTERVAL,
        )
        try:
            while self._running:
                scan_start = time.monotonic()
                await self._run_scan()
                elapsed = time.monotonic() - scan_start
                await asyncio.sleep(max(0, self.SCAN_INTERVAL - elapsed))
        except asyncio.CancelledError:
            logger.info("WeatherAgent cancelled")
        finally:
            self._running = False
            await self._close_session()
            logger.info(
                "WeatherAgent stopped | scans=%d | signals=%d",
                self._scans_completed,
                self._signals_found,
            )

    async def _run_scan(self) -> None:
        """Execute one full weather scan cycle."""
        try:
            # Get weather markets from scanner
            if self.market_scanner is None:
                logger.warning("WeatherAgent: no market_scanner configured")
                return

            markets = self.market_scanner.get_markets_for_agent("weather")
            if not markets:
                logger.info("WeatherAgent: no weather markets found in scanner cache")
                self._scans_completed += 1
                return

            signals_this_scan = 0
            for market in markets:
                loc_info = self.match_location(market.question)
                if loc_info is None:
                    continue

                location, office, gx, gy = loc_info
                forecast = await self.fetch_forecast(office, gx, gy)
                if not forecast:
                    continue

                signal = self.evaluate_market(market, forecast, location)
                if signal:
                    signals_this_scan += 1
                    self._signals_found += 1
                    logger.info("WeatherAgent signal: %s", signal)
                    if not self.dry_run and self.signal_callback:
                        try:
                            result = self.signal_callback(signal)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception as exc:
                            logger.error("WeatherAgent callback error: %s", exc)

            self._scans_completed += 1
            logger.info(
                "WeatherAgent scan #%d: %d signal(s) from %d weather markets",
                self._scans_completed,
                signals_this_scan,
                len(markets),
            )
        except Exception as exc:
            logger.error("WeatherAgent scan error: %s", exc)

    async def stop(self) -> None:
        """Gracefully stop the agent."""
        self._running = False

    def get_status(self) -> dict:
        return {
            "agent": "weather",
            "running": self._running,
            "dry_run": self.dry_run,
            "scans_completed": self._scans_completed,
            "signals_found": self._signals_found,
        }
