"""
Tests for agents/negrisk_agent.py — NegRisk Guaranteed Arbitrage Agent.

Test cases:
 1.  sum < 1.0 → YES arb identified
 2.  sum > 1.0 → NO arb identified
 3.  Edge below MIN_EDGE → filtered out
 4.  Liquidity below MIN_LIQUIDITY → filtered out
 5.  Position sizing respects MAX_POSITION_PCT
 6.  Single-outcome market → skipped (need 2+ outcomes)
 7.  Two-outcome market with perfect pricing (sum == 1.0) → skipped
 8.  Fee calculation correctly reduces edge
 9.  ArbSignal dataclass fields all populated correctly
10.  DRY_RUN mode logs but does not call signal_callback
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.negrisk_agent import NegRiskAgent, ArbSignal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_market(condition_id: str, yes_price: float, liquidity: float = 500_000) -> dict:
    """Helper to create a minimal Gamma-style market dict."""
    return {
        "conditionId": condition_id,
        "outcomePrices": [str(yes_price), str(round(1.0 - yes_price, 4))],
        "liquidity": liquidity,
        "volume": liquidity,
    }


def make_event(title: str, markets: list[dict], event_id: str = "evt1") -> dict:
    """Helper to create a minimal Gamma-style event dict."""
    return {"id": event_id, "title": title, "markets": markets}


@pytest.fixture
def agent():
    return NegRiskAgent(dry_run=True, portfolio_value=5000.0)


# ---------------------------------------------------------------------------
# Test 1: sum < 1.0 → YES arb
# ---------------------------------------------------------------------------

def test_yes_arb_detected_when_sum_below_one(agent):
    """sum < 1.0 should produce arb_type='YES'."""
    events = [make_event("Election 2024", [
        make_market("cid_A", 0.35),
        make_market("cid_B", 0.28),
        make_market("cid_C", 0.20),
    ])]
    # sum = 0.83 → YES arb
    signals = agent.find_arb_opportunities(events)
    assert len(signals) == 1
    assert signals[0].arb_type == "YES"
    assert signals[0].sum_of_prices == pytest.approx(0.83, abs=1e-4)


# ---------------------------------------------------------------------------
# Test 2: sum > 1.0 → NO arb
# ---------------------------------------------------------------------------

def test_no_arb_detected_when_sum_above_one(agent):
    """sum > 1.0 should produce arb_type='NO'."""
    # 2 legs → total_fee = 0.02 * 2 = 0.04
    # Need deviation > MIN_EDGE + total_fee = 0.03 + 0.04 = 0.07
    # sum = 1.10 → deviation = 0.10 > 0.07 → edge = 0.06 > 0.03 ✓
    events = [make_event("Sports Market", [
        make_market("cid_A", 0.55),
        make_market("cid_B", 0.55),
    ])]
    # sum = 1.10 → NO arb
    signals = agent.find_arb_opportunities(events)
    assert len(signals) == 1
    assert signals[0].arb_type == "NO"
    assert signals[0].sum_of_prices == pytest.approx(1.10, abs=1e-4)


# ---------------------------------------------------------------------------
# Test 3: edge below MIN_EDGE → filtered out
# ---------------------------------------------------------------------------

def test_low_edge_filtered(agent):
    """Opportunities with edge ≤ MIN_EDGE (after fees) should not appear."""
    # MIN_EDGE = 0.03, FEE_RATE = 0.02, num_legs = 2 → total_fee = 0.04
    # For edge > MIN_EDGE need: abs(1.0 - sum) > 0.03 + 0.04 = 0.07
    # sum = 0.95 → deviation = 0.05 → edge = 0.05 - 0.04 = 0.01 < 0.03
    events = [make_event("Small Edge Market", [
        make_market("cid_A", 0.47),
        make_market("cid_B", 0.48),
    ])]
    signals = agent.find_arb_opportunities(events)
    assert len(signals) == 0


# ---------------------------------------------------------------------------
# Test 4: liquidity below MIN_LIQUIDITY → filtered out
# ---------------------------------------------------------------------------

def test_low_liquidity_filtered(agent):
    """Markets with liquidity below MIN_LIQUIDITY should be skipped."""
    # Great edge but terrible liquidity
    events = [make_event("Illiquid Market", [
        make_market("cid_A", 0.20, liquidity=50_000),   # below $100k
        make_market("cid_B", 0.20, liquidity=50_000),
        make_market("cid_C", 0.20, liquidity=50_000),
    ])]
    signals = agent.find_arb_opportunities(events)
    assert len(signals) == 0


# ---------------------------------------------------------------------------
# Test 5: position sizing respects MAX_POSITION_PCT
# ---------------------------------------------------------------------------

def test_position_size_respects_max_position_pct(agent):
    """Size per leg × num_legs must not exceed NEGRISK_MAX_POSITION_PCT × portfolio."""
    import config

    num_legs = 4
    max_total = agent.portfolio_value * config.NEGRISK_MAX_POSITION_PCT
    max_per_leg = max_total / num_legs

    size = agent.calculate_position_size(
        signal=None,
        portfolio_value=agent.portfolio_value,
        num_legs=num_legs,
        min_leg_liquidity=1_000_000,
    )

    assert size <= max_per_leg + 0.01  # small float tolerance
    total_exposure = size * num_legs
    assert total_exposure <= max_total + 0.01


# ---------------------------------------------------------------------------
# Test 6: single-outcome market → skipped
# ---------------------------------------------------------------------------

def test_single_outcome_market_skipped(agent):
    """Events with only one market should be ignored (no arb possible)."""
    events = [make_event("Single Outcome", [
        make_market("cid_A", 0.30),
    ])]
    signals = agent.find_arb_opportunities(events)
    assert len(signals) == 0


# ---------------------------------------------------------------------------
# Test 7: two-outcome market with perfect pricing → skipped
# ---------------------------------------------------------------------------

def test_perfect_pricing_skipped(agent):
    """When sum == 1.0 exactly, there is no arb (edge = 0 - fees < 0)."""
    events = [make_event("Perfect Pricing", [
        make_market("cid_A", 0.50),
        make_market("cid_B", 0.50),
    ])]
    signals = agent.find_arb_opportunities(events)
    assert len(signals) == 0


# ---------------------------------------------------------------------------
# Test 8: fee calculation correctly reduces edge
# ---------------------------------------------------------------------------

def test_fee_reduces_edge_correctly(agent):
    """
    With sum=0.85 and 3 legs:
      raw_deviation = |1.0 - 0.85| = 0.15
      total_fee = FEE_RATE * num_legs = 0.02 * 3 = 0.06
      expected_edge = 0.15 - 0.06 = 0.09
    """
    events = [make_event("Fee Test", [
        make_market("cid_A", 0.30),
        make_market("cid_B", 0.30),
        make_market("cid_C", 0.25),
    ])]
    signals = agent.find_arb_opportunities(events)
    assert len(signals) == 1
    expected_edge = abs(1.0 - 0.85) - (agent.FEE_RATE * 3)
    assert signals[0].edge_after_fees == pytest.approx(expected_edge, abs=1e-4)


# ---------------------------------------------------------------------------
# Test 9: ArbSignal dataclass fields all populated correctly
# ---------------------------------------------------------------------------

def test_arb_signal_fields_populated(agent):
    """All ArbSignal fields should be present and have correct types."""
    events = [make_event("Governor Race", [
        make_market("cid_A", 0.20),
        make_market("cid_B", 0.20),
        make_market("cid_C", 0.20),
    ], event_id="evt_gov")]
    signals = agent.find_arb_opportunities(events)
    assert len(signals) == 1

    sig = signals[0]
    assert isinstance(sig.event_name, str) and sig.event_name
    assert isinstance(sig.event_id, str)
    assert isinstance(sig.markets, list) and len(sig.markets) == 3
    assert sig.arb_type in ("YES", "NO")
    assert isinstance(sig.sum_of_prices, float)
    assert isinstance(sig.edge_after_fees, float)
    assert isinstance(sig.recommended_size_per_leg, float)
    assert isinstance(sig.guaranteed_profit_usd, float)
    assert sig.detected_at is not None

    # Each leg should be a 3-tuple: (condition_id, yes_price, liquidity)
    for leg in sig.markets:
        assert len(leg) == 3
        cid, price, liq = leg
        assert isinstance(cid, str)
        assert 0.0 <= price <= 1.0
        assert liq >= 0.0


# ---------------------------------------------------------------------------
# Test 10: DRY_RUN mode logs but does NOT call signal_callback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_does_not_call_signal_callback():
    """In DRY_RUN mode, signal_callback must never be invoked."""
    callback = MagicMock()
    agent = NegRiskAgent(dry_run=True, signal_callback=callback, portfolio_value=5000.0)

    # Provide one strong arb opportunity
    events = [make_event("Dry Run Test", [
        make_market("cid_A", 0.20),
        make_market("cid_B", 0.20),
        make_market("cid_C", 0.20),
    ])]

    # Patch fetch to return our events
    agent.fetch_grouped_markets = AsyncMock(return_value=events)

    # Run one scan cycle
    await agent._run_scan()

    # Callback must NOT be called in DRY_RUN mode
    callback.assert_not_called()


# ---------------------------------------------------------------------------
# Bonus: Multiple events — correct count
# ---------------------------------------------------------------------------

def test_multiple_events_multiple_signals(agent):
    """Multiple qualifying events should produce multiple signals."""
    # 2 legs: total_fee=0.04, need deviation > 0.03+0.04=0.07
    # 3 legs: total_fee=0.06, need deviation > 0.03+0.06=0.09
    # Event A: sum=0.60 (3 legs) → deviation=0.40 > 0.09 ✓ YES arb
    # Event B: sum=1.10 (2 legs) → deviation=0.10 > 0.07 ✓ NO arb
    # Event C: sum=0.98 (2 legs) → deviation=0.02 < 0.07 → skipped
    events = [
        make_event("Event A", [
            make_market("A1", 0.20),
            make_market("A2", 0.20),
            make_market("A3", 0.20),
        ]),
        make_event("Event B", [
            make_market("B1", 0.55),
            make_market("B2", 0.55),  # sum = 1.10 → edge = 0.06 > MIN_EDGE
        ]),
        make_event("Event C", [
            make_market("C1", 0.49),  # sum = 0.98 → deviation=0.02 → edge=-0.02 → skipped
            make_market("C2", 0.49),
        ]),
    ]
    signals = agent.find_arb_opportunities(events)
    # Event A: YES arb, Event B: NO arb, Event C: skipped
    assert len(signals) == 2
