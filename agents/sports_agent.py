"""
agents/sports_agent.py — The Odds API Sports Signal Agent

Fetches bookmaker odds from The Odds API, converts to implied probability,
then compares against open Polymarket sports markets. Emits BUY signals when
the consensus Vegas line diverges from the Polymarket price by more than
SPORTS_MIN_EDGE (default 5 percentage points).

If ODDS_API_KEY is not set, the agent logs a disabled message and returns [].
All bets are logged in DRY_RUN mode — no orders placed.

Supported sports (Odds API sport keys):
  - americanfootball_nfl
  - basketball_nba
  - baseball_mlb
  - icehockey_nhl
  - soccer_epl

Odds API documentation: https://the-odds-api.com/liveapi/guides/v4/
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Callable, Any

import aiohttp

import config
from core.market_scanner import Market

logger = logging.getLogger(__name__)

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Odds API sport keys to scan (in priority order)
SPORT_KEYS = [
    "americanfootball_nfl",
    "basketball_nba",
    "baseball_mlb",
    "icehockey_nhl",
    "soccer_epl",
]

# Map sport keywords in Polymarket questions to Odds API sport keys
_SPORT_KEYWORD_MAP: dict[str, str] = {
    "nfl":        "americanfootball_nfl",
    "football":   "americanfootball_nfl",
    "nba":        "basketball_nba",
    "basketball": "basketball_nba",
    "mlb":        "baseball_mlb",
    "baseball":   "baseball_mlb",
    "nhl":        "icehockey_nhl",
    "hockey":     "icehockey_nhl",
    "soccer":     "soccer_epl",
    "premier":    "soccer_epl",
    "epl":        "soccer_epl",
}

SPORTS_MIN_EDGE: float = float(config.__dict__.get("SPORTS_MIN_EDGE", 0.05))
SPORTS_MIN_LIQUIDITY: float = float(config.__dict__.get("SPORTS_MIN_LIQUIDITY", 5_000))
SPORTS_SCAN_INTERVAL: int = int(config.__dict__.get("SPORTS_SCAN_INTERVAL", 900))  # 15 min
SPORTS_CACHE_TTL: int = 15 * 60  # 15 minutes


# ---------------------------------------------------------------------------
# SportsSignal dataclass
# ---------------------------------------------------------------------------

@dataclass
class SportsSignal:
    """A trade signal derived from bookmaker odds vs. Polymarket price."""

    market_question: str
    condition_id: str
    side: str                    # 'YES' or 'NO'
    market_price: float          # Polymarket YES price
    consensus_probability: float # Vegas-implied probability
    edge: float                  # |consensus - market| - fee
    size_usd: float
    sport_key: str
    home_team: str
    away_team: str
    bookmakers_used: int
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self) -> str:
        return (
            f"SportsSignal({self.side} {self.condition_id[:16]} | "
            f"{self.away_team} @ {self.home_team} | "
            f"market={self.market_price:.3f} consensus={self.consensus_probability:.3f} "
            f"edge={self.edge:.2%} | ${self.size_usd:.2f})"
        )


# ---------------------------------------------------------------------------
# SportsAgent
# ---------------------------------------------------------------------------

class SportsAgent:
    """
    Fetches bookmaker odds from The Odds API and generates signals for
    Polymarket sports prediction markets.

    If ODDS_API_KEY is absent, the agent is disabled and returns [] from all
    scan calls — it does NOT raise or crash the bot.

    Usage:
        agent = SportsAgent(market_scanner=scanner, signal_callback=router.handle)
        await agent.run()
    """

    FEE_RATE: float = 0.02
    MIN_EDGE: float = SPORTS_MIN_EDGE
    MIN_LIQUIDITY: float = SPORTS_MIN_LIQUIDITY
    SCAN_INTERVAL: int = SPORTS_SCAN_INTERVAL

    def __init__(
        self,
        market_scanner=None,
        signal_callback: Optional[Callable[[SportsSignal], Any]] = None,
        dry_run: bool = config.DRY_RUN,
        portfolio_value: float = config.TOTAL_CAPITAL_USD,
    ):
        self.market_scanner = market_scanner
        self.signal_callback = signal_callback
        self.dry_run = dry_run
        self.portfolio_value = portfolio_value
        self.odds_api_key: str = config.ODDS_API_KEY

        self._enabled: bool = bool(self.odds_api_key)
        self._running: bool = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._signals_found: int = 0
        self._scans_completed: int = 0
        # Cache: sport_key -> (timestamp, odds_data)
        self._odds_cache: dict[str, tuple[float, list[dict]]] = {}

        if not self._enabled:
            logger.info(
                "SportsAgent disabled — ODDS_API_KEY not set. "
                "Set ODDS_API_KEY in .env to enable sports market signals."
            )
        else:
            logger.info(
                "SportsAgent initialised | dry_run=%s | min_edge=%.1f%% | "
                "scan_interval=%ds | sports=%d",
                self.dry_run,
                self.MIN_EDGE * 100,
                self.SCAN_INTERVAL,
                len(SPORT_KEYS),
            )

    # ------------------------------------------------------------------
    # HTTP Session
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15, connect=5),
                headers={"User-Agent": "polymarket-bot/1.0"},
            )
        return self._session

    async def _close_session(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Odds API
    # ------------------------------------------------------------------

    async def fetch_odds(
        self,
        sport: str,
        regions: str = "us",
        markets: str = "h2h",
    ) -> list[dict]:
        """
        Retrieve current odds from The Odds API for a given sport.

        Returns empty list if API key is missing or request fails.
        Results are cached for SPORTS_CACHE_TTL seconds.

        Args:
            sport:   Sport key (e.g. 'americanfootball_nfl').
            regions: Bookmaker regions (e.g. 'us', 'uk', 'eu').
            markets: Market type ('h2h' = moneyline / head-to-head).

        Returns:
            List of event dicts with bookmaker odds, or [].
        """
        if not self._enabled:
            return []

        # Cache check
        cached_at, cached_data = self._odds_cache.get(sport, (0.0, []))
        if cached_data and (time.time() - cached_at) < SPORTS_CACHE_TTL:
            logger.debug("SportsAgent: cache hit for sport=%s", sport)
            return cached_data

        session = await self._get_session()
        url = f"{ODDS_API_BASE}/sports/{sport}/odds"
        params = {
            "apiKey": self.odds_api_key,
            "regions": regions,
            "markets": markets,
            "oddsFormat": "american",
        }

        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._odds_cache[sport] = (time.time(), data)
                    logger.info(
                        "SportsAgent: fetched %d events for sport=%s",
                        len(data),
                        sport,
                    )
                    return data
                elif resp.status == 401:
                    logger.error("SportsAgent: Odds API 401 — invalid API key")
                    self._enabled = False  # Disable to avoid hammering with bad key
                elif resp.status == 429:
                    logger.warning("SportsAgent: Odds API rate limited (429)")
                elif resp.status == 422:
                    logger.debug("SportsAgent: sport=%s not currently available", sport)
                else:
                    logger.warning(
                        "SportsAgent: Odds API HTTP %d for sport=%s", resp.status, sport
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error("SportsAgent: Odds API error for sport=%s: %s", sport, exc)

        return []

    # ------------------------------------------------------------------
    # Probability Calculation
    # ------------------------------------------------------------------

    def implied_probability(self, american_odds: int) -> float:
        """
        Convert American odds to implied probability.

        Positive odds (underdog): prob = 100 / (odds + 100)
        Negative odds (favourite): prob = |odds| / (|odds| + 100)

        Args:
            american_odds: Integer American odds (e.g. -110, +150).

        Returns:
            Float in (0, 1) representing raw implied probability.
        """
        if american_odds >= 0:
            return 100.0 / (american_odds + 100.0)
        else:
            abs_odds = abs(american_odds)
            return abs_odds / (abs_odds + 100.0)

    def consensus_probability(
        self,
        event: dict,
        outcome_name: str,
    ) -> Optional[float]:
        """
        Calculate the average (consensus) implied probability for one outcome
        across all available bookmakers in an event.

        Averages raw implied probabilities without vig removal for simplicity.

        Args:
            event:        Odds API event dict with 'bookmakers' list.
            outcome_name: Team/outcome name to look up (e.g. "Los Angeles Lakers").

        Returns:
            Float in (0, 1) or None if no bookmakers have this outcome.
        """
        bookmakers = event.get("bookmakers", [])
        probs: list[float] = []

        for bm in bookmakers:
            for market in bm.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                for outcome in market.get("outcomes", []):
                    if outcome.get("name", "").lower() == outcome_name.lower():
                        price = outcome.get("price")
                        if price is not None:
                            try:
                                probs.append(self.implied_probability(int(price)))
                            except (ValueError, TypeError):
                                pass

        if not probs:
            return None
        return sum(probs) / len(probs)

    # ------------------------------------------------------------------
    # Market Matching
    # ------------------------------------------------------------------

    def match_sport(self, question: str) -> Optional[str]:
        """
        Identify which Odds API sport key a Polymarket question refers to.

        Returns sport_key string or None if no match.
        """
        q_lower = question.lower()
        for keyword, sport_key in _SPORT_KEYWORD_MAP.items():
            if keyword in q_lower:
                return sport_key
        return None

    def find_matching_event(
        self,
        market: Market,
        odds_events: list[dict],
    ) -> Optional[tuple[dict, str]]:
        """
        Find the Odds API event that best matches a Polymarket market question.

        Looks for team names from the event in the market question.

        Returns:
            (event_dict, outcome_name) for the team that YES resolves to, or None.
        """
        q_lower = market.question.lower()

        for event in odds_events:
            home = event.get("home_team", "")
            away = event.get("away_team", "")

            # Check if either team name appears in the question
            home_in_q = any(part.lower() in q_lower for part in home.split() if len(part) > 3)
            away_in_q = any(part.lower() in q_lower for part in away.split() if len(part) > 3)

            if not (home_in_q or away_in_q):
                continue

            # Determine which team is the YES outcome
            # Heuristic: "Will X win?" → X is the YES outcome
            if home_in_q and not away_in_q:
                return (event, home)
            if away_in_q and not home_in_q:
                return (event, away)
            # Both teams mentioned → question may be about the home team winning
            if home_in_q and away_in_q:
                # Default: home team is YES if "home" or their name appears first
                return (event, home)

        return None

    # ------------------------------------------------------------------
    # Signal Generation
    # ------------------------------------------------------------------

    def evaluate_market(
        self,
        market: Market,
        event: dict,
        outcome_name: str,
    ) -> Optional[SportsSignal]:
        """
        Compare consensus Vegas probability to Polymarket price.

        Returns a SportsSignal if edge > MIN_EDGE, else None.
        """
        if market.liquidity < self.MIN_LIQUIDITY:
            return None

        bookmakers = event.get("bookmakers", [])
        if not bookmakers:
            return None

        prob = self.consensus_probability(event, outcome_name)
        if prob is None:
            return None

        market_price = market.yes_price
        raw_edge = abs(prob - market_price)
        edge = raw_edge - self.FEE_RATE
        if edge < self.MIN_EDGE:
            return None

        side = "YES" if prob > market_price else "NO"
        size_usd = min(
            self.portfolio_value * 0.01,
            market.liquidity / 20.0,
        )

        sport_key = self.match_sport(market.question) or "unknown"

        return SportsSignal(
            market_question=market.question,
            condition_id=market.condition_id,
            side=side,
            market_price=market_price,
            consensus_probability=round(prob, 4),
            edge=round(edge, 4),
            size_usd=round(size_usd, 2),
            sport_key=sport_key,
            home_team=event.get("home_team", ""),
            away_team=event.get("away_team", ""),
            bookmakers_used=len(bookmakers),
        )

    # ------------------------------------------------------------------
    # Run Loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main scan loop. If disabled (no API key), exits immediately."""
        if not self._enabled:
            logger.info("SportsAgent disabled — not starting run loop")
            return

        self._running = True
        logger.info(
            "SportsAgent starting | DRY_RUN=%s | scan_interval=%ds",
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
            logger.info("SportsAgent cancelled")
        finally:
            self._running = False
            await self._close_session()
            logger.info(
                "SportsAgent stopped | scans=%d | signals=%d",
                self._scans_completed,
                self._signals_found,
            )

    async def _run_scan(self) -> None:
        """Execute one full sports scan cycle."""
        if not self._enabled:
            return

        try:
            if self.market_scanner is None:
                logger.warning("SportsAgent: no market_scanner configured")
                return

            sports_markets = self.market_scanner.get_markets_for_agent("sports")
            if not sports_markets:
                logger.info("SportsAgent: no sports markets in scanner cache")
                self._scans_completed += 1
                return

            signals_this_scan = 0

            # Group markets by sport to minimise API calls
            sport_to_markets: dict[str, list[Market]] = {}
            for market in sports_markets:
                sport_key = self.match_sport(market.question)
                if sport_key:
                    sport_to_markets.setdefault(sport_key, []).append(market)

            for sport_key, markets in sport_to_markets.items():
                odds_events = await self.fetch_odds(sport_key)
                if not odds_events:
                    continue

                for market in markets:
                    match = self.find_matching_event(market, odds_events)
                    if match is None:
                        continue

                    event, outcome_name = match
                    signal = self.evaluate_market(market, event, outcome_name)
                    if signal:
                        signals_this_scan += 1
                        self._signals_found += 1
                        logger.info(
                            "SportsAgent signal: %s | Vegas=%.3f vs Market=%.3f | "
                            "bookmakers=%d",
                            signal,
                            signal.consensus_probability,
                            signal.market_price,
                            signal.bookmakers_used,
                        )
                        if not self.dry_run and self.signal_callback:
                            try:
                                result = self.signal_callback(signal)
                                if asyncio.iscoroutine(result):
                                    await result
                            except Exception as exc:
                                logger.error("SportsAgent callback error: %s", exc)

            self._scans_completed += 1
            logger.info(
                "SportsAgent scan #%d: %d signal(s) from %d sports markets",
                self._scans_completed,
                signals_this_scan,
                len(sports_markets),
            )
        except Exception as exc:
            logger.error("SportsAgent scan error: %s", exc)

    async def stop(self) -> None:
        """Gracefully stop the agent."""
        self._running = False

    def get_status(self) -> dict:
        return {
            "agent": "sports",
            "enabled": self._enabled,
            "running": self._running,
            "dry_run": self.dry_run,
            "scans_completed": self._scans_completed,
            "signals_found": self._signals_found,
        }
