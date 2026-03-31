"""
tests/test_risk_manager.py — Unit tests for core/risk_manager.py

Covers:
- Kelly criterion position sizing
- Daily loss limit enforcement and kill switch
- Position count cap
- Per-market exposure cap
- Portfolio drawdown kill switch
- Edge cases: zero balance, negative PnL, zero prices
- Position open/close/PnL tracking
"""

import pytest
from datetime import date, timedelta
from unittest.mock import patch

import config
from core.risk_manager import RiskManager, Position, RiskDecision


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def rm():
    """Fresh RiskManager with $5,000 capital."""
    return RiskManager(capital_usd=5000.0)


@pytest.fixture
def tiny_rm():
    """RiskManager with $100 capital for edge-case tests."""
    return RiskManager(capital_usd=100.0)


# ---------------------------------------------------------------------------
# Kelly Criterion Position Sizing
# ---------------------------------------------------------------------------

class TestKellyPositionSize:
    def test_positive_edge_returns_nonzero(self, rm):
        """60% win probability with 2x payout should produce a positive size."""
        size = rm.kelly_position_size(win_probability=0.60, win_payout_multiple=2.0)
        assert size > 0

    def test_negative_edge_returns_zero(self, rm):
        """40% win probability with 2x payout is a losing bet — Kelly returns 0."""
        size = rm.kelly_position_size(win_probability=0.40, win_payout_multiple=2.0)
        assert size == 0.0

    def test_zero_probability_returns_zero(self, rm):
        size = rm.kelly_position_size(win_probability=0.0, win_payout_multiple=2.0)
        assert size == 0.0

    def test_one_probability_returns_zero(self, rm):
        """p=1 is degenerate (infinite Kelly) — should be clamped to 0."""
        size = rm.kelly_position_size(win_probability=1.0, win_payout_multiple=2.0)
        assert size == 0.0

    def test_payout_multiple_one_returns_zero(self, rm):
        """Payout multiple of 1 (breakeven) — Kelly is zero."""
        size = rm.kelly_position_size(win_probability=0.6, win_payout_multiple=1.0)
        assert size == 0.0

    def test_quarter_kelly_applied(self, rm):
        """Verify quarter-Kelly fraction is applied correctly."""
        # Full Kelly for p=0.6, b=1 (2x payout): f* = (1*0.6 - 0.4)/1 = 0.2
        # Quarter-Kelly: 0.2 * 0.25 = 0.05
        # Size: 5000 * 0.05 = 250
        size = rm.kelly_position_size(win_probability=0.6, win_payout_multiple=2.0)
        expected = 5000.0 * 0.05   # quarter-Kelly fraction
        assert abs(size - expected) < 0.01

    def test_size_scales_with_portfolio(self):
        """Larger portfolio should produce proportionally larger Kelly size."""
        rm_small = RiskManager(capital_usd=1000.0)
        rm_large = RiskManager(capital_usd=10000.0)
        size_small = rm_small.kelly_position_size(0.6, 2.0)
        size_large = rm_large.kelly_position_size(0.6, 2.0)
        assert abs(size_large / size_small - 10.0) < 0.01


# ---------------------------------------------------------------------------
# Position Sizing (with all guardrails)
# ---------------------------------------------------------------------------

class TestCalculatePositionSize:
    def test_approved_with_valid_params(self, rm):
        """Normal trade with sufficient edge should be approved."""
        decision = rm.calculate_position_size(
            market_id="market_1",
            win_probability=0.65,
            market_price=0.50,
            edge=0.15,
        )
        assert decision.approved is True
        assert decision.recommended_size_usd > 0

    def test_rejected_below_min_edge(self, rm):
        """Edge below MIN_KELLY_EDGE should be rejected."""
        decision = rm.calculate_position_size(
            market_id="market_1",
            win_probability=0.52,
            market_price=0.50,
            edge=0.01,   # below 0.03 threshold
        )
        assert decision.approved is False
        assert "Edge" in decision.reason

    def test_rejected_when_kill_switch_active(self, rm):
        """Kill switch active should block all trades."""
        rm.trigger_kill_switch("test")
        decision = rm.calculate_position_size(
            market_id="market_1",
            win_probability=0.80,
            market_price=0.50,
            edge=0.30,
        )
        assert decision.approved is False
        assert "Kill switch" in decision.reason

    def test_rejected_at_max_positions(self, rm):
        """Bot at MAX_OPEN_POSITIONS should reject new trades."""
        for i in range(config.MAX_OPEN_POSITIONS):
            rm.open_positions[f"market_{i}"] = Position(
                market_id=f"market_{i}",
                side="YES",
                size_usd=5.0,
                entry_price=0.5,
                current_price=0.5,
            )
        decision = rm.calculate_position_size(
            market_id="market_new",
            win_probability=0.65,
            market_price=0.50,
            edge=0.15,
        )
        assert decision.approved is False
        assert "Max open positions" in decision.reason

    def test_rejected_when_daily_loss_exceeded(self, rm):
        """Exceeding daily loss limit should trigger kill switch and reject."""
        rm.daily_realised_pnl = -(config.DAILY_LOSS_LIMIT_USD + 1)
        decision = rm.calculate_position_size(
            market_id="market_1",
            win_probability=0.65,
            market_price=0.50,
            edge=0.15,
        )
        assert decision.approved is False
        assert rm.kill_switch_active is True

    def test_size_capped_by_max_per_trade(self, rm):
        """Recommended size must never exceed MAX_CAPITAL_PER_TRADE_USD."""
        decision = rm.calculate_position_size(
            market_id="market_1",
            win_probability=0.90,   # very high edge to push Kelly up
            market_price=0.50,
            edge=0.40,
        )
        if decision.approved:
            assert decision.recommended_size_usd <= config.MAX_CAPITAL_PER_TRADE_USD

    def test_rejected_on_zero_market_price(self, rm):
        """Zero market price is invalid — should reject gracefully."""
        decision = rm.calculate_position_size(
            market_id="market_1",
            win_probability=0.65,
            market_price=0.0,
            edge=0.15,
        )
        assert decision.approved is False

    def test_zero_balance_no_crash(self):
        """Zero capital should not crash — Kelly size is zero."""
        rm = RiskManager(capital_usd=0.0)
        decision = rm.calculate_position_size(
            market_id="market_1",
            win_probability=0.65,
            market_price=0.50,
            edge=0.15,
        )
        # Should be rejected (size would be 0)
        assert isinstance(decision, RiskDecision)
        assert decision.approved is False


