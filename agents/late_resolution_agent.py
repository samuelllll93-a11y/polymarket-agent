"""
agents/late_resolution_agent.py - Late Resolution Sniper Agent

Scans active markets for near-resolved outcomes (price $0.97-$0.995) and
buys the winning side to hold to $1.00 resolution.

Strategy:
  - Query Gamma API for active, non-negRisk markets
  - Filter for prices between 0.970 and 0.995
  - Score resolution confidence via time-based and price-based signals
  - Check for oracle dispute risk (recent price volatility)
  - Track positions in data/late_res_positions.json
  - Target 0.5-3% return per trade, high frequency

DRY_RUN=True always - all signals are logged but no orders placed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Callable, Any

import aiohttp

import config

logger = logging.getLogger(__name__)

# Risk keywords that indicate oracle dispute risk.
# NOTE: "resolution" is excluded — Polymarket uses "resolve/resolution" in
# nearly every market description ("This market will resolve to ...") which
# causes false-positive -50 penalties on legitimate candidates.
RISK_KEYWORDS = [
    "dispute", "disputed", "appeal", "appealed", "oracle",
    "uma", "reopen", "reopened", "re-resolution",
]

POSITIONS_FILE = Path(__file__).parent.parent / "data" / "late_res_positions.json"


# ---------------------------------------------------------------------------
# LateResSignal dataclass
# ---------------------------------------------------------------------------

@dataclass
class LateResSignal:
    """A late-resolution sniper signal."""

    market_id: str
    question: str
    side: str               # "YES" or "NO"
    entry_price: float      # Best ask on winning side
    expected_return: float   # (1.00 - price) / price
    net_edge: float          # expected_return - taker fee
    confidence_score: int    # 0-100 resolution confidence
    expected_resolution: str  # ISO timestamp or "unknown"
    position_size_usd: float
    agent: str = "late_res"
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self) -> str:
        return (
            f"LateResSignal({self.side} {self.market_id[:16]} | "
            f"price={self.entry_price:.3f} edge={self.net_edge:.2%} "
            f"conf={self.confidence_score}/100 | ${self.position_size_usd:.2f})"
        )


# ---------------------------------------------------------------------------
# Position tracking
# ---------------------------------------------------------------------------

def load_positions() -> list[dict]:
    """Load open positions from disk."""
    if POSITIONS_FILE.exists():
        try:
            data = json.loads(POSITIONS_FILE.read_text())
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load positions file: %s", exc)
    return []


def save_positions(positions: list[dict]) -> None:
    """Persist positions to disk."""
    POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    POSITIONS_FILE.write_text(json.dumps(positions, indent=2, default=str))


# ---------------------------------------------------------------------------
# Confidence Scorer
# ---------------------------------------------------------------------------

def score_resolution_confidence(
    market: dict,
    best_price: float,
    spread: float,
    volume_24h: float,
) -> int:
    """
    Score resolution confidence 0-100 based on time, price, and risk signals.

    Args:
        market: Raw market dict from Gamma API.
        best_price: Best ask price on the winning side (0.97-0.995).
        spread: Ask - bid spread on winning side.
        volume_24h: 24h volume in USD.

    Returns:
        Integer confidence score 0-100.
    """
    score = 0

    # --- TIME-BASED signals ---
    # A closed market is already past resolution — highest confidence tier
    is_closed = (
        market.get("closed", False)
        or str(market.get("active", "true")).lower() == "false"
    )
    if is_closed:
        score += 90
    else:
        end_date_str = (
            market.get("endDate")
            or market.get("end_date_iso")
            or market.get("end_date")
            or market.get("expiration")
            or ""
        )
        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                hours_to_end = (end_date - now).total_seconds() / 3600.0

                if hours_to_end <= 0:
                    # End date already passed, oracle pending
                    score += 90
                elif hours_to_end <= 1:
                    score += 90
                elif hours_to_end <= 6:
                    score += 70
                elif hours_to_end <= 24:
                    score += 50
                elif hours_to_end <= 48:
                    score += 30
            except (ValueError, TypeError):
                pass

    # --- PRICE-BASED signals ---
    if 0.990 <= best_price <= 0.995:
        score += 30
    elif 0.980 <= best_price < 0.990:
        score += 20
    elif 0.970 <= best_price < 0.980:
        score += 10

    if volume_24h > 10_000:
        score += 10

    if spread < 0.005:
        score += 10

    # --- RISK signals (subtract) ---
    question = (market.get("question", "") + " " + market.get("description", "")).lower()
    for keyword in RISK_KEYWORDS:
        if keyword in question:
            score -= 50
            break  # One penalty is enough

    if market.get("negRisk", False):
        score -= 40

    # Clamp to 0-100
    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# Edge calculation
# ---------------------------------------------------------------------------

def calculate_edge(entry_price: float, taker_fee: float = 0.005) -> tuple[float, float]:
    """
    Calculate expected return and net edge.

    Returns:
        (expected_return, net_edge) where net_edge = expected_return - taker_fee
    """
    expected_return = (1.0 - entry_price) / entry_price
    net_edge = expected_return - taker_fee
    return expected_return, net_edge


# ---------------------------------------------------------------------------
# LateResolutionAgent
# ---------------------------------------------------------------------------

class LateResolutionAgent:
    """
    Scans markets for near-resolved outcomes and snipes the final
    price movement to $1.00 resolution.

    Usage:
        agent = LateResolutionAgent(
            signal_callback=router.handle_signal,
            alerter=alerter,
            dry_run=True,
        )
        await agent.run()
    """

    TAKER_FEE: float = 0.005  # ~0.5% taker fee estimate

    def __init__(
        self,
        signal_callback: Optional[Callable] = None,
        alerter=None,
        dry_run: bool = config.DRY_RUN,
    ):
        self.signal_callback = signal_callback
        self.alerter = alerter
        self.dry_run = dry_run

        # Config thresholds
        self.min_confidence = config.LATE_RES_MIN_CONFIDENCE
        self.min_price = config.LATE_RES_MIN_PRICE
        self.max_price = config.LATE_RES_MAX_PRICE
        self.max_position_usd = config.LATE_RES_MAX_POSITION_USD
        self.max_total_exposure = config.LATE_RES_MAX_TOTAL_EXPOSURE
        self.scan_interval = config.LATE_RES_SCAN_INTERVAL

        self._running: bool = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._scans_completed: int = 0
        self._signals_found: int = 0
        self._positions: list[dict] = load_positions()

        # Simulated PnL tracking (dry run)
        self._sim_total_pnl: float = 0.0
        self._sim_trades: int = 0
        self._sim_wins: int = 0

        logger.info(
            "LateResolutionAgent initialised | dry_run=%s | "
            "min_conf=%d | price_range=%.3f-%.3f | scan=%ds",
            self.dry_run, self.min_confidence,
            self.min_price, self.max_price, self.scan_interval,
        )

    # ------------------------------------------------------------------
    # HTTP Session
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20, connect=5),
            )
        return self._session

    async def _close_session(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Market Scanning
    # ------------------------------------------------------------------

    async def fetch_active_markets(self) -> list[dict]:
        """
        Fetch near-expiry markets from the Gamma API.

        Two queries:
          1. Active markets with endDate within 48 hours (imminent resolution)
          2. Recently closed markets not yet settled (oracle pending)

        Returns:
            Combined, deduplicated list of market dicts (negRisk excluded).
        """
        session = await self._get_session()
        url = f"{config.POLY_GAMMA_API_URL}/markets"
        end_date_max = (
            datetime.now(timezone.utc) + timedelta(hours=48)
        ).isoformat()

        all_markets: list[dict] = []

        # --- Query 1: active markets expiring within 48h ---
        params_active = {
            "active": "true",
            "closed": "false",
            "end_date_max": end_date_max,
            "limit": 100,
        }
        try:
            async with session.get(url, params=params_active) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if isinstance(data, list):
                        all_markets.extend(data)
                    elif isinstance(data, dict):
                        all_markets.extend(
                            data.get("data", data.get("markets", []))
                        )
                else:
                    logger.warning(
                        "LateRes: Gamma API (active) HTTP %d", resp.status
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error("LateRes: Gamma API (active) error: %s", exc)

        # --- Query 2: closed but not yet settled (oracle pending) ---
        params_closed = {
            "closed": "true",
            "limit": 50,
        }
        try:
            async with session.get(url, params=params_closed) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    closed_markets = []
                    if isinstance(data, list):
                        closed_markets = data
                    elif isinstance(data, dict):
                        closed_markets = data.get("data", data.get("markets", []))
                    # Only keep markets with near-resolution prices (>0.95 on one side)
                    seen_ids = {
                        m.get("conditionId") or m.get("condition_id") or m.get("id")
                        for m in all_markets
                    }
                    for m in closed_markets:
                        mid = m.get("conditionId") or m.get("condition_id") or m.get("id")
                        if mid and mid not in seen_ids:
                            all_markets.append(m)
                            seen_ids.add(mid)
                else:
                    logger.warning(
                        "LateRes: Gamma API (closed) HTTP %d", resp.status
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error("LateRes: Gamma API (closed) error: %s", exc)

        # Filter out negRisk markets
        return [m for m in all_markets if not m.get("negRisk", False)]

    def find_candidates(self, markets: list[dict]) -> list[dict]:
        """
        Filter markets for near-resolved candidates.

        A candidate has best YES ask or best NO ask between min_price and max_price.
        Returns list of dicts: {market, side, best_price, spread, volume_24h}
        """
        candidates = []
        open_market_ids = {p["market_id"] for p in self._positions if p.get("status") in ("open", "simulated")}

        for m in markets:
            market_id = m.get("conditionId") or m.get("condition_id") or m.get("id", "")

            # Skip if already have an open position
            if market_id in open_market_ids:
                continue

            # Parse prices
            yes_price = self._parse_price(m, "yes")
            no_price = self._parse_price(m, "no")

            # Parse volume
            volume_24h = float(m.get("volume24hr", 0) or m.get("volume_24h", 0) or 0)

            # Check YES side
            if yes_price and self.min_price <= yes_price <= self.max_price:
                yes_bid = self._parse_bid(m, "yes")
                spread = (yes_price - yes_bid) if yes_bid else 0.01
                candidates.append({
                    "market": m,
                    "market_id": market_id,
                    "side": "YES",
                    "best_price": yes_price,
                    "spread": spread,
                    "volume_24h": volume_24h,
                })

            # Check NO side
            if no_price and self.min_price <= no_price <= self.max_price:
                no_bid = self._parse_bid(m, "no")
                spread = (no_price - no_bid) if no_bid else 0.01
                candidates.append({
                    "market": m,
                    "market_id": market_id,
                    "side": "NO",
                    "best_price": no_price,
                    "spread": spread,
                    "volume_24h": volume_24h,
                })

        return candidates

    def _parse_price(self, market: dict, side: str) -> Optional[float]:
        """Parse best ask price for a side from various API formats."""
        try:
            # Gamma API format: outcomePrices as JSON string "[\"0.98\",\"0.02\"]"
            outcome_prices = market.get("outcomePrices")
            if outcome_prices:
                if isinstance(outcome_prices, str):
                    prices = json.loads(outcome_prices)
                else:
                    prices = outcome_prices
                if isinstance(prices, list) and len(prices) >= 2:
                    idx = 0 if side == "yes" else 1
                    return float(prices[idx])

            # Fallback: bestAsk field
            if side == "yes":
                val = market.get("bestAsk") or market.get("yes_ask") or market.get("bestYesAsk")
            else:
                val = market.get("no_ask") or market.get("bestNoAsk")
            if val:
                return float(val)
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
        return None

    def _parse_bid(self, market: dict, side: str) -> Optional[float]:
        """Parse best bid price for a side."""
        try:
            if side == "yes":
                val = market.get("bestBid") or market.get("yes_bid") or market.get("bestYesBid")
            else:
                val = market.get("no_bid") or market.get("bestNoBid")
            if val:
                return float(val)
        except (ValueError, TypeError):
            pass
        return None

    # ------------------------------------------------------------------
    # Oracle Dispute Safety Check
    # ------------------------------------------------------------------

    async def check_price_volatility(self, condition_id: str) -> bool:
        """
        Check CLOB API for recent price volatility that may indicate
        an oracle dispute.

        Returns:
            True if safe to trade, False if volatility detected.
        """
        session = await self._get_session()
        url = f"{config.POLY_CLOB_API_URL}/prices-history"
        params = {
            "market": condition_id,
            "interval": "1h",
            "fidelity": "1",
        }

        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 403:
                    logger.warning(
                        "LateRes: CLOB prices-history returned 403 (geo-block?) "
                        "for %s — skipping volatility check, allowing trade",
                        condition_id[:16],
                    )
                    return True  # Don't block on geo-restriction
                if resp.status != 200:
                    logger.warning(
                        "LateRes: CLOB prices-history HTTP %d for %s",
                        resp.status, condition_id[:16],
                    )
                    return True  # Don't block on API errors

                data = await resp.json(content_type=None)
                history = data if isinstance(data, list) else data.get("history", [])

                if not history:
                    return True

                # Check last hour: any drop > 3 cents?
                prices = []
                now = time.time()
                for point in history:
                    ts = point.get("t", 0)
                    price = float(point.get("p", 0))
                    if isinstance(ts, (int, float)) and (now - ts) < 3600:
                        prices.append(price)

                if len(prices) >= 2:
                    max_price = max(prices)
                    min_price = min(prices)
                    if max_price - min_price > 0.03:
                        logger.info(
                            "LateRes: price volatility detected for %s "
                            "(%.3f swing in 1h) — SKIP",
                            condition_id[:16], max_price - min_price,
                        )
                        return False

                # Check 24h: recovery from < 0.80
                for point in history:
                    ts = point.get("t", 0)
                    price = float(point.get("p", 0))
                    if isinstance(ts, (int, float)) and (now - ts) < 86400:
                        if price < 0.80:
                            logger.info(
                                "LateRes: recent dispute recovery for %s "
                                "(price was %.3f in last 24h) — SKIP",
                                condition_id[:16], price,
                            )
                            return False

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error("LateRes: CLOB volatility check error: %s", exc)
            return True  # Don't block on network errors
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("LateRes: volatility data parse error: %s", exc)
            return True

        return True

    # ------------------------------------------------------------------
    # Position Management
    # ------------------------------------------------------------------

    def _total_open_exposure(self) -> float:
        """Sum of all open/simulated position sizes."""
        return sum(
            p.get("position_usd", p.get("position_size_usd", 0))
            for p in self._positions
            if p.get("status") in ("open", "simulated")
        )

    def _open_position_count(self) -> int:
        """Count of open/simulated positions."""
        return sum(1 for p in self._positions if p.get("status") in ("open", "simulated"))

    def _add_position(self, signal: LateResSignal) -> dict:
        """Add a new position to tracking. Returns the position dict."""
        shares = signal.position_size_usd / signal.entry_price
        position = {
            "market_id": signal.market_id,
            "condition_id": signal.market_id,
            "question": signal.question,
            "side": signal.side,
            "entry_price": signal.entry_price,
            "entry_time": signal.generated_at.isoformat(),
            "position_usd": signal.position_size_usd,
            "shares": round(shares, 2),
            "expected_resolution": signal.expected_resolution,
            "confidence_score": signal.confidence_score,
            "status": "open",
            "simulated": True,
        }
        self._positions.append(position)
        save_positions(self._positions)
        return position

    # ------------------------------------------------------------------
    # Telegram Alerts
    # ------------------------------------------------------------------

    async def _send_buy_alert(
        self, signal: LateResSignal, shares: float, time_str: str,
    ) -> None:
        """Send Telegram alert for a simulated BUY."""
        if self.alerter is None:
            return
        msg = (
            f"\U0001f7e2 <b>[DRY RUN] LATE RES BUY</b>\n"
            f"Market: <i>{signal.question[:60]}</i>\n"
            f"Side: <code>{signal.side}</code> @ <code>${signal.entry_price:.3f}</code>\n"
            f"Size: <code>${signal.position_size_usd:.2f}</code> "
            f"(<code>{shares:.1f} shares</code>)\n"
            f"Confidence: <code>{signal.confidence_score}/100</code>\n"
            f"Edge: <code>{signal.net_edge:.1%}</code> net of fees\n"
            f"Expected resolution: <code>{time_str}</code>"
        )
        await self.alerter.send(msg)

    async def _send_win_alert(self, pos: dict, pnl: float, pnl_pct: float, dur_str: str) -> None:
        """Telegram alert for a resolved WIN."""
        if self.alerter is None:
            return
        msg = (
            f"\u2705 <b>[DRY RUN] LATE RES CLOSED \u2014 WIN</b>\n"
            f"Market: <i>{pos['question'][:60]}</i>\n"
            f"Entry: <code>${pos['entry_price']:.3f}</code> \u2192 "
            f"Exit: <code>$1.000</code>\n"
            f"P&amp;L: <code>+${pnl:.2f}</code> (<code>{pnl_pct:.1%}</code>)\n"
            f"Hold: <code>{dur_str}</code>\n"
            f"Shares: <code>{pos['shares']:.1f}</code>"
        )
        await self.alerter.send(msg)

    async def _send_loss_alert(self, pos: dict, pnl: float, pnl_pct: float, dur_str: str) -> None:
        """Telegram alert for a resolved LOSS."""
        if self.alerter is None:
            return
        msg = (
            f"\u274c <b>[DRY RUN] LATE RES CLOSED \u2014 LOSS</b>\n"
            f"Market: <i>{pos['question'][:60]}</i>\n"
            f"Entry: <code>${pos['entry_price']:.3f}</code> \u2192 "
            f"Exit: <code>$0.000</code>\n"
            f"P&amp;L: <code>-${abs(pnl):.2f}</code> (<code>{pnl_pct:.1%}</code>)\n"
            f"Hold: <code>{dur_str}</code>\n"
            f"NOTE: Check for oracle dispute"
        )
        await self.alerter.send(msg)

    async def _send_drop_warning(self, pos: dict, current_price: float, drop_pct: float) -> None:
        """Telegram warning when price drops >3c from entry."""
        if self.alerter is None:
            return
        msg = (
            f"\u26a0\ufe0f <b>[DRY RUN] LATE RES WARNING</b>\n"
            f"Market: <i>{pos['question'][:60]}</i>\n"
            f"Entry: <code>${pos['entry_price']:.3f}</code> | "
            f"Current: <code>${current_price:.3f}</code>\n"
            f"Drop: <code>{drop_pct:.1%}</code> \u2014 possible dispute forming"
        )
        await self.alerter.send(msg)

    async def _send_daily_summary(self) -> None:
        """Send daily P&L summary for closed positions."""
        if self.alerter is None:
            return

        # Gather all positions closed today
        today = datetime.now(timezone.utc).date()
        closed_today = []
        for p in self._positions:
            if p.get("status") not in ("closed_win", "closed_loss"):
                continue
            closed_at = p.get("closed_at", "")
            if not closed_at:
                continue
            try:
                close_date = datetime.fromisoformat(closed_at).date()
                if close_date == today:
                    closed_today.append(p)
            except (ValueError, TypeError):
                continue

        if not closed_today:
            return

        wins = sum(1 for p in closed_today if p["status"] == "closed_win")
        losses = sum(1 for p in closed_today if p["status"] == "closed_loss")
        total = wins + losses
        win_rate = (wins / total) if total > 0 else 0.0
        pnls = [p.get("realised_pnl", 0.0) for p in closed_today]
        total_pnl = sum(pnls)
        best = max(pnls) if pnls else 0.0
        worst = min(pnls) if pnls else 0.0

        # Avg hold time
        hold_secs = []
        for p in closed_today:
            try:
                entry_t = datetime.fromisoformat(p["entry_time"])
                close_t = datetime.fromisoformat(p["closed_at"])
                hold_secs.append((close_t - entry_t).total_seconds())
            except (ValueError, TypeError, KeyError):
                pass
        if hold_secs:
            avg_s = sum(hold_secs) / len(hold_secs)
            if avg_s < 3600:
                avg_str = f"{avg_s / 60:.0f}m"
            else:
                avg_str = f"{avg_s / 3600:.1f}h"
        else:
            avg_str = "N/A"

        msg = (
            f"\U0001f4ca <b>LATE RES DAILY SUMMARY</b>\n"
            f"Trades closed: <code>{total}</code>\n"
            f"Wins: <code>{wins}</code> (<code>{win_rate:.0%}</code>)\n"
            f"Losses: <code>{losses}</code>\n"
            f"Total P&amp;L: <code>${total_pnl:+.2f}</code>\n"
            f"Avg hold time: <code>{avg_str}</code>\n"
            f"Best trade: <code>+${best:.2f}</code>\n"
            f"Worst trade: <code>${worst:.2f}</code>"
        )
        await self.alerter.send(msg)

    # ------------------------------------------------------------------
    # Scan & Signal
    # ------------------------------------------------------------------

    async def _run_scan(self) -> None:
        """Execute one full scan cycle."""
        try:
            markets = await self.fetch_active_markets()
            if not markets:
                logger.info("LateRes scan #%d: 0 markets fetched", self._scans_completed + 1)
                self._scans_completed += 1
                return

            candidates = self.find_candidates(markets)
            signals_this_scan = 0

            for c in candidates:
                market = c["market"]
                market_id = c["market_id"]
                side = c["side"]
                best_price = c["best_price"]
                spread = c["spread"]
                volume_24h = c["volume_24h"]

                # Score confidence
                conf = score_resolution_confidence(market, best_price, spread, volume_24h)

                # Calculate edge (needed for debug log even if filtered)
                expected_return, net_edge = calculate_edge(best_price, self.TAKER_FEE)

                # Parse hours remaining for debug log
                _hours_remaining = -1.0
                _end_str = (
                    market.get("endDate")
                    or market.get("end_date_iso")
                    or market.get("end_date")
                    or ""
                )
                if _end_str:
                    try:
                        _end_dt = datetime.fromisoformat(_end_str.replace("Z", "+00:00"))
                        _hours_remaining = (
                            (_end_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
                        )
                    except (ValueError, TypeError):
                        pass

                # Determine filter result
                question = market.get("question", "Unknown market")
                if conf < self.min_confidence:
                    _result = f"FILTERED (score {conf} < {self.min_confidence})"
                elif net_edge < 0.005:
                    _result = f"FILTERED (edge {net_edge:.1%} < 0.5%)"
                else:
                    _result = "SIGNAL"

                logger.info(
                    "\U0001f50d LateRes candidate: %s | side=%s | price=%.3f | "
                    "score=%d/100 | edge=%.1f%% | time_to_end=%.1fh | RESULT: %s",
                    question[:50], side, best_price,
                    conf, net_edge * 100, _hours_remaining, _result,
                )

                if conf < self.min_confidence:
                    continue

                if net_edge < 0.005:  # Minimum 0.5% net edge
                    continue

                # Check position limits
                if self._open_position_count() >= 10:
                    logger.info("LateRes: max 10 positions reached — skipping")
                    break

                if self._total_open_exposure() + self.max_position_usd > self.max_total_exposure:
                    logger.info("LateRes: max total exposure reached — skipping")
                    break

                # Oracle dispute safety check
                safe = await self.check_price_volatility(market_id)
                if not safe:
                    continue

                # Build expected resolution timestamp
                end_date_str = (
                    market.get("endDate")
                    or market.get("end_date_iso")
                    or market.get("end_date")
                    or "unknown"
                )

                signal = LateResSignal(
                    market_id=market_id,
                    question=question,
                    side=side,
                    entry_price=best_price,
                    expected_return=expected_return,
                    net_edge=net_edge,
                    confidence_score=conf,
                    expected_resolution=end_date_str,
                    position_size_usd=self.max_position_usd,
                )

                signals_this_scan += 1
                self._signals_found += 1

                # Track position (open, monitored until resolution)
                pos = self._add_position(signal)
                shares = pos["shares"]

                # Calculate time to resolution for display
                time_str = signal.expected_resolution
                if time_str and time_str != "unknown":
                    try:
                        res_time = datetime.fromisoformat(
                            time_str.replace("Z", "+00:00")
                        )
                        delta = res_time - datetime.now(timezone.utc)
                        if delta.total_seconds() <= 0:
                            time_str = "overdue (oracle pending)"
                        elif delta.total_seconds() < 3600:
                            time_str = f"{int(delta.total_seconds() / 60)}m"
                        else:
                            time_str = f"{delta.total_seconds() / 3600:.1f}h"
                    except (ValueError, TypeError):
                        pass

                logger.info(
                    "\U0001f7e2 [DRY RUN] LATE RES BUY\n"
                    "  Market: %s\n"
                    "  Side: %s @ $%.3f\n"
                    "  Size: $%.2f (%.1f shares)\n"
                    "  Confidence: %d/100\n"
                    "  Edge: %.1f%% net of fees\n"
                    "  Expected resolution: %s",
                    question[:60], side, best_price,
                    signal.position_size_usd, shares,
                    conf, net_edge * 100, time_str,
                )

                # Telegram buy alert
                await self._send_buy_alert(signal, shares, time_str)

            self._scans_completed += 1
            logger.info(
                "LateRes scan #%d: %d candidates, %d signals from %d markets",
                self._scans_completed, len(candidates),
                signals_this_scan, len(markets),
            )
        except Exception as exc:
            logger.error("LateRes scan error: %s", exc)

    # ------------------------------------------------------------------
    # Position Monitor Loop
    # ------------------------------------------------------------------

    async def _fetch_market_state(self, condition_id: str) -> Optional[dict]:
        """Fetch current market state from Gamma API."""
        session = await self._get_session()
        url = f"{config.POLY_GAMMA_API_URL}/markets/{condition_id}"
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                logger.warning(
                    "LateRes monitor: Gamma HTTP %d for %s",
                    resp.status, condition_id[:16],
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error("LateRes monitor: fetch error for %s: %s", condition_id[:16], exc)
        return None

    def _hold_duration_str(self, pos: dict) -> str:
        """Return human-readable hold duration for a position."""
        try:
            entry_time = datetime.fromisoformat(pos["entry_time"])
            duration = datetime.now(timezone.utc) - entry_time
            hours = duration.total_seconds() / 3600
            if hours < 1:
                return f"{int(duration.total_seconds() / 60)}m"
            return f"{hours:.1f}h"
        except (ValueError, TypeError, KeyError):
            return "unknown"

    async def _monitor_positions(self) -> None:
        """Check each open position for resolution or price drops."""
        open_positions = [
            p for p in self._positions if p.get("status") == "open"
        ]
        if not open_positions:
            return

        for pos in open_positions:
            cid = pos.get("condition_id") or pos.get("market_id", "")
            if not cid:
                continue

            market_data = await self._fetch_market_state(cid)
            if market_data is None:
                continue

            # Determine current price on the position's side
            current_price = self._parse_price(market_data, pos["side"].lower())
            is_closed = (
                market_data.get("closed", False)
                or str(market_data.get("active", "true")).lower() == "false"
            )

            if current_price is None and not is_closed:
                continue

            entry = pos["entry_price"]
            shares = pos["shares"]
            dur_str = self._hold_duration_str(pos)
            now_iso = datetime.now(timezone.utc).isoformat()

            # --- Resolution: WIN (price >= 0.99 and market closed) ---
            if is_closed and current_price is not None and current_price >= 0.99:
                pnl = shares * (1.0 - entry)
                pnl_pct = (1.0 - entry) / entry if entry > 0 else 0.0
                pos["status"] = "closed_win"
                pos["exit_price"] = 1.0
                pos["realised_pnl"] = round(pnl, 2)
                pos["closed_at"] = now_iso
                self._sim_wins += 1
                self._sim_trades += 1
                self._sim_total_pnl += pnl
                save_positions(self._positions)
                logger.info(
                    "\u2705 [DRY RUN] LATE RES CLOSED \u2014 WIN\n"
                    "  Market: %s\n"
                    "  Entry: $%.3f \u2192 Exit: $1.000\n"
                    "  P&L: +$%.2f (%.1f%%)\n"
                    "  Hold: %s | Shares: %.1f",
                    pos["question"][:60], entry, pnl, pnl_pct * 100, dur_str, shares,
                )
                await self._send_win_alert(pos, pnl, pnl_pct, dur_str)
                continue

            # --- Resolution: LOSS (price <= 0.01 and market closed) ---
            if is_closed and current_price is not None and current_price <= 0.01:
                pnl = shares * (0.0 - entry)
                pnl_pct = -1.0  # total loss
                pos["status"] = "closed_loss"
                pos["exit_price"] = 0.0
                pos["realised_pnl"] = round(pnl, 2)
                pos["closed_at"] = now_iso
                self._sim_trades += 1
                self._sim_total_pnl += pnl
                save_positions(self._positions)
                logger.info(
                    "\u274c [DRY RUN] LATE RES CLOSED \u2014 LOSS\n"
                    "  Market: %s\n"
                    "  Entry: $%.3f \u2192 Exit: $0.000\n"
                    "  P&L: -$%.2f (%.1f%%)\n"
                    "  Hold: %s | Shares: %.1f\n"
                    "  NOTE: Check for oracle dispute",
                    pos["question"][:60], entry, abs(pnl), abs(pnl_pct) * 100,
                    dur_str, shares,
                )
                await self._send_loss_alert(pos, pnl, pnl_pct, dur_str)
                continue

            # --- Market closed but ambiguous price ---
            if is_closed:
                # Resolved to intermediate price (unusual) — treat as loss
                exit_price = current_price if current_price is not None else 0.0
                pnl = shares * (exit_price - entry)
                pnl_pct = (exit_price - entry) / entry if entry > 0 else 0.0
                status = "closed_win" if pnl >= 0 else "closed_loss"
                pos["status"] = status
                pos["exit_price"] = exit_price
                pos["realised_pnl"] = round(pnl, 2)
                pos["closed_at"] = now_iso
                self._sim_trades += 1
                self._sim_total_pnl += pnl
                if pnl >= 0:
                    self._sim_wins += 1
                save_positions(self._positions)
                logger.info(
                    "LateRes: position closed at ambiguous price %.3f | "
                    "pnl=$%.2f | %s",
                    exit_price, pnl, pos["question"][:40],
                )
                if pnl >= 0:
                    await self._send_win_alert(pos, pnl, pnl_pct, dur_str)
                else:
                    await self._send_loss_alert(pos, pnl, pnl_pct, dur_str)
                continue

            # --- Price drop warning (>3c below entry while still open) ---
            if current_price is not None and (entry - current_price) > 0.03:
                drop_pct = (entry - current_price) / entry
                # Only warn once per position per 30 min (use a flag)
                last_warn = pos.get("_last_drop_warn", 0.0)
                if time.time() - last_warn > 1800:
                    pos["_last_drop_warn"] = time.time()
                    logger.warning(
                        "\u26a0\ufe0f [DRY RUN] LATE RES WARNING\n"
                        "  Market: %s\n"
                        "  Entry: $%.3f | Current: $%.3f\n"
                        "  Drop: %.1f%% \u2014 possible dispute forming",
                        pos["question"][:60], entry, current_price, drop_pct * 100,
                    )
                    await self._send_drop_warning(pos, current_price, drop_pct)

            # Brief pause between API calls for rate limiting
            await asyncio.sleep(0.5)

    # ------------------------------------------------------------------
    # Daily Summary Loop
    # ------------------------------------------------------------------

    async def _daily_summary_loop(self) -> None:
        """Send a daily P&L summary at 00:00 UTC."""
        try:
            while self._running:
                await asyncio.sleep(60)  # check every minute
                if not self._running:
                    break
                now = datetime.now(timezone.utc)
                if now.hour == 0 and now.minute == 0:
                    await self._send_daily_summary()
                    await asyncio.sleep(61)  # avoid double-send
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Run Loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main loop: scan for candidates + monitor open positions."""
        self._running = True
        logger.info(
            "LateResolutionAgent starting | DRY_RUN=%s | scan_interval=%ds | "
            "open_positions=%d",
            self.dry_run, self.scan_interval, self._open_position_count(),
        )

        # Launch daily summary loop as a child task
        summary_task = asyncio.create_task(
            self._daily_summary_loop(), name="late_res_daily_summary"
        )

        try:
            while self._running:
                scan_start = time.monotonic()

                # 1. Scan for new candidates
                await self._run_scan()

                # 2. Monitor open positions for resolution / price drops
                await self._monitor_positions()

                elapsed = time.monotonic() - scan_start
                sleep_time = max(0, self.scan_interval - elapsed)
                await asyncio.sleep(sleep_time)
        except asyncio.CancelledError:
            logger.info("LateResolutionAgent cancelled")
        finally:
            self._running = False
            summary_task.cancel()
            await asyncio.gather(summary_task, return_exceptions=True)
            await self._close_session()
            logger.info(
                "LateResolutionAgent stopped | scans=%d | signals=%d | "
                "sim_trades=%d | sim_wins=%d | sim_pnl=$%.2f",
                self._scans_completed, self._signals_found,
                self._sim_trades, self._sim_wins, self._sim_total_pnl,
            )

    async def stop(self) -> None:
        """Gracefully stop the agent."""
        self._running = False

    def get_status(self) -> dict:
        return {
            "agent": "late_res",
            "running": self._running,
            "dry_run": self.dry_run,
            "scans_completed": self._scans_completed,
            "signals_found": self._signals_found,
            "open_positions": self._open_position_count(),
            "total_exposure": self._total_open_exposure(),
            "sim_total_pnl": self._sim_total_pnl,
            "sim_trades": self._sim_trades,
        }
