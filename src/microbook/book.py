"""A deterministic price-time-priority limit order book."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


@dataclass
class Order:
    order_id: int
    owner: str
    side: Side
    quantity: int
    timestamp: int
    price: int | None = None
    remaining: int | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.price is not None and self.price <= 0:
            raise ValueError("price must be positive")
        if self.remaining is None:
            self.remaining = self.quantity


@dataclass(frozen=True)
class Fill:
    timestamp: int
    price: int
    quantity: int
    maker_order_id: int
    maker_owner: str
    maker_side: Side
    taker_order_id: int
    taker_owner: str


@dataclass(frozen=True)
class Submission:
    accepted: bool
    fills: list[Fill]
    resting_order_id: int | None = None
    reason: str | None = None


class LimitOrderBook:
    """Integer-tick matching engine with FIFO queues at each price."""

    def __init__(self) -> None:
        self.bids: dict[int, deque[Order]] = {}
        self.asks: dict[int, deque[Order]] = {}
        self.orders: dict[int, Order] = {}

    @property
    def best_bid(self) -> int | None:
        return max(self.bids, default=None)

    @property
    def best_ask(self) -> int | None:
        return min(self.asks, default=None)

    @property
    def midpoint(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    def depth(self, side: Side, price: int) -> int:
        levels = self.bids if side is Side.BUY else self.asks
        return sum(int(order.remaining) for order in levels.get(price, ()))

    def _crosses(self, side: Side, price: int | None) -> bool:
        if side is Side.BUY:
            return self.best_ask is not None and (price is None or price >= self.best_ask)
        return self.best_bid is not None and (price is None or price <= self.best_bid)

    def submit(self, order: Order, *, post_only: bool = False) -> Submission:
        if order.order_id in self.orders:
            raise ValueError(f"duplicate order id {order.order_id}")
        if post_only and order.price is not None and self._crosses(order.side, order.price):
            return Submission(False, [], reason="post-only order would cross")

        fills: list[Fill] = []
        while order.remaining and self._crosses(order.side, order.price):
            opposing = self.asks if order.side is Side.BUY else self.bids
            best_price = min(opposing) if order.side is Side.BUY else max(opposing)
            queue = opposing[best_price]
            maker = queue[0]
            traded = min(int(order.remaining), int(maker.remaining))
            order.remaining = int(order.remaining) - traded
            maker.remaining = int(maker.remaining) - traded
            fills.append(
                Fill(
                    timestamp=order.timestamp,
                    price=best_price,
                    quantity=traded,
                    maker_order_id=maker.order_id,
                    maker_owner=maker.owner,
                    maker_side=maker.side,
                    taker_order_id=order.order_id,
                    taker_owner=order.owner,
                )
            )
            if maker.remaining == 0:
                queue.popleft()
                self.orders.pop(maker.order_id, None)
            if not queue:
                opposing.pop(best_price)

        if order.remaining and order.price is not None:
            levels = self.bids if order.side is Side.BUY else self.asks
            levels.setdefault(order.price, deque()).append(order)
            self.orders[order.order_id] = order
            return Submission(True, fills, resting_order_id=order.order_id)
        return Submission(True, fills)

    def cancel(self, order_id: int, *, owner: str | None = None) -> bool:
        order = self.orders.get(order_id)
        if order is None or (owner is not None and order.owner != owner):
            return False
        levels = self.bids if order.side is Side.BUY else self.asks
        queue = levels[order.price]
        queue.remove(order)
        if not queue:
            levels.pop(order.price)
        self.orders.pop(order_id)
        return True

    def assert_invariants(self) -> None:
        if self.best_bid is not None and self.best_ask is not None:
            if self.best_bid >= self.best_ask:
                raise AssertionError("crossed book")
        indexed = {
            order.order_id
            for levels in (self.bids, self.asks)
            for queue in levels.values()
            for order in queue
        }
        if indexed != set(self.orders):
            raise AssertionError("order index differs from price levels")
        if any(int(order.remaining) <= 0 for order in self.orders.values()):
            raise AssertionError("non-positive resting quantity")

