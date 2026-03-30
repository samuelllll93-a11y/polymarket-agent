"""
Weather agent: translates NOAA forecast data into Polymarket trade signals.

Polls NOAA's public weather API for temperature, precipitation, and storm
forecasts, then compares them against current Polymarket odds for weather-
related markets (e.g., "Will it snow in NYC in January?"). Emits a BUY or
SELL signal when the model's estimated probability diverges from the market
price by more than the configured edge threshold.
"""

import asyncio
from typing import Optional


NOAA_API_BASE = "https://api.weather.gov"


class WeatherAgent:
    """Fetches NOAA data and generates signals for weather prediction markets."""

    def __init__(self, market_scanner, order_manager, dry_run: bool = True):
        self.market_scanner = market_scanner
        self.order_manager = order_manager
        self.dry_run = dry_run

    async def fetch_forecast(self, office: str, grid_x: int, grid_y: int) -> dict:
        """Retrieve a gridpoint forecast from the NOAA API."""
        raise NotImplementedError

    def estimate_probability(self, forecast: dict, market: dict) -> Optional[float]:
        """
        Convert a NOAA forecast into a probability estimate for a given market.

        Returns:
            A float in [0, 1] representing estimated YES probability, or None
            if the forecast is not applicable to this market.
        """
        raise NotImplementedError

    async def run(self) -> None:
        """Main loop: fetch forecasts, evaluate markets, emit signals."""
        raise NotImplementedError
