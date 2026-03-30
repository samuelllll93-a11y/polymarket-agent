"""
Position sizing and risk management for the Polymarket trading bot.

Enforces capital preservation rules before any order reaches the CLOB:
- Maximum position size per market (absolute and % of bankroll)
- Maximum total open exposure across all positions
- Daily drawdown circuit-breaker (halts trading if daily loss exceeds limit)
- Minimum edge threshold (rejects signals below the configured edge)
- Correlation limits (prevents over-concentration in correlated markets)

STUB - full implementation built in a later task.
"""


class RiskManager:
    """Evaluates proposed trades against configurable risk limits."""

    def __init__(self, config: dict):
        """
        Args:
            config: Dict of risk parameters, typically loaded from config.py.
                    Expected keys: max_position_usdc, max_exposure_pct,
                    daily_drawdown_limit_pct, min_edge_pct.
        """
        self.config = config
        self._daily_pnl: float = 0.0
        self._open_positions: list[dict] = []

    def check_signal(self, signal: dict) -> tuple[bool, str]:
        """
        Validate a trade signal against all risk rules.

        Args:
            signal: Dict with keys: market_id, side, estimated_prob,
                    market_price, proposed_size_usdc.

        Returns:
            (approved: bool, reason: str) — reason explains approval or rejection.
        """
        raise NotImplementedError

    def size_position(self, edge: float, bankroll: float) -> float:
        """
        Compute optimal position size using a Kelly-fraction approach.

        Args:
            edge:     Estimated probability edge over market price (0–1).
            bankroll: Current available capital in USDC.

        Returns:
            Recommended position size in USDC, capped by risk limits.
        """
        raise NotImplementedError

    def record_fill(self, fill: dict) -> None:
        """Update internal state when an order is filled."""
        raise NotImplementedError

    def is_halted(self) -> bool:
        """Return True if the daily drawdown circuit-breaker has tripped."""
        raise NotImplementedError

    def reset_daily_pnl(self) -> None:
        """Reset daily P&L counter (called at start of each trading day)."""
        self._daily_pnl = 0.0
