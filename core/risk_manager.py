"""
core/risk_manager.py — Risk Management Engine

The most critical module in the bot. Controls position sizing, enforces loss
limits, and prevents portfolio blowups. Every trade decision passes through here.

Key responsibilities:
  - Kelly criterion position sizing (quarter-Kelly)
  - Daily loss limit enforcement with kill-switch
  - Maximum concurrent position tracking
  - Per-market exposure caps
  - Portfolio value tracking
  - Emergency exit trigger
  - Full audit logging of every risk decision
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """Represents an open position on a Polymarket market."""
    market_id: str
    side: str                  # "YES" or "NO"
    size_usd: float
    entry_price: float         # probability 0-1
    current_price: float       # updated in real time
    opened_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def unrealised_pnl(self) -> float:
        """Current unrealised PnL in USD."""
        price_delta = self.current_price - self.entry_price
        if self.side == "NO":
            price_delta = -price_delta
        # Rough linear approx: PnL = shares * price_delta
        if self.entry_price <= 0:
            return 0.0
        shares = self.size_usd / self.entry_price
        return shares * price_delta


@dataclass
class RiskDecision:
    """Result returned by every risk check."""
    approved: bool
    reason: str
    recommended_size_usd: float = 0.0
    kelly_fraction_used: float = 0.0


# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------

class RiskManager:
    """
    Centralised risk management for the Polymarket bot.

    All position-sizing and kill-switch logic lives here.
    Designed for single-process async use.
    """

    def __init__(self, capital_usd: float = config.TOTAL_CAPITAL_USD):
        self.capital_usd: float = capital_usd
        self.portfolio_value_usd: float = capital_usd

        # Tracking
        self.open_positions: dict[str, Position] = {}   # market_id -> Position
        self.daily_realised_pnl: float = 0.0
        self.daily_loss_start: date = date.today()
        self.total_wins: int = 0
        self.total_losses: int = 0

        # Per-trade history (populated on close_position) — used for /summary
        self._trade_log: list[dict] = []

        # Kill switch
        self.kill_switch_active: bool = False
        self.kill_switch_reason: str = ""

        logger.info(
            "RiskManager initialised | capital=%.2f | daily_loss_limit=%.2f | "
            "max_positions=%d | DRY_RUN=%s",
            self.capital_usd,
            config.DAILY_LOSS_LIMIT_USD,
            config.MAX_OPEN_POSITIONS,
            config.DRY_RUN,
        )

    # ------------------------------------------------------------------
    # Kill Switch
    # ------------------------------------------------------------------

    def trigger_kill_switch(self, reason: str) -> None:
        """Activate the kill switch — stops all new trades."""
        if not self.kill_switch_active:
            self.kill_switch_active = True
            self.kill_switch_reason = reason
            logger.critical(
                "KILL SWITCH ACTIVATED: %s | daily_pnl=%.2f",
                reason,
                self.daily_realised_pnl,
            )

    def reset_kill_switch(self) -> None:
        """Manually reset the kill switch (requires explicit call)."""
        logger.warning("Kill switch reset by operator")
        self.kill_switch_active = False
        self.kill_switch_reason = ""

    # ------------------------------------------------------------------
    # Daily PnL Reset
    # ------------------------------------------------------------------

    def _maybe_reset_daily_pnl(self) -> None:
        """Reset daily PnL tracker at UTC midnight."""
        today = date.today()
        if today != self.daily_loss_start:
            logger.info(
                "New trading day — resetting daily PnL (previous: %.2f)",
                self.daily_realised_pnl,
            )
            self.daily_realised_pnl = 0.0
            self.daily_loss_start = today
            # Kill switch does NOT auto-reset — requires operator action

    # ------------------------------------------------------------------
    # Kelly Criterion
    # ------------------------------------------------------------------

    def kelly_position_size(
        self,
        win_probability: float,
        win_payout_multiple: float,
    ) -> float:
        """
        Calculate quarter-Kelly position size in USD.

        Args:
            win_probability:    Estimated probability of winning (0-1).
            win_payout_multiple: Payout multiple if we win (e.g. 2.0 = 2x stake).

        Returns:
            Recommended position size in USD (0 if Kelly is negative or zero).
        """
        if win_probability <= 0 or win_probability >= 1:
            return 0.0
        if win_payout_multiple <= 1:
            return 0.0

        # Full Kelly: f* = (b*p - q) / b
        b = win_payout_multiple - 1   # net odds
        p = win_probability
        q = 1.0 - p
        full_kelly = (b * p - q) / b

        if full_kelly <= 0:
            logger.debug("Kelly is negative (%.4f) — no bet", full_kelly)
            return 0.0

        quarter_kelly = full_kelly * config.KELLY_FRACTION
        size_usd = self.portfolio_value_usd * quarter_kelly

        logger.debug(
            "Kelly sizing | p=%.3f | payout=%.2f | full_kelly=%.4f | "
            "quarter_kelly=%.4f | size_usd=%.2f",
            win_probability,
            win_payout_multiple,
            full_kelly,
            quarter_kelly,
            size_usd,
        )
        return size_usd

    # ------------------------------------------------------------------
    # Position Sizing (main entry point)
    # ------------------------------------------------------------------

    def calculate_position_size(
        self,
        market_id: str,
        win_probability: float,
        market_price: float,
        edge: float,
    ) -> RiskDecision:
        """
        Full position sizing with all guardrails applied.

        Args:
            market_id:       Polymarket market identifier.
            win_probability: Bot's estimated fair probability.
            market_price:    Current market price (probability 0-1).
            edge:            Estimated edge (fair_value - market_price).

        Returns:
            RiskDecision with approved flag and recommended size.
        """
        self._maybe_reset_daily_pnl()

        # Kill switch check
        if self.kill_switch_active:
            return RiskDecision(
                approved=False,
                reason=f"Kill switch active: {self.kill_switch_reason}",
            )

        # Minimum edge check
        if abs(edge) < config.MIN_KELLY_EDGE:
            return RiskDecision(
                approved=False,
                reason=f"Edge {edge:.4f} below minimum {config.MIN_KELLY_EDGE}",
            )

        # Position count check
        if len(self.open_positions) >= config.MAX_OPEN_POSITIONS:
            return RiskDecision(
                approved=False,
                reason=(
                    f"Max open positions reached "
                    f"({len(self.open_positions)}/{config.MAX_OPEN_POSITIONS})"
                ),
            )

        # Daily loss check
        if self.daily_realised_pnl <= -config.DAILY_LOSS_LIMIT_USD:
            self.trigger_kill_switch(
                f"Daily loss limit hit: ${self.daily_realised_pnl:.2f}"
            )
            return RiskDecision(
                approved=False,
                reason=f"Daily loss limit exceeded: ${self.daily_realised_pnl:.2f}",
            )

        # Drawdown warning (non-blocking)
        warning_threshold = -(config.DAILY_LOSS_LIMIT_USD * config.DRAWDOWN_WARNING_PCT)
        if self.daily_realised_pnl <= warning_threshold:
            logger.warning(
                "DRAWDOWN WARNING: daily_pnl=%.2f (%.0f%% of limit)",
                self.daily_realised_pnl,
                abs(self.daily_realised_pnl) / config.DAILY_LOSS_LIMIT_USD * 100,
            )

        # Portfolio drawdown check
        if self.capital_usd > 0:
            drawdown = (self.capital_usd - self.portfolio_value_usd) / self.capital_usd
            if drawdown >= config.MAX_DRAWDOWN_PCT:
                self.trigger_kill_switch(
                    f"Max portfolio drawdown reached: {drawdown:.2%}"
                )
                return RiskDecision(
                    approved=False,
                    reason=f"Portfolio drawdown {drawdown:.2%} exceeds limit",
                )

        # Per-market exposure check
        market_exposure = self._market_exposure(market_id)
        max_market_exposure = (
            self.portfolio_value_usd * config.MAX_EXPOSURE_PER_MARKET_PCT
        )
        if market_exposure >= max_market_exposure:
            return RiskDecision(
                approved=False,
                reason=(
                    f"Market exposure cap hit: "
                    f"${market_exposure:.2f} >= ${max_market_exposure:.2f}"
                ),
            )

        # Kelly sizing — binary market: payout = 1/market_price
        if market_price <= 0:
            return RiskDecision(approved=False, reason="Invalid market price (0)")
        win_payout = 1.0 / market_price

        kelly_size = self.kelly_position_size(win_probability, win_payout)
        if kelly_size <= 0:
            return RiskDecision(
                approved=False,
                reason=f"Kelly size is zero or negative for edge={edge:.4f}",
            )

        # Apply hard caps
        available_market_room = max_market_exposure - market_exposure
        max_trade = min(config.MAX_CAPITAL_PER_TRADE_USD, available_market_room)
        final_size = min(kelly_size, max_trade)

        if final_size <= 0:
            return RiskDecision(
                approved=False,
                reason="Final size is zero after applying caps",
            )

        logger.info(
            "Risk APPROVED | market=%s | size=%.2f | kelly=%.2f | edge=%.4f | "
            "daily_pnl=%.2f | positions=%d",
            market_id,
            final_size,
            kelly_size,
            edge,
            self.daily_realised_pnl,
            len(self.open_positions),
        )

        return RiskDecision(
            approved=True,
            reason="All checks passed",
            recommended_size_usd=final_size,
            kelly_fraction_used=config.KELLY_FRACTION,
        )

    # ------------------------------------------------------------------
    # Position Tracking
    # ------------------------------------------------------------------

    def open_position(self, position: Position) -> None:
        """Register a new open position."""
        self.open_positions[position.market_id] = position
        logger.info(
            "Position opened | market=%s | side=%s | size=%.2f | price=%.4f",
            position.market_id,
            position.side,
            position.size_usd,
            position.entry_price,
        )

    def close_position(self, market_id: str, exit_price: float) -> Optional[float]:
        """
        Close a position and record realised PnL.

        Returns the realised PnL in USD, or None if position not found.
        """
        position = self.open_positions.pop(market_id, None)
        if position is None:
            logger.warning("close_position: market_id=%s not found", market_id)
            return None

        position.current_price = exit_price
        realised_pnl = position.unrealised_pnl
        self.daily_realised_pnl += realised_pnl
        self.portfolio_value_usd += realised_pnl

        if realised_pnl >= 0:
            self.total_wins += 1
        else:
            self.total_losses += 1

        logger.info(
            "Position closed | market=%s | side=%s | pnl=%.2f | "
            "daily_pnl=%.2f | portfolio=%.2f",
            market_id,
            position.side,
            realised_pnl,
            self.daily_realised_pnl,
            self.portfolio_value_usd,
        )

        self._trade_log.append({
            "market_id": market_id,
            "side": position.side,
            "size_usd": position.size_usd,
            "pnl_usd": realised_pnl,
            "closed_at": datetime.utcnow(),
        })

        # Check if kill switch should trigger post-close
        if self.daily_realised_pnl <= -config.DAILY_LOSS_LIMIT_USD:
            self.trigger_kill_switch(
                f"Daily loss limit hit after close: ${self.daily_realised_pnl:.2f}"
            )

        return realised_pnl

    def update_position_price(self, market_id: str, current_price: float) -> None:
        """Update the current market price for an open position."""
        if market_id in self.open_positions:
            self.open_positions[market_id].current_price = current_price

    # ------------------------------------------------------------------
    # Portfolio Summary
    # ------------------------------------------------------------------

    def total_unrealised_pnl(self) -> float:
        """Sum of unrealised PnL across all open positions."""
        return sum(p.unrealised_pnl for p in self.open_positions.values())

    def total_exposure(self) -> float:
        """Total capital currently at risk."""
        return sum(p.size_usd for p in self.open_positions.values())

    def _market_exposure(self, market_id: str) -> float:
        """Current USD exposure in a specific market."""
        pos = self.open_positions.get(market_id)
        return pos.size_usd if pos else 0.0

    def get_trades_since(self, cutoff: datetime) -> list[dict]:
        """Return closed trades recorded since the given UTC datetime."""
        return [t for t in self._trade_log if t["closed_at"] >= cutoff]

    def get_portfolio_summary(self) -> dict:
        """Return a serialisable summary of portfolio state."""
        drawdown = 0.0
        if self.capital_usd > 0:
            drawdown = round(
                (self.capital_usd - self.portfolio_value_usd) / self.capital_usd, 4
            )
        return {
            "portfolio_value_usd": round(self.portfolio_value_usd, 2),
            "capital_usd": round(self.capital_usd, 2),
            "daily_realised_pnl": round(self.daily_realised_pnl, 2),
            "unrealised_pnl": round(self.total_unrealised_pnl(), 2),
            "total_exposure": round(self.total_exposure(), 2),
            "open_positions": len(self.open_positions),
            "kill_switch_active": self.kill_switch_active,
            "kill_switch_reason": self.kill_switch_reason,
            "drawdown_pct": drawdown,
            "total_wins": self.total_wins,
            "total_losses": self.total_losses,
        }
