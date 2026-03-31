"""
core/order_manager.py — Order Lifecycle Manager

Single gateway between agent signals and the CLOB client.

Responsibilities:
  - Receive validated signals, submit limit orders via CLOBClient
  - Track open orders and poll for fills every ORDER_MONITOR_INTERVAL_SEC
  - Cancel stale unfilled orders after ORDER_TIMEOUT_MINS
  - Persist order history to data/order_history.json
  - Log every order state transition
  - Emit fill/cancel events back to SignalRouter (via callbacks)

In DRY_RUN mode:
  - Simulates order placement (returns fake order_id)
  - Simulates 30% fill rate within the timeout window
  - Logs all actions — never touches the CLOB API
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import config

logger = logging.getLogger(__name__)

ORDER_HISTORY_FILE = Path(__file__).parent.parent / "data" / "order_history.json"
ORDER_TIMEOUT_MINS = int(getattr(config, "ORDER_TIMEOUT_MINS", 10))


# ---------------------------------------------------------------------------
# Order dataclass
# ---------------------------------------------------------------------------

@dataclass
class Order:
    """Represents a single Polymarket order throughout its lifecycle."""

    order_id: str
    market_id: str
    token_id: str
    side: str               # 'BUY' or 'SELL'
    price: float
    size_usd: float
    status: str = "pending" # pending / filled / cancelled / expired
    filled_at: Optional[float] = None
    cancelled_at: Optional[float] = None
    placed_at: float = field(default_factory=time.time)
    dry_run: bool = True
    agent: str = "unknown"

    @property
    def age_seconds(self) -> float:
        return time.time() - self.placed_at

    def to_dict(self) -> dict:
        d = asdict(self)
        d["placed_at_iso"] = datetime.fromtimestamp(self.placed_at, tz=timezone.utc).isoformat()
        return d


# ---------------------------------------------------------------------------
# OrderManager
# ---------------------------------------------------------------------------

class OrderManager:
    """
    Manages the full lifecycle of Polymarket orders.

    Usage:
        om = OrderManager(clob_client=client, risk_manager=rm, alerter=alerter)
        order_id = await om.submit_signal(signal)
        await om.run()  # background monitoring loop
    """

    def __init__(
        self,
        clob_client=None,
        risk_manager=None,
        alerter=None,
        dry_run: bool = config.DRY_RUN,
        on_fill: Optional[Callable[[Order], Any]] = None,
        on_cancel: Optional[Callable[[Order], Any]] = None,
    ):
        self.clob_client = clob_client
        self.risk_manager = risk_manager
        self.alerter = alerter
        self.dry_run = dry_run
        self.on_fill = on_fill
        self.on_cancel = on_cancel

        self._open_orders: dict[str, Order] = {}
        self._order_history: list[Order] = []
        self._running: bool = False

        # Load persisted history
        self._load_history()

        logger.info(
            "OrderManager initialised | dry_run=%s | timeout=%dm | "
            "history_loaded=%d",
            self.dry_run,
            ORDER_TIMEOUT_MINS,
            len(self._order_history),
        )

    # ------------------------------------------------------------------
    # Submit Signal → Place Order
    # ------------------------------------------------------------------

    async def submit_signal(self, signal: dict) -> Optional[str]:
        """
        Accept a trade signal, validate with risk manager, and place the order.

        Args:
            signal: Dict with keys:
                market_id, token_id, side, estimated_prob,
                market_price, proposed_size_usdc, agent (optional)

        Returns:
            order_id string if placed, None if rejected or skipped.
        """
        market_id = signal.get("market_id", "")
        token_id = signal.get("token_id", market_id)
        side = signal.get("side", "BUY")
        market_price = float(signal.get("market_price", 0.5))
        size_usd = float(signal.get("proposed_size_usdc", 0.0))
        agent = signal.get("agent", "unknown")

        if size_usd <= 0:
            logger.warning("OrderManager: signal rejected — zero size | market=%s", market_id)
            return None

        # Risk manager check (if available)
        if self.risk_manager is not None:
            decision = self.risk_manager.calculate_position_size(
                market_id=market_id,
                win_probability=float(signal.get("estimated_prob", market_price)),
                market_price=market_price,
                edge=float(signal.get("edge", 0.0)),
            )
            if not decision.approved:
                logger.info(
                    "OrderManager: signal REJECTED by risk manager: %s | market=%s",
                    decision.reason,
                    market_id,
                )
                return None
            size_usd = decision.recommended_size_usd

        # Place order
        order_id = await self.place_maker_order(
            market_id=market_id,
            token_id=token_id,
            side=side,
            price=market_price,
            size=size_usd,
            agent=agent,
        )
        return order_id

    async def place_maker_order(
        self,
        market_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        agent: str = "unknown",
    ) -> Optional[str]:
        """
        Place a limit (maker) order.

        In DRY_RUN: generates a fake order_id, records order as pending.
        In live mode: calls CLOBClient.place_order().

        Returns:
            order_id string or None on failure.
        """
        if self.dry_run:
            order_id = f"DRY_{uuid.uuid4().hex[:12].upper()}"
            logger.info(
                "DRY_RUN order placed | id=%s | market=%s | %s %.4f @ %.4f | agent=%s",
                order_id, market_id, side, size, price, agent,
            )
        else:
            if self.clob_client is None:
                logger.error("OrderManager: no CLOB client — cannot place order")
                return None
            try:
                resp = await self.clob_client.place_order(
                    token_id=token_id,
                    side=side,
                    size=size,
                    price=price,
                )
                if resp is None:
                    return None
                order_id = resp.get("order_id", f"LIVE_{uuid.uuid4().hex[:12].upper()}")
            except Exception as exc:
                logger.error("OrderManager: place_order failed: %s", exc)
                return None

        order = Order(
            order_id=order_id,
            market_id=market_id,
            token_id=token_id,
            side=side,
            price=price,
            size_usd=round(size, 4),
            status="pending",
            dry_run=self.dry_run,
            agent=agent,
        )
        self._open_orders[order_id] = order
        self._save_history_append(order)

        return order_id

    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an open order.

        Returns True if successfully cancelled.
        """
        order = self._open_orders.get(order_id)
        if order is None:
            logger.warning("OrderManager: cancel_order — order %s not found", order_id)
            return False

        if self.dry_run:
            logger.info("DRY_RUN cancel | order_id=%s", order_id)
            success = True
        else:
            if self.clob_client is None:
                logger.error("OrderManager: no CLOB client — cannot cancel")
                return False
            try:
                success = await self.clob_client.cancel_order(order_id)
            except Exception as exc:
                logger.error("OrderManager: cancel_order failed: %s", exc)
                return False

        if success:
            self._transition(order, "cancelled")

        return success

    async def get_order_status(self, order_id: str) -> str:
        """
        Get the current status of an order.

        Returns one of: pending / filled / cancelled / expired / unknown
        """
        # Check local cache first
        order = self._open_orders.get(order_id)
        if order:
            return order.status

        # Check history
        for hist_order in reversed(self._order_history):
            if hist_order.order_id == order_id:
                return hist_order.status

        return "unknown"

    # ------------------------------------------------------------------
    # Fill Monitoring
    # ------------------------------------------------------------------

    async def poll_fills(self) -> list[Order]:
        """
        Check status of all open orders and process any new fills.

        In DRY_RUN: simulates 30% fill rate per poll cycle.
        In live mode: queries CLOBClient for order status.

        Returns:
            List of newly-filled orders.
        """
        filled: list[Order] = []

        for order_id, order in list(self._open_orders.items()):
            if self.dry_run:
                # Simulate 30% fill rate — fills become more likely as order ages
                age_factor = min(order.age_seconds / (ORDER_TIMEOUT_MINS * 60), 1.0)
                fill_prob = 0.30 * (1.0 + age_factor)  # 30–60% depending on age
                if random.random() < fill_prob:
                    logger.info(
                        "DRY_RUN simulated fill | order_id=%s | market=%s | "
                        "size=%.2f @ %.4f",
                        order_id, order.market_id, order.size_usd, order.price,
                    )
                    self._transition(order, "filled")
                    filled.append(order)
            else:
                if self.clob_client is None:
                    continue
                try:
                    # Live: query order status from CLOB
                    resp = await self.clob_client.get_order_status(order_id)
                    if resp and resp.get("status") == "FILLED":
                        self._transition(order, "filled")
                        filled.append(order)
                except Exception as exc:
                    logger.error("OrderManager: poll_fills error for %s: %s", order_id, exc)

        # Fire fill callbacks
        for order in filled:
            if self.on_fill:
                try:
                    result = self.on_fill(order)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    logger.error("OrderManager: on_fill callback error: %s", exc)

        return filled

    # ------------------------------------------------------------------
    # Stale Order Cancellation
    # ------------------------------------------------------------------

    async def cancel_stale_orders(
        self, max_age_seconds: Optional[int] = None
    ) -> int:
        """
        Cancel open orders older than max_age_seconds.

        Defaults to ORDER_TIMEOUT_MINS × 60.

        Returns:
            Number of orders cancelled.
        """
        if max_age_seconds is None:
            max_age_seconds = ORDER_TIMEOUT_MINS * 60

        cancelled_count = 0
        for order_id, order in list(self._open_orders.items()):
            if order.age_seconds > max_age_seconds:
                logger.info(
                    "OrderManager: cancelling stale order | id=%s | age=%.0fs | "
                    "market=%s",
                    order_id, order.age_seconds, order.market_id,
                )
                success = await self.cancel_order(order_id)
                if success:
                    cancelled_count += 1

        return cancelled_count

    # ------------------------------------------------------------------
    # Background Monitoring Loop
    # ------------------------------------------------------------------

    async def run(
        self,
        poll_interval: int = config.ORDER_MONITOR_INTERVAL_SEC,
    ) -> None:
        """
        Background loop: poll fills and cancel stale orders periodically.

        Runs until cancelled.
        """
        self._running = True
        logger.info(
            "OrderManager monitoring loop starting | poll_interval=%ds | "
            "timeout=%dm",
            poll_interval,
            ORDER_TIMEOUT_MINS,
        )

        try:
            while self._running:
                try:
                    fills = await self.poll_fills()
                    cancelled = await self.cancel_stale_orders()

                    if fills or cancelled:
                        logger.info(
                            "OrderManager: fills=%d | cancelled=%d | open=%d",
                            len(fills), cancelled, len(self._open_orders),
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("OrderManager: monitor loop error: %s", exc)

                await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            logger.info("OrderManager monitoring loop cancelled")
        finally:
            self._running = False
            logger.info(
                "OrderManager stopped | open_orders=%d | history=%d",
                len(self._open_orders),
                len(self._order_history),
            )

    # ------------------------------------------------------------------
    # State Transitions
    # ------------------------------------------------------------------

    def _transition(self, order: Order, new_status: str) -> None:
        """Apply a status transition to an order and move it to history."""
        old_status = order.status
        order.status = new_status

        if new_status == "filled":
            order.filled_at = time.time()
        elif new_status in ("cancelled", "expired"):
            order.cancelled_at = time.time()

        logger.info(
            "Order transition: %s → %s | id=%s | market=%s",
            old_status, new_status, order.order_id, order.market_id,
        )

        # Remove from open orders
        self._open_orders.pop(order.order_id, None)

        # Update in history
        self._order_history.append(order)
        self._save_history_append(order)

        # Cancel callbacks
        if new_status in ("cancelled", "expired") and self.on_cancel:
            try:
                self.on_cancel(order)
            except Exception as exc:
                logger.error("OrderManager: on_cancel callback error: %s", exc)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_history(self) -> None:
        """Load order history from JSON file."""
        if not ORDER_HISTORY_FILE.exists():
            return
        try:
            with open(ORDER_HISTORY_FILE) as f:
                raw = json.load(f)
            for entry in raw:
                # Only load terminal orders (not open)
                if entry.get("status") in ("filled", "cancelled", "expired"):
                    self._order_history.append(Order(**{
                        k: v for k, v in entry.items()
                        if k in Order.__dataclass_fields__
                    }))
        except Exception as exc:
            logger.warning("OrderManager: could not load order history: %s", exc)

    def _save_history_append(self, order: Order) -> None:
        """Append one order to the history JSON file."""
        ORDER_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Read existing
            existing: list[dict] = []
            if ORDER_HISTORY_FILE.exists():
                with open(ORDER_HISTORY_FILE) as f:
                    existing = json.load(f)
        except Exception:
            existing = []

        # Update or append
        order_dict = order.to_dict()
        for i, entry in enumerate(existing):
            if entry.get("order_id") == order.order_id:
                existing[i] = order_dict
                break
        else:
            existing.append(order_dict)

        try:
            with open(ORDER_HISTORY_FILE, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception as exc:
            logger.warning("OrderManager: could not save order history: %s", exc)

    # ------------------------------------------------------------------
    # Status & Reporting
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Return current state summary."""
        return {
            "open_orders": len(self._open_orders),
            "history_count": len(self._order_history),
            "running": self._running,
            "dry_run": self.dry_run,
        }

    def get_open_orders(self) -> list[Order]:
        """Return all currently open orders."""
        return list(self._open_orders.values())

    def get_fills_today(self) -> list[Order]:
        """Return orders filled today."""
        from datetime import date
        today = date.today()
        return [
            o for o in self._order_history
            if o.status == "filled" and o.filled_at is not None
            and datetime.fromtimestamp(o.filled_at, tz=timezone.utc).date() == today
        ]
