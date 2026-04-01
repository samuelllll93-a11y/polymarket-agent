"""
agents/politics_agent.py — NewsAPI + Claude Politics Signal Agent

Fetches recent political headlines via NewsAPI, then uses the Claude API to
estimate the probability of the YES outcome for each open Polymarket politics
market. Emits a BUY signal when Claude's estimate diverges from the market
price by more than POLITICS_MIN_EDGE.

Flow per scan:
  1. Get 'politics' markets from market_scanner
  2. For each market, extract keywords from the question
  3. Fetch recent headlines from NewsAPI matching those keywords
  4. Call Claude with: market question + headlines → probability estimate
  5. Compare estimate to market price; emit signal if edge > threshold

Claude is prompted to respond with a single float and reasoning.
All Claude calls go through the Anthropic SDK (model: claude-haiku-4-5 for cost).

DRY_RUN=True always — signals are logged but no orders placed.
Gracefully degrades: if NEWS_API_KEY missing, uses Claude with no news context.
If ANTHROPIC_API_KEY missing, falls back to keyword-sentiment heuristic only.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Callable, Any

import aiohttp

import config
from core.market_scanner import Market

logger = logging.getLogger(__name__)

NEWS_API_BASE = "https://newsapi.org/v2"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"   # Fast + cheap for scoring
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"


# ---------------------------------------------------------------------------
# PoliticsSignal dataclass
# ---------------------------------------------------------------------------

@dataclass
class PoliticsSignal:
    """A trade signal from news + Claude analysis vs. Polymarket market price."""

    market_question: str
    condition_id: str
    side: str                    # 'YES' or 'NO'
    market_price: float
    model_probability: float     # Claude's estimate
    edge: float
    size_usd: float
    reasoning: str               # Claude's one-sentence reasoning
    headlines_used: int          # Number of headlines fed to Claude
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self) -> str:
        return (
            f"PoliticsSignal({self.side} {self.condition_id[:16]} | "
            f"market={self.market_price:.3f} model={self.model_probability:.3f} "
            f"edge={self.edge:.2%} | ${self.size_usd:.2f} | {self.reasoning[:60]})"
        )


# ---------------------------------------------------------------------------
# PoliticsAgent
# ---------------------------------------------------------------------------

class PoliticsAgent:
    """
    Combines NewsAPI headlines and Claude inference for politics market signals.

    Usage:
        agent = PoliticsAgent(market_scanner=scanner, signal_callback=router.handle)
        await agent.run()
    """

    FEE_RATE: float = 0.02
    MIN_EDGE: float = config.POLITICS_MIN_EDGE
    MIN_LIQUIDITY: float = config.POLITICS_MIN_LIQUIDITY
    SCAN_INTERVAL: int = config.POLITICS_SCAN_INTERVAL
    HEADLINES_COUNT: int = config.POLITICS_HEADLINES_COUNT

    def __init__(
        self,
        market_scanner=None,
        signal_callback: Optional[Callable[[PoliticsSignal], Any]] = None,
        dry_run: bool = config.DRY_RUN,
        portfolio_value: float = config.TOTAL_CAPITAL_USD,
    ):
        self.market_scanner = market_scanner
        self.signal_callback = signal_callback
        self.dry_run = dry_run
        self.portfolio_value = portfolio_value

        self.news_api_key: str = config.NEWS_API_KEY
        self.anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")

        self._running: bool = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._signals_found: int = 0
        self._scans_completed: int = 0
        # Simple cache: question → (ts, probability, reasoning)
        self._score_cache: dict[str, tuple[float, float, str]] = {}
        self._cache_ttl: int = 1800  # Re-score every 30 minutes

        if not self.news_api_key:
            logger.warning("PoliticsAgent: NEWS_API_KEY not set — will score with Claude only")
        if not self.anthropic_api_key:
            logger.warning("PoliticsAgent: ANTHROPIC_API_KEY not set — will use heuristic fallback")

        logger.info(
            "PoliticsAgent initialised | dry_run=%s | min_edge=%.1f%% | scan_interval=%ds",
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
                timeout=aiohttp.ClientTimeout(total=20, connect=5),
                headers={"User-Agent": "polymarket-bot/1.0"},
            )
        return self._session

    async def _close_session(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # NewsAPI
    # ------------------------------------------------------------------

    async def fetch_headlines(self, query: str, page_size: int = 10) -> list[dict]:
        """
        Pull recent headlines from NewsAPI matching the given query.

        Args:
            query:      Search query string.
            page_size:  Max number of articles to return (1-100).

        Returns:
            List of article dicts with 'title', 'description', 'publishedAt'.
            Empty list if API key missing or request fails.
        """
        if not self.news_api_key:
            return []

        session = await self._get_session()
        params = {
            "q": query,
            "pageSize": min(page_size, 20),
            "sortBy": "publishedAt",
            "language": "en",
            "apiKey": self.news_api_key,
        }
        try:
            async with session.get(f"{NEWS_API_BASE}/everything", params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    articles = data.get("articles", [])
                    logger.debug(
                        "PoliticsAgent: NewsAPI returned %d articles for query '%s'",
                        len(articles), query
                    )
                    return articles
                elif resp.status == 401:
                    logger.error("PoliticsAgent: NewsAPI 401 — invalid API key")
                elif resp.status == 429:
                    logger.warning("PoliticsAgent: NewsAPI rate limited")
                else:
                    logger.warning("PoliticsAgent: NewsAPI HTTP %d", resp.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error("PoliticsAgent: NewsAPI error: %s", exc)
        return []

    # ------------------------------------------------------------------
    # Claude Scoring
    # ------------------------------------------------------------------

    async def estimate_probability_with_claude(
        self,
        market: dict,
        headlines: list[dict],
    ) -> tuple[float, str]:
        """
        Ask Claude to estimate the YES probability for a Polymarket market.

        Args:
            market:    Dict with 'question' key.
            headlines: List of recent headline dicts from NewsAPI.

        Returns:
            (probability, reasoning) — float in [0,1] and one-sentence reason.
            Falls back to heuristic if Claude unavailable.
        """
        question = market.get("question", "")

        # Check cache
        cached = self._score_cache.get(question)
        if cached:
            cached_at, prob, reasoning = cached
            if time.time() - cached_at < self._cache_ttl:
                logger.debug("PoliticsAgent: cache hit for '%s'", question[:50])
                return prob, reasoning

        if not self.anthropic_api_key:
            return self._heuristic_fallback(question, headlines)

        # Build headline context
        headline_text = ""
        if headlines:
            items = []
            for h in headlines[:self.HEADLINES_COUNT]:
                title = h.get("title", "")
                desc = h.get("description", "") or ""
                pub = h.get("publishedAt", "")[:10]
                items.append(f"[{pub}] {title}. {desc[:100]}")
            headline_text = "\n".join(items)
        else:
            headline_text = "(No recent headlines available)"

        prompt = (
            f"You are a prediction market analyst. Given the following recent news headlines "
            f"and a Polymarket question, estimate the probability that the answer is YES.\n\n"
            f"Question: {question}\n\n"
            f"Recent headlines:\n{headline_text}\n\n"
            f"Respond with ONLY a JSON object in this exact format:\n"
            f'{"{"}"probability": 0.XX, "reasoning": "One sentence explanation."{"}"}' + "\n"
            f"The probability must be a float between 0.0 and 1.0."
        )

        session = await self._get_session()
        payload = {
            "model": CLAUDE_MODEL,
            "max_tokens": 150,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": self.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        try:
            async with session.post(ANTHROPIC_API_URL, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = data.get("content", [{}])[0].get("text", "")
                    prob, reasoning = self._parse_claude_response(content)
                    self._score_cache[question] = (time.time(), prob, reasoning)
                    logger.debug(
                        "PoliticsAgent: Claude scored '%s' → %.3f (%s)",
                        question[:50], prob, reasoning[:60]
                    )
                    return prob, reasoning
                elif resp.status == 401:
                    logger.error("PoliticsAgent: Anthropic API 401 — invalid key")
                elif resp.status == 429:
                    logger.warning("PoliticsAgent: Anthropic API rate limited")
                else:
                    logger.warning("PoliticsAgent: Anthropic API HTTP %d", resp.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error("PoliticsAgent: Claude API error: %s", exc)

        # Fallback on any Claude error
        return self._heuristic_fallback(question, headlines)

    def _parse_claude_response(self, text: str) -> tuple[float, str]:
        """Parse Claude's JSON probability response."""
        import json
        # Try to extract JSON from response (Claude may add surrounding text)
        json_match = re.search(r'\{[^}]+\}', text, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group())
                prob = float(parsed.get("probability", 0.5))
                reasoning = str(parsed.get("reasoning", "No reasoning provided."))
                prob = max(0.01, min(0.99, prob))  # Clamp to avoid extremes
                return prob, reasoning
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

        # Fallback: extract first float from response
        float_match = re.search(r'\b0\.\d+\b', text)
        if float_match:
            prob = float(float_match.group())
            return max(0.01, min(0.99, prob)), "Extracted from response."

        return 0.50, "Could not parse Claude response."

    def _heuristic_fallback(self, question: str, headlines: list[dict]) -> tuple[float, str]:
        """
        Simple keyword-sentiment heuristic when Claude is unavailable.

        Counts positive/negative sentiment words in headlines relative to the
        question's subject and returns a rough probability.
        """
        question_lower = question.lower()
        positive_kw = ["win", "lead", "ahead", "victory", "surge", "polling high",
                       "favorite", "advantage", "popular", "rising"]
        negative_kw = ["lose", "trailing", "behind", "defeat", "drop", "scandal",
                       "arrest", "indicted", "falling", "unpopular"]

        pos_score = 0
        neg_score = 0

        # Score headlines only (question contains words like "win" which bias the count)
        all_text = ""
        for h in headlines[:self.HEADLINES_COUNT]:
            all_text += " " + (h.get("title", "") + " " + (h.get("description") or "")).lower()

        for kw in positive_kw:
            pos_score += all_text.count(kw)
        for kw in negative_kw:
            neg_score += all_text.count(kw)

        total = pos_score + neg_score
        if total == 0:
            return 0.50, "No sentiment signals in headlines (heuristic)."

        prob = pos_score / total
        prob = max(0.05, min(0.95, prob))
        direction = "positive" if prob > 0.5 else "negative"
        return round(prob, 3), f"Heuristic: {direction} sentiment ({pos_score}+ / {neg_score}-)."

    # ------------------------------------------------------------------
    # Keyword Extraction
    # ------------------------------------------------------------------

    def extract_search_query(self, question: str) -> str:
        """
        Extract a NewsAPI search query from a Polymarket market question.

        Removes common filler words and extracts key named entities.
        """
        # Remove question marks and common filler
        query = re.sub(r'[?!]', '', question)
        stop_words = {
            "will", "the", "a", "an", "be", "is", "are", "was", "were",
            "in", "on", "at", "to", "for", "of", "and", "or", "win",
            "election", "2024", "2025", "2026", "by",
        }
        words = query.split()
        keywords = [w for w in words if w.lower() not in stop_words and len(w) > 2]
        # Take first 5 meaningful keywords
        query_str = " ".join(keywords[:5])
        return query_str or question[:50]

    # ------------------------------------------------------------------
    # Signal Generation
    # ------------------------------------------------------------------

    async def evaluate_market(self, market: Market) -> Optional[PoliticsSignal]:
        """
        Evaluate a single politics market using news + Claude.

        Returns a PoliticsSignal if edge > MIN_EDGE, else None.
        """
        if market.liquidity < self.MIN_LIQUIDITY:
            return None

        query = self.extract_search_query(market.question)
        headlines = await self.fetch_headlines(query, page_size=self.HEADLINES_COUNT)

        model_prob, reasoning = await self.estimate_probability_with_claude(
            {"question": market.question}, headlines
        )

        market_price = market.yes_price
        raw_edge = abs(model_prob - market_price)
        edge = raw_edge - self.FEE_RATE
        if edge < self.MIN_EDGE:
            return None

        side = "YES" if model_prob > market_price else "NO"
        size_usd = min(
            self.portfolio_value * 0.01,
            market.liquidity / 20.0,
        )

        return PoliticsSignal(
            market_question=market.question,
            condition_id=market.condition_id,
            side=side,
            market_price=market_price,
            model_probability=model_prob,
            edge=round(edge, 4),
            size_usd=round(size_usd, 2),
            reasoning=reasoning,
            headlines_used=len(headlines),
        )

    # ------------------------------------------------------------------
    # Run Loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main scan loop. Runs every SCAN_INTERVAL seconds."""
        self._running = True
        logger.info(
            "PoliticsAgent starting | DRY_RUN=%s | scan_interval=%ds",
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
            logger.info("PoliticsAgent cancelled")
        finally:
            self._running = False
            await self._close_session()
            logger.info(
                "PoliticsAgent stopped | scans=%d | signals=%d",
                self._scans_completed,
                self._signals_found,
            )

    async def _run_scan(self) -> None:
        """Execute one full politics scan cycle."""
        try:
            if self.market_scanner is None:
                logger.warning("PoliticsAgent: no market_scanner configured")
                return

            markets = self.market_scanner.get_markets_for_agent("politics")
            if not markets:
                logger.info("PoliticsAgent: no politics markets found in scanner cache")
                self._scans_completed += 1
                return

            signals_this_scan = 0
            for market in markets:
                try:
                    signal = await self.evaluate_market(market)
                    if signal:
                        signals_this_scan += 1
                        self._signals_found += 1
                        logger.info("PoliticsAgent signal: %s", signal)
                        if not self.dry_run and self.signal_callback:
                            try:
                                result = self.signal_callback(signal)
                                if asyncio.iscoroutine(result):
                                    await result
                            except Exception as exc:
                                logger.error("PoliticsAgent callback error: %s", exc)
                except Exception as exc:
                    logger.error(
                        "PoliticsAgent: error evaluating market '%s': %s",
                        market.question[:50], exc
                    )

            self._scans_completed += 1
            logger.info(
                "PoliticsAgent scan #%d: %d signal(s) from %d politics markets",
                self._scans_completed,
                signals_this_scan,
                len(markets),
            )
        except Exception as exc:
            logger.error("PoliticsAgent scan error: %s", exc)

    async def stop(self) -> None:
        """Gracefully stop the agent."""
        self._running = False

    def get_status(self) -> dict:
        return {
            "agent": "politics",
            "running": self._running,
            "dry_run": self.dry_run,
            "scans_completed": self._scans_completed,
            "signals_found": self._signals_found,
        }
