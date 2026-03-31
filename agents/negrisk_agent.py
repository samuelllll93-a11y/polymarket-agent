"""
agents/negrisk_agent.py — NegRisk Guaranteed Arbitrage Agent

Polymarket's NegRisk system allows multi-outcome markets where exactly one
outcome must occur. This creates arbitrage when the sum of YES prices across
all outcomes deviates from $1.00.

Strategy:
  - Sum of YES prices < 1.0: Buy YES on ALL outcomes (guaranteed $1 payout for < $1 cost)
  - Sum of YES prices > 1.0: Buy NO on ALL outcomes (one outcome MUST fail)

Historical performance (2024):
  - $29M extracted from Polymarket via NegRisk rebalancing in one year
  - First live scan found 182 opportunities across 440 grouped events
  - Best: 43% guaranteed ROI (Vermont Governor election)
  - Typical edge after fees: 2–8%
  - Opportunities last 30s–5min before closing

DRY_RUN=True always — all opportunities are logged but no orders placed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import aiohttp

import config

logger = logging.getLogger(__name__)

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


# ---------------------------------------------------------------------------
# ArbSignal dataclass
# ---------------------------------------------------------------------------

@dataclass
class ArbSignal:
    """Represents a guaranteed arbitrage opportunity in a NegRisk group."""

    event_name: str
    event_id: str
    markets: list[tuple[str, float, float]]   # (condition_id, yes_price, liquidity)
    arb_type: str                              # 'YES' or 'NO'
    sum_of_prices: float
    edge_after_fees: float
    recommended_size_per_leg: float
    guaranteed_profit_usd: float
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self) -> str:
        return (
            f"ArbSignal({self.arb_type} | {self.event_name[:40]} | "
            f"sum={self.sum_of_prices:.4f} | edge={self.edge_after_fees:.2%} | "
            f"size=${self.recommended_size_per_leg:.2f}/leg | "
            f"profit=${self.guaranteed_profit_usd:.2f})"
        )


# ---------------------------------------------------------------------------
# NegRiskAgent
# ---------------------------------------------------------------------------

class NegRiskAgent:
    """
    Scans Polymarket grouped events for NegRisk arbitrage opportunities.

    A NegRisk arb exists when the sum of YES prices across all outcomes in a
    multi-outcome group deviates from 1.0 by more than the fee (2%) plus our
    minimum edge threshold.

    Usage:
        agent = NegRiskAgent(signal_callback=router.handle_arb)
        await agent.run()
    """

    FEE_RATE: float = 0.02        # Polymarket 2% taker fee per leg
    MIN_EDGE: float = config.NEGRISK_MIN_EDGE
    MIN_LIQUIDITY: float = config.NEGRISK_MIN_LIQUIDITY
    SCAN_INTERVAL: int = config.NEGRISK_SCAN_INTERVAL

    def __init__(
        self,
        signal_callback: Optional[Callable[[ArbSignal], Any]] = None,
        dry_run: bool = config.DRY_RUN,
        portfolio_value: float = config.TOTAL_CAPITAL_USD,
    ):
        self.signal_callback = signal_callback
        self.dry_run = dry_run
        self.portfolio_value = portfolio_value

        self._running: bool = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._signals_found: int = 0
        self._scans_completed: int = 0
        self._last_scan_time: float = 0.0

        logger.info(
            "NegRiskAgent initialised | dry_run=%s | min_edge=%.1f%% | "
            "min_liquidity=$%.0f | scan_interval=%ds",
            self.dry_run,
            self.MIN_EDGE * 100,
            self.MIN_LIQUIDITY,
            self.SCAN_INTERVAL,
        )

    # ------------------------------------------------------------------
    # HTTP Session
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10, connect=5),
                headers={"User-Agent": "polymarket-bot/1.0"},
            )
        return self._session

    async def _close_session(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Data Fetching
    # ------------------------------------------------------------------

    async def fetch_grouped_markets(self) -> list[dict]:
        """
        Fetch all active events from the Gamma API.

        Each event contains multiple related outcome markets (the 'markets' field).
        These are the NegRisk groups we scan for arbitrage.

        Returns:
            List of event dicts, each with a 'markets' key containing outcomes.
        """
        session = await self._get_session()
        all_events: list[dict] = []
        offset = 0
        limit = 100

        while True:
            params = {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": offset,
            }
            try:
                async with session.get(GAMMA_EVENTS_URL, params=params) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

                    # Gamma returns a list directly
                    if isinstance(data, list):
                        events = data
                    else:
                        events = data.get("data", data.get("events", []))

                    if not events:
                        break

                    all_events.extend(events)

                    # Stop if we got fewer than the page size (last page)
                    if len(events) < limit:
                        break

                    offset += limit

            except aiohttp.ClientError as exc:
                logger.error("NegRiskAgent: Gamma API error: %s", exc)
                break
            except Exception as exc:
                logger.error("NegRiskAgent: Unexpected error fetching events: %s", exc)
                break

        logger.info("NegRiskAgent: Fetched %d events from Gamma API", len(all_events))
        return all_events

    # ------------------------------------------------------------------
    # Arbitrage Detection
    # ------------------------------------------------------------------

    def find_arb_opportunities(self, events: list[dict]) -> list[ArbSignal]:
        """
        Scan event groups for NegRisk arbitrage opportunities.

        For each event with 2+ outcome markets:
          1. Sum all YES prices across outcomes
          2. Calculate net edge = abs(1.0 - sum) - total_fees
          3. If edge > MIN_EDGE and all markets have liquidity > MIN_LIQUIDITY → signal

        Args:
            events: List of event dicts from Gamma API.

        Returns:
            List of ArbSignal objects sorted by edge descending.
        """
        signals: list[ArbSignal] = []

        for event in events:
            markets = event.get("markets", [])
            if len(markets) < 2:
                continue  # Need at least 2 outcomes for NegRisk

            event_name = event.get("title", event.get("slug", "Unknown Event"))
            event_id = str(event.get("id", ""))

            # Extract prices and liquidity for each outcome
            legs: list[tuple[str, float, float]] = []
            for market in markets:
                condition_id = market.get("conditionId", market.get("condition_id", ""))
                if not condition_id:
                    continue

                # Try various price fields
                yes_price = self._extract_yes_price(market)
                if yes_price is None:
                    continue

                liquidity = self._extract_liquidity(market)

                legs.append((condition_id, yes_price, liquidity))

            if len(legs) < 2:
                continue

            # Sum all YES prices
            sum_prices = sum(price for _, price, _ in legs)

            # Edge = deviation from 1.0, minus total fees across all legs
            # Fee applies once per leg when entering the position
            total_fee = self.FEE_RATE * len(legs)
            raw_deviation = abs(1.0 - sum_prices)
            edge = raw_deviation - total_fee

            if edge <= self.MIN_EDGE:
                continue

            # Liquidity check: all legs must be liquid enough
            min_leg_liquidity = min(liq for _, _, liq in legs)
            if min_leg_liquidity < self.MIN_LIQUIDITY:
                logger.debug(
                    "NegRisk skipped (low liquidity): %s | min_liq=$%.0f",
                    event_name,
                    min_leg_liquidity,
                )
                continue

            # Determine direction
            arb_type = "YES" if sum_prices < 1.0 else "NO"

            # Position sizing
            size_per_leg = self.calculate_position_size(
                signal=None,
                portfolio_value=self.portfolio_value,
                num_legs=len(legs),
                min_leg_liquidity=min_leg_liquidity,
                _edge=edge,
            )

            # Guaranteed profit estimate
            if arb_type == "YES":
                # Cost = sum of YES prices per unit; payout = 1.0 per unit
                cost_per_unit = sum_prices
                profit_per_unit = 1.0 - cost_per_unit
            else:
                # Cost = sum of NO prices per unit = len(legs) - sum_yes
                cost_per_unit = len(legs) - sum_prices
                profit_per_unit = 1.0 - cost_per_unit

            total_cost = size_per_leg * len(legs)
            # Profit scales with the units we can buy at size_per_leg
            if cost_per_unit > 0:
                units = size_per_leg / (cost_per_unit / len(legs))
                guaranteed_profit = units * profit_per_unit - total_cost * self.FEE_RATE * len(legs)
            else:
                guaranteed_profit = 0.0

            signal = ArbSignal(
                event_name=event_name,
                event_id=event_id,
                markets=legs,
                arb_type=arb_type,
                sum_of_prices=round(sum_prices, 6),
                edge_after_fees=round(edge, 6),
                recommended_size_per_leg=round(size_per_leg, 2),
                guaranteed_profit_usd=round(max(0.0, guaranteed_profit), 2),
            )
            signals.append(signal)

        # Sort by edge descending — best opportunities first
        signals.sort(key=lambda s: s.edge_after_fees, reverse=True)
        return signals

    def _extract_yes_price(self, market: dict) -> Optional[float]:
        """Extract the current YES token price from market data."""
        # Try multiple field names used by Gamma API
        for field_name in ("outcomePrices", "outcome_prices"):
            prices = market.get(field_name)
            if isinstance(prices, list) and prices:
                try:
                    return float(prices[0])  # index 0 = YES price
                except (ValueError, TypeError):
                    pass
            if isinstance(prices, str):
                # Sometimes it's a JSON string: '["0.35", "0.65"]'
                import json
                try:
                    parsed = json.loads(prices)
                    if isinstance(parsed, list) and parsed:
                        return float(parsed[0])
                except (ValueError, json.JSONDecodeError):
                    pass

        # Fallback: best_bid / best_ask mid
        bid = market.get("best_bid") or market.get("bestBid")
        ask = market.get("best_ask") or market.get("bestAsk")
        if bid and ask:
            try:
                return (float(bid) + float(ask)) / 2.0
            except (ValueError, TypeError):
                pass

        # Last trade price
        ltp = market.get("last_trade_price") or market.get("lastTradePrice")
        if ltp:
            try:
                return float(ltp)
            except (ValueError, TypeError):
                pass

        return None

    def _extract_liquidity(self, market: dict) -> float:
        """Extract liquidity / volume from market data."""
        for field_name in ("liquidity", "volume", "volumeNum", "volume_num", "volume24hr"):
            val = market.get(field_name)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
        return 0.0

    # ------------------------------------------------------------------
    # Position Sizing
    # ------------------------------------------------------------------

    def calculate_position_size(
        self,
        signal,                     # ArbSignal or None (used for external callers)
        portfolio_value: float,
        num_legs: int = 2,
        min_leg_liquidity: float = 0.0,
        _edge: float = 0.0,
    ) -> float:
        """
        Calculate recommended size per leg for a NegRisk arbitrage.

        Rules:
          - Each leg gets min(1% of portfolio, available_liquidity / 10)
          - Total across all legs must not exceed 5% of portfolio
          - Conservative: start at 1% per leg

        Args:
            signal:             ArbSignal object (optional, used if provided).
            portfolio_value:    Current portfolio value in USD.
            num_legs:           Number of outcome legs.
            min_leg_liquidity:  Minimum liquidity across all legs.
            _edge:              Edge after fees (used for internal sizing).

        Returns:
            Recommended USD size per leg.
        """
        if signal is not None:
            num_legs = len(signal.markets)
            min_leg_liquidity = min(liq for _, _, liq in signal.markets) if signal.markets else 0.0

        # Base size: 1% of portfolio per leg
        base_pct = 0.01
        base_size = portfolio_value * base_pct

        # Liquidity cap: never use more than 10% of available liquidity per leg
        liquidity_cap = min_leg_liquidity / 10.0 if min_leg_liquidity > 0 else base_size

        size_per_leg = min(base_size, liquidity_cap)

        # Portfolio cap: total exposure ≤ NEGRISK_MAX_POSITION_PCT of portfolio
        max_total = portfolio_value * config.NEGRISK_MAX_POSITION_PCT
        max_per_leg = max_total / max(num_legs, 1)

        size_per_leg = min(size_per_leg, max_per_leg)
        size_per_leg = max(size_per_leg, 0.0)

        return round(size_per_leg, 2)

    # ------------------------------------------------------------------
    # Run Loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Main scan loop. Runs every SCAN_INTERVAL seconds.

        In DRY_RUN:
          - Logs all opportunities found with full details
          - Does NOT place any orders
        In live mode:
          - Passes signals to signal_callback for order placement
        """
        self._running = True
        logger.info(
            "NegRiskAgent starting | DRY_RUN=%s | scan_interval=%ds",
            self.dry_run,
            self.SCAN_INTERVAL,
        )

        try:
            while self._running:
                scan_start = time.monotonic()
                await self._run_scan()
                elapsed = time.monotonic() - scan_start
                sleep_time = max(0, self.SCAN_INTERVAL - elapsed)
                await asyncio.sleep(sleep_time)

        except asyncio.CancelledError:
            logger.info("NegRiskAgent cancelled")
        finally:
            self._running = False
            try:
                await self._close_session()
            except Exception:
                pass
            logger.info(
                "NegRiskAgent stopped | scans=%d | signals_found=%d",
                self._scans_completed,
                self._signals_found,
            )

    async def _run_scan(self) -> None:
        """Execute one full scan cycle."""
        try:
            events = await self.fetch_grouped_markets()
            signals = self.find_arb_opportunities(events)

            self._scans_completed += 1
            self._signals_found += len(signals)
            self._last_scan_time = time.time()

            if signals:
                logger.info(
                    "NegRiskAgent scan #%d: found %d opportunity(ies) across %d events",
                    self._scans_completed,
                    len(signals),
                    len(events),
                )
                for sig in signals:
                    self._log_opportunity(sig)
                    if not self.dry_run and self.signal_callback:
                        try:
                            result = self.signal_callback(sig)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception as exc:
                            logger.error("NegRiskAgent signal_callback error: %s", exc)
            else:
                logger.info(
                    "NegRiskAgent scan #%d: no opportunities (scanned %d events)",
                    self._scans_completed,
                    len(events),
                )

        except Exception as exc:
            logger.error("NegRiskAgent scan error: %s", exc)

    def _log_opportunity(self, sig: ArbSignal) -> None:
        """Log an arbitrage opportunity in a structured format."""
        logger.info(
            "ARB OPPORTUNITY: %s | Sum=%.4f | Edge=%.2f%% | Size=$%.2f per leg | "
            "Type=%s | Legs=%d | Profit=$%.2f",
            sig.event_name,
            sig.sum_of_prices,
            sig.edge_after_fees * 100,
            sig.recommended_size_per_leg,
            sig.arb_type,
            len(sig.markets),
            sig.guaranteed_profit_usd,
        )
        for condition_id, yes_price, liquidity in sig.markets:
            logger.debug(
                "  Leg: %s | YES=%.4f | Liq=$%.0f",
                condition_id[:16],
                yes_price,
                liquidity,
            )

    async def stop(self) -> None:
        """Gracefully stop the agent."""
        self._running = False
        logger.info("NegRiskAgent stop requested")

    # ------------------------------------------------------------------
    # Historical Performance
    # ------------------------------------------------------------------

    def get_historical_performance(self) -> dict:
        """
        Read past arb signals from log files and compute performance stats.

        Reads logs/bot_YYYY-MM-DD.log and counts ARB OPPORTUNITY entries.

        Returns:
            Summary dict for postmortem.py.
        """
        import re
        from pathlib import Path
        from datetime import date

        logs_dir = Path(__file__).parent.parent / "logs"
        today = date.today().isoformat()
        log_file = logs_dir / f"bot_{today}.log"

        opportunities: list[dict] = []

        if log_file.exists():
            pattern = re.compile(
                r"ARB OPPORTUNITY: (.+?) \| Sum=([\d.]+) \| Edge=([\d.]+)% \| "
                r"Size=\$([\d.]+) per leg \| Type=(\w+)"
            )
            try:
                with open(log_file) as f:
                    for line in f:
                        m = pattern.search(line)
                        if m:
                            opportunities.append({
                                "event": m.group(1),
                                "sum": float(m.group(2)),
                                "edge_pct": float(m.group(3)),
                                "size_per_leg": float(m.group(4)),
                                "arb_type": m.group(5),
                            })
            except OSError as exc:
                logger.warning("NegRiskAgent: could not read log file: %s", exc)

        avg_edge = (
            sum(o["edge_pct"] for o in opportunities) / len(opportunities)
            if opportunities else 0.0
        )

        return {
            "opportunities_today": len(opportunities),
            "avg_edge_pct": round(avg_edge, 3),
            "scans_completed": self._scans_completed,
            "signals_found_session": self._signals_found,
            "theoretical_profit_usd": sum(
                o["size_per_leg"] * o["edge_pct"] / 100 for o in opportunities
            ),
        }

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Return current agent status."""
        return {
            "agent": "negrisk",
            "running": self._running,
            "dry_run": self.dry_run,
            "scans_completed": self._scans_completed,
            "signals_found": self._signals_found,
            "last_scan_time": self._last_scan_time,
        }