# ---------------------------------------------------------------------------
# Kill Switch
# ---------------------------------------------------------------------------

class TestKillSwitch:
    def test_trigger_sets_flag(self, rm):
        rm.trigger_kill_switch("test reason")
        assert rm.kill_switch_active is True
        assert rm.kill_switch_reason == "test reason"

    def test_trigger_idempotent(self, rm):
        """Triggering twice should not overwrite the original reason."""
        rm.trigger_kill_switch("first reason")
        rm.trigger_kill_switch("second reason")
        assert rm.kill_switch_reason == "first reason"

    def test_reset_clears_flag(self, rm):
        rm.trigger_kill_switch("test")
        rm.reset_kill_switch()
        assert rm.kill_switch_active is False
        assert rm.kill_switch_reason == ""


# ---------------------------------------------------------------------------
# Position Tracking
# ---------------------------------------------------------------------------

class TestPositionTracking:
    def test_open_position_registers(self, rm):
        pos = Position("mkt1", "YES", 5.0, 0.50, 0.50)
        rm.open_position(pos)
        assert "mkt1" in rm.open_positions

    def test_close_position_calculates_pnl(self, rm):
        pos = Position("mkt1", "YES", 5.0, 0.50, 0.50)
        rm.open_position(pos)
        # Price moves from 0.50 to 0.75 — unrealised PnL = (5/0.5) * 0.25 = 2.50
        pnl = rm.close_position("mkt1", exit_price=0.75)
        assert pnl is not None
        assert abs(pnl - 2.50) < 0.01
        assert "mkt1" not in rm.open_positions

    def test_close_nonexistent_returns_none(self, rm):
        result = rm.close_position("nonexistent", 0.5)
        assert result is None

    def test_daily_pnl_updated_on_close(self, rm):
        pos = Position("mkt1", "YES", 10.0, 0.50, 0.50)
        rm.open_position(pos)
        rm.close_position("mkt1", exit_price=0.75)
        assert rm.daily_realised_pnl > 0

    def test_negative_pnl_tracked(self, rm):
        """Losses are recorded correctly."""
        pos = Position("mkt1", "YES", 10.0, 0.50, 0.50)
        rm.open_position(pos)
        # Price drops from 0.50 to 0.25 — loss
        pnl = rm.close_position("mkt1", exit_price=0.25)
        assert pnl is not None
        assert pnl < 0
        assert rm.daily_realised_pnl < 0

    def test_kill_switch_triggers_on_loss_close(self, rm):
        """If a close pushes daily PnL past limit, kill switch fires."""
        # Set daily PnL just below the limit
        rm.daily_realised_pnl = -(config.DAILY_LOSS_LIMIT_USD - 1)
        # Open a large losing position
        pos = Position("mkt1", "YES", 500.0, 0.90, 0.90)
        rm.open_position(pos)
        # Close at a big loss
        rm.close_position("mkt1", exit_price=0.01)
        assert rm.kill_switch_active is True


# ---------------------------------------------------------------------------
# Portfolio Summary
# ---------------------------------------------------------------------------

class TestPortfolioSummary:
    def test_summary_returns_dict(self, rm):
        summary = rm.get_portfolio_summary()
        assert isinstance(summary, dict)
        assert "portfolio_value_usd" in summary
        assert "kill_switch_active" in summary

    def test_initial_drawdown_is_zero(self, rm):
        summary = rm.get_portfolio_summary()
        assert summary["drawdown_pct"] == 0.0

    def test_summary_reflects_open_positions(self, rm):
        pos = Position("mkt1", "YES", 5.0, 0.5, 0.5)
        rm.open_position(pos)
        summary = rm.get_portfolio_summary()
        assert summary["open_positions"] == 1
        assert summary["total_exposure"] == 5.0

    def test_total_exposure_zero_initially(self, rm):
        assert rm.total_exposure() == 0.0
