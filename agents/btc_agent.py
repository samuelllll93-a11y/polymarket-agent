"""
BTC agent: streams Binance WebSocket price data to inform BTC prediction markets.

Connects to the Binance public WebSocket feed for BTC/USDT and maintains a
rolling price window. Compares live price trajectory against Polymarket BTC
price markets (e.g., "Will BTC exceed $100k by end of month?") and emits
signals when the model's estimate diverges from market odds significantly.

STUB - full implementation built in a later task.
"""

import asyncio
import os
from typing import Optional


BINANCE_WS_URL = os.getenv("BINANCE_WS_URL", "wss://stream.binance.com:9443/ws")


class BTCAgent:
    """Streams Binance BTC prices and generates signals for BTC prediction markets."""

    def __init__(self, market_scanner, order_manager, dry_run: bool = True):
        self.market_scanner = market_scanner
        self.order_manager = order_manager
        self.dry_run = dry_run
        self._ws = None
        self._price_history: list[float] = []

    async def connect(self) -> None:
        """Open the Binance WebSocket connection for btcusdt@trade stream."""
        raise NotImplementedError

    async def on_price_tick(self, price: float, timestamp: int) -> None:
        """Handle an incoming price tick: update history and re-evaluate signals."""
        raise NotImplementedError

    def estimate_probability(self, market: dict) -> Optional[float]:
        """Estimate YES probability for a BTC market given current price history."""
        raise NotImplementedError

    async def run(self) -> None:
        """Main loop: connect to Binance, stream prices, emit trade signals."""
        raise NotImplementedError
