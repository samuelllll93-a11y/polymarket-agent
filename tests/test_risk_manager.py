"""
Tests for core/risk_manager.py.

Covers:
- Signal approval and rejection at edge thresholds
- Position sizing via Kelly fraction
- Daily drawdown circuit-breaker logic
- Max exposure limits
- Fill recording and state updates

STUB - tests to be implemented alongside the full RiskManager in a later task.
"""

import pytest
from core.risk_manager import RiskManager


SAMPLE_CONFIG = {
    "max_position_usdc": 50.0,
    "max_exposure_pct": 0.20,
    "daily_drawdown_limit_pct": 0.05,
    "min_edge_pct": 0.03,
    "kelly_fraction": 0.25,
}


@pytest.fixture
def risk_manager():
    return RiskManager(config=SAMPLE_CONFIG)


def test_signal_approved_above_edge(risk_manager):
    """A signal with sufficient edge should be approved."""
    raise NotImplementedError


def test_signal_rejected_below_edge(risk_manager):
    """A signal below the minimum edge threshold should be rejected."""
    raise NotImplementedError


def test_drawdown_halts_trading(risk_manager):
    """After hitting the daily drawdown limit, is_halted() should return True."""
    raise NotImplementedError


def test_position_sizing_respects_cap(risk_manager):
    """size_position() must never exceed max_position_usdc."""
    raise NotImplementedError


def test_reset_daily_pnl(risk_manager):
    """reset_daily_pnl() should clear the daily loss counter."""
    raise NotImplementedError
