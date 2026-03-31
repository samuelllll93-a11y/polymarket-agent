"""
agents/btc_agent.py — BTC Price Market Agent

Connects to the Binance public WebSocket for live BTC/USDT prices and
generates trading signals for Polymarket BTC price prediction markets.

Strategy:
  1. Connect to Binance wss://stream.binance.com:9443/ws/btcusdt@aggTrade
  2. Maintain a rolling price window (last 60 seconds of ticks)
  3. Scan Polymarket for active BTC price markets (e.g. "Will BTC > $X by Y?")
  4. Parse the threshold price and expiry from each market's question
  5. Estimate fair probability using current Binance price vs market threshold
  6. Identify markets where Polymarket price diverges from fair value by > 3%
  7. Emit a Signal for each identified opportunity
  8. In DRY_RUN: log signals, never place orders

Signal: encapsulates market_id, side, fair_value, market_price, edge, confidence.

Error handling:
  - WebSocket disconnections: exponential backoff reconnect
  - JSON parse errors: logged and skipped
  - Market parse errors: logged and skipped (not every market title is parseable)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosedError, WebSocketException

import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signal dataclass
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    """A trading signal emitted by an agent."""
    market_id: str
    market_question: str
    side: str                   # "YES" or "NO"
    fair_value: float           # Bot's estimated probability (0-1)
    market_price: float         # Current Polymarket price (0-1)
    edge: float                 # fair_value - market_price (positive = buy YES)
    confidence: float           # 0-1 confidence in the fair_value estimate
    agent: str = "btc"
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self) -> str:
        return (
            f"Signal({self.agent} | {self.side} {self.market_id[:12]}... | "
            f"fair={self.fair_value:.4f} mkt={self.market_price:.4f} "
            f"edge={self.edge:+.4f} conf={self.confidence:.2f})"
        )


# ---------------------------------------------------------------------------
# BTCAgent
# ---------------------------------------------------------------------------

class BTCAgent:
    """
    Streams Binance BTC/USDT prices and generates signals for BTC
    prediction markets on Polymarket.

    Usage:
        agent = BTCAgent(clob_client=client, signal_callback=my_handler)
        await agent.run()
    """

    # Regex patterns to parse BTC market questions
    _PRICE_PATTERNS = [
        # "Will BTC exceed $105,000 by March 31?"
        re.compile(
            r"(?:BTC|Bitcoin).*?\$([0-9,]+(?:\.[0-9]+)?)[kK]?",
            re.IGNORECASE,
        ),
        # "BTC above 100000"
        re.compile(r"(?:above|exceed|over|reach|hit)\s+\$?([0-9,]+(?:\.[0-9]+)?)[kK]?", re.IGNORECASE),
    ]

    def __init__(
        self,
        clob_client: Any = None,
        signal_callback: Optional[Callable[[Signal], Any]] = None,
        dry_run: bool = config.DRY_RUN,
        price_history_seconds: int = 60,
    ):
        self.clob_client = clob_client
        self.signal_callback = signal_callback
        self.dry_run = dry_run

        # Rolling price window
        self._price_history: deque[tuple[float, float]] = deque()  # (timestamp, price)
        self._price_history_seconds = price_history_seconds
        self._latest_price: Optional[float] = None
        self._price_update_count: int = 0

        # State
        self._running: bool = False
        self._ws_connected: bool = False
        self._connect_attempts: int = 0
        self._last_market_scan: float = 0.0
        self._btc_markets: list[dict] = []

        logger.info(
            "BTCAgent initialised | dry_run=%s | ws_url=%s",
            self.dry_run,
            config.BINANCE_WS_URL,
        )

    # ------------------------------------------------------------------
    # WebSocket Connection
    # ------------------------------------------------------------------

    async def _connect_binance(self) -> None:
        """Connect to Binance aggTrade stream with reconnect logic."""
        stream = "btcusdt@aggTrade"
        url = f"{config.BINANCE_WS_URL}/{stream}"

        while self._running:
            try:
                self._connect_attempts += 1
                logger.info(
                    "Connecting to Binance WebSocket (attempt %d): %s",
                    self._connect_attempts,
                    url,
                )
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws_connected = True
                    self._connect_attempts = 0  # reset on success
                    logger.info("Binance WebSocket connected")

                    async for raw_message in ws:
                        if not self._running:
                            break
                        await self._handle_message(raw_message)

            except ConnectionClosedError as exc:
                self._ws_connected = False
                logger.warning("Binance WebSocket closed: %s", exc)
            except WebSocketException as exc:
                self._ws_connected = False
                logger.error("Binance WebSocket error: %s", exc)
            except (OSError, asyncio.TimeoutError) as exc:
                self._ws_connected = False
                logger.error("Binance connection failed: %s", exc)

            if not self._running:
                break

            # Exponential backoff reconnect
            attempts = min(self._connect_attempts, config.BINANCE_RECONNECT_ATTEMPTS)
            delay = config.BINANCE_RECONNECT_DELAY * (2 ** (attempts - 1))
            delay = min(delay, 60)  # cap at 60s
            logger.info("Reconnecting in %.0fs...", delay)
            await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Message Handling
    # ------------------------------------------------------------------

    async def _handle_message(self, raw: str) -> None:
        """Parse a Binance aggTrade WebSocket message."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.debug("Failed to parse Binance message: %s", exc)
            return

        # aggTrade message format: {"e":"aggTrade","p":"<price>","T":"<timestamp>"}
        if data.get("e") != "aggTrade":
            return

        try:
            price = float(data["p"])
            timestamp = float(data["T"]) / 1000.0  # ms → seconds
        except (KeyError, ValueError) as exc:
            logger.debug("Malformed aggTrade message: %s", exc)
            return

        await self.on_price_tick(price, timestamp)

    async def on_price_tick(self, price: float, timestamp: float) -> None:
        """
        Handle an incoming price tick.

        Updates price history, trims old entries, and periodically
        triggers signal evaluation.
        """
        self._latest_price = price
        self._price_history.append((timestamp, price))
        self._price_update_count += 1

        # Trim history older than the window
        cutoff = time.time() - self._price_history_seconds
        while self._price_history and self._price_history[0][0] < cutoff:
            self._price_history.popleft()

        # Re-scan markets every MARKET_SCAN_INTERVAL_SEC
        now = time.time()
        if now - self._last_market_scan >= config.MARKET_SCAN_INTERVAL_SEC:
            self._last_market_scan = now
            await self._scan_and_signal()

    # ------------------------------------------------------------------
    # Market Scanning & Signal Generation
    # ------------------------------------------------------------------

    async def _refresh_btc_markets(self) -> None:
        """Fetch BTC-related markets from Polymarket via CLOBClient."""
        if self.clob_client is None:
            logger.debug("No CLOB client — skipping market refresh")
            return

        try:
            result = await self.clob_client.get_markets(limit=200)
            # Gamma API may return a list directly or {"data": [...]}
            if isinstance(result, list):
                all_markets = result
            else:
                all_markets = result.get("data", [])
            self._btc_markets = [
                m for m in all_markets
                if self._is_btc_market(m)
            ]
            logger.info(
                "Found %d BTC markets (from %d total)",
                len(self._btc_markets),
                len(all_markets),
            )
        except Exception as exc:
            logger.error("Failed to refresh BTC markets: %s", exc)

    def _is_btc_market(self, market: dict) -> bool:
        """Return True if the market question is about BTC price."""
        question = market.get("question", "").lower()
        return any(kw in question for kw in ["btc", "bitcoin"])

    async def _scan_and_signal(self) -> None:
        """
        Evaluate all BTC markets and emit signals for any with sufficient edge.
        """
        if self._latest_price is None:
            return

        await self._refresh_btc_markets()

        for market in self._btc_markets:
            signal = self._evaluate_market(market)
            if signal is None:
                continue
            if abs(signal.edge) < config.MIN_KELLY_EDGE:
                continue

            logger.info("Signal generated: %s", signal)

            if self.dry_run:
                logger.info(
                    "DRY_RUN: signal suppressed (not routing to order manager): %s",
                    signal,
                )
            elif self.signal_callback:
                try:
                    result = self.signal_callback(signal)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    logger.error("Signal callback error: %s", exc)

    def _evaluate_market(self, market: dict) -> Optional[Signal]:
        """
        Evaluate a single market and return a Signal if there is edge.

        Returns None if the market can't be parsed or has no edge.
        """
        question = market.get("question", "")
        market_id = market.get("condition_id", market.get("id", "unknown"))

        # Parse threshold price from question
        threshold = self._parse_threshold(question)
        if threshold is None:
            return None

        # Parse expiry
        expiry_dt = self._parse_expiry(market)
        if expiry_dt is None:
            return None

        # Filter on time-to-expiry
        now = datetime.now(timezone.utc)
        hours_to_expiry = (expiry_dt - now).total_seconds() / 3600
        if hours_to_expiry < config.MIN_TIME_TO_EXPIRY_HOURS:
            return None
        if hours_to_expiry > config.MAX_TIME_TO_EXPIRY_DAYS * 24:
            return None

        # Estimate fair probability
        fair_value = self.estimate_probability(threshold, hours_to_expiry)
        if fair_value is None:
            return None

        # Get market's current best price (mid of bid/ask)
        market_price = self._get_market_mid(market)
        if market_price is None or market_price <= 0 or market_price >= 1:
            return None

        edge = fair_value - market_price
        side = "YES" if edge > 0 else "NO"

        # Confidence scales with price history depth
        confidence = min(len(self._price_history) / 60.0, 1.0)

        return Signal(
            market_id=market_id,
            market_question=question,
            side=side,
            fair_value=round(fair_value, 4),
            market_price=round(market_price, 4),
            edge=round(edge, 4),
            confidence=round(confidence, 3),
        )

    def estimate_probability(
        self,
        threshold_usd: float,
        hours_to_expiry: float,
    ) -> Optional[float]:
        """
        Estimate the probability that BTC exceeds `threshold_usd` by expiry.

        Uses a simple log-normal model based on current price and historical
        30-day BTC volatility (~3.5% daily).

        Args:
            threshold_usd:    Target BTC price.
            hours_to_expiry:  Time remaining until market resolves.

        Returns:
            Probability (0-1) or None if current price is unavailable.
        """
        import math

        if self._latest_price is None or self._latest_price <= 0:
            return None
        if threshold_usd <= 0:
            return None

        current_price = self._latest_price

        # BTC daily log-volatility (conservative estimate ~3.5% daily)
        DAILY_VOL = 0.035
        t_days = hours_to_expiry / 24.0

        if t_days <= 0:
            # Already at expiry
            return 1.0 if current_price >= threshold_usd else 0.0

        vol_t = DAILY_VOL * math.sqrt(t_days)

        # Log-normal: P(S_T >= K) = N(d2) where
        # d2 = (ln(S/K) + (mu - 0.5*sigma^2)*t) / (sigma*sqrt(t))
        # Using zero drift (conservative) for prediction markets
        try:
            d2 = math.log(current_price / threshold_usd) / vol_t
        except (ValueError, ZeroDivisionError):
            return None

        # Standard normal CDF approximation (Abramowitz & Stegun)
        prob = _norm_cdf(d2)
        return max(0.01, min(0.99, prob))  # clamp away from 0/1

    def _parse_threshold(self, question: str) -> Optional[float]:
        """Extract the BTC price threshold from a market question string."""
        for pattern in self._PRICE_PATTERNS:
            match = pattern.search(question)
            if match:
                raw = match.group(1).replace(",", "")
                try:
                    value = float(raw)
                    # Handle 'k' suffix (e.g. "100k")
                    if "k" in question[match.start():match.end()].lower():
                        value *= 1000
                    return value
                except ValueError:
                    continue
        return None

    def _parse_expiry(self, market: dict) -> Optional[datetime]:
        """Extract market expiry datetime from market data."""
        from dateutil import parser as dateutil_parser

        for key in ("end_date_iso", "end_date", "endDate", "expiration"):
            raw = market.get(key)
            if raw:
                try:
                    dt = dateutil_parser.parse(str(raw))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt
                except (ValueError, TypeError):
                    continue
        return None

    def _get_market_mid(self, market: dict) -> Optional[float]:
        """Get the mid-price for the YES token from market data."""
        # Some endpoints return best_bid/best_ask directly
        bid = market.get("best_bid") or market.get("bestBid")
        ask = market.get("best_ask") or market.get("bestAsk")
        if bid and ask:
            try:
                return (float(bid) + float(ask)) / 2.0
            except (ValueError, TypeError):
                pass

        # Fall back to last_trade_price
        ltp = market.get("last_trade_price") or market.get("lastTradePrice")
        if ltp:
            try:
                return float(ltp)
            except (ValueError, TypeError):
                pass

        return None

    # ------------------------------------------------------------------
    # Main Run Loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Main agent loop.

        Starts the Binance WebSocket feed. Runs until self._running is False.
        """
        self._running = True
        logger.info("BTCAgent starting | DRY_RUN=%s", self.dry_run)

        try:
            await self._connect_binance()
        except asyncio.CancelledError:
            logger.info("BTCAgent cancelled")
        finally:
            self._running = False
            logger.info(
                "BTCAgent stopped | price_ticks_received=%d",
                self._price_update_count,
            )

    async def stop(self) -> None:
        """Signal the agent to stop gracefully."""
        self._running = False
        logger.info("BTCAgent stop requested")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Return agent status for monitoring."""
        return {
            "agent": "btc",
            "running": self._running,
            "ws_connected": self._ws_connected,
            "latest_price": self._latest_price,
            "price_history_len": len(self._price_history),
            "btc_markets_tracked": len(self._btc_markets),
            "connect_attempts": self._connect_attempts,
            "price_ticks": self._price_update_count,
        }


# ---------------------------------------------------------------------------
# Math helper
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    """
    Standard normal CDF approximation.
    Accurate to ~7 decimal places (Abramowitz & Stegun 26.2.17).
    """
    import math
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = t * (0.319381530
                + t * (-0.356563782
                       + t * (1.781477937
                              + t * (-1.821255978
                                     + t * 1.330274429))))
    prob = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x) * poly
    return prob if x >= 0 else 1.0 - prob
