"""Cross-validate the matching engine against an independent reference model.

Why a hand-written reference instead of a third-party backtester
---------------------------------------------------------------

The original plan was to cross-check fills against ``nautilus_trader``. It was
evaluated and rejected (see the README "Validation" note):

* its matching engine is no longer an importable Python module (it moved to a
  Rust/pyo3 extension), so there is nothing to call at the fill-logic level
  without standing up a full backtest node;
* it pins ``pandas<3.0.0``, which would downgrade the pandas this project
  resolves and defeat the point of the modernization.

So the reference here is a deliberately naive, obviously-correct model of
price-time priority written independently of ``book.py``: a flat list of resting
orders scanned in (price, arrival) order. It is far too slow to be the engine,
which is exactly why it makes a good oracle -- it shares no code, no data
structures and no optimizations with the implementation under test.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from microbook.book import LimitOrderBook, Order, Side


@dataclass
class _RestingOrder:
    order_id: int
    side: Side
    price: int
    remaining: int
    arrival: int


class ReferenceBook:
    """Naive price-time-priority book. Independent of ``LimitOrderBook``.

    Deliberately O(n) per match: it re-scans every resting order to find the
    best-priced, earliest-arriving one. Integer-only, like the implementation.
    """

    def __init__(self) -> None:
        self.resting: list[_RestingOrder] = []
        self._arrival = 0

    def _best(self, taker_side: Side, limit: int | None) -> _RestingOrder | None:
        # A buy taker consumes the lowest ask; a sell taker the highest bid.
        maker_side = Side.SELL if taker_side is Side.BUY else Side.BUY
        candidates = [order for order in self.resting if order.side is maker_side]
        if limit is not None:
            candidates = [
                order
                for order in candidates
                if (order.price <= limit if taker_side is Side.BUY else order.price >= limit)
            ]
        if not candidates:
            return None
        if taker_side is Side.BUY:
            # Lowest ask wins; ties broken by earliest arrival (FIFO).
            return min(candidates, key=lambda order: (order.price, order.arrival))
        # Highest bid wins; ties again broken by earliest arrival. Negating
        # arrival makes `max` prefer the *smallest* arrival at equal price.
        return max(candidates, key=lambda order: (order.price, -order.arrival))

    def _crosses(self, side: Side, price: int | None) -> bool:
        return self._best(side, price) is not None

    def submit(self, order_id: int, side: Side, quantity: int, price: int | None) -> list[tuple]:
        """Return ``[(maker_order_id, trade_price, traded_quantity), ...]``."""
        fills: list[tuple] = []
        remaining = quantity
        while remaining:
            maker = self._best(side, price)
            if maker is None:
                break
            traded = min(remaining, maker.remaining)
            remaining -= traded
            maker.remaining -= traded
            fills.append((maker.order_id, maker.price, traded))
            if maker.remaining == 0:
                self.resting.remove(maker)
        if remaining and price is not None:
            self.resting.append(
                _RestingOrder(order_id, side, price, remaining, self._arrival)
            )
            self._arrival += 1
        return fills

    def cancel(self, order_id: int) -> bool:
        for order in self.resting:
            if order.order_id == order_id:
                self.resting.remove(order)
                return True
        return False

    @property
    def best_bid(self) -> int | None:
        prices = [order.price for order in self.resting if order.side is Side.BUY]
        return max(prices, default=None)

    @property
    def best_ask(self) -> int | None:
        prices = [order.price for order in self.resting if order.side is Side.SELL]
        return min(prices, default=None)


def test_reference_agrees_on_a_hand_built_priority_case() -> None:
    # Two makers at the same price: the earlier arrival must fill first.
    book = LimitOrderBook()
    reference = ReferenceBook()
    for order_id, (side, quantity, price) in enumerate(
        [(Side.SELL, 2, 101), (Side.SELL, 2, 101), (Side.SELL, 5, 100)], start=1
    ):
        book.submit(Order(order_id, "maker", side, quantity, order_id, price))
        reference.submit(order_id, side, quantity, price)

    actual = book.submit(Order(99, "taker", Side.BUY, 8, 99))
    expected = reference.submit(99, Side.BUY, 8, None)
    assert [
        (fill.maker_order_id, fill.price, fill.quantity) for fill in actual.fills
    ] == expected
    # Best price first (100 before 101), then FIFO within the 101 level.
    assert expected == [(3, 100, 5), (1, 101, 2), (2, 101, 1)]


def test_randomized_order_flow_matches_the_reference_engine() -> None:
    """Fuzz both engines with the same integer order flow and compare fills."""
    rng = random.Random(20260728)
    book = LimitOrderBook()
    reference = ReferenceBook()
    live: list[int] = []
    next_id = 1

    for _ in range(4_000):
        action = rng.random()
        if action < 0.12 and live:
            # Cancel: both engines must agree on whether the order was there.
            order_id = live.pop(rng.randrange(len(live)))
            assert book.cancel(order_id) == reference.cancel(order_id)
        else:
            side = Side.BUY if rng.random() < 0.5 else Side.SELL
            quantity = 1 + rng.randrange(4)
            if action < 0.28:
                price = None  # market order
            else:
                price = 100 + rng.randrange(-4, 5)
            order_id = next_id
            next_id += 1

            actual = book.submit(Order(order_id, "flow", side, quantity, order_id, price))
            expected = reference.submit(order_id, side, quantity, price)
            assert [
                (fill.maker_order_id, fill.price, fill.quantity) for fill in actual.fills
            ] == expected, f"fill divergence on order {order_id}"
            if actual.resting_order_id is not None:
                live.append(order_id)
            else:
                live[:] = [candidate for candidate in live if candidate in book.orders]

        # Top of book must agree after every single event, not just at the end.
        assert book.best_bid == reference.best_bid
        assert book.best_ask == reference.best_ask
        book.assert_invariants()


def test_reference_and_engine_agree_on_post_only_crossing() -> None:
    book = LimitOrderBook()
    reference = ReferenceBook()
    book.submit(Order(1, "maker", Side.SELL, 3, 1, 101))
    reference.submit(1, Side.SELL, 3, 101)

    # A buy at or above the best ask crosses and must be rejected post-only.
    assert reference._crosses(Side.BUY, 101)
    assert not book.submit(Order(2, "agent", Side.BUY, 1, 2, 101), post_only=True).accepted
    # A buy below the ask rests.
    assert not reference._crosses(Side.BUY, 100)
    assert book.submit(Order(3, "agent", Side.BUY, 1, 3, 100), post_only=True).accepted
