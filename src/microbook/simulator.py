"""Latency- and queue-aware synthetic exchange simulation.

The event loop below is intentionally scalar and integer-typed: prices are
integer ticks, sizes are integer lots, and events are sequenced one at a time
because price-time priority and queue position are order-dependent. Aggregation
of what this loop *records* is vectorized in :mod:`microbook.analytics`; see the
module docstring there for the full integer-in-the-book / numpy-in-the-analysis
rationale.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import deque
from dataclasses import dataclass, field

from . import analytics
from .book import Fill, LimitOrderBook, Order, Side
from .strategy import FixedSpread, InventoryAware


@dataclass(frozen=True)
class SimulationConfig:
    events: int = 30_000
    seed: int = 31
    quote_interval: int = 20
    latency: int = 3
    maker_rebate: float = 0.002
    initial_price: int = 10_000
    markout_horizon: int = 20


@dataclass(frozen=True)
class PendingQuote:
    due_at: int
    bid: int | None
    ask: int | None
    size: int
    observed_at: int


@dataclass(frozen=True)
class PendingMarkout:
    due_at: int
    side: Side
    fill_price: int
    order_id: int
    fill_event: int


@dataclass
class QuoteRecord:
    """Per-quote outcome, in integer ticks and lots, for the analytics layer.

    ``queue_ahead_lots`` is the resting quantity in front of this order in its
    FIFO queue at the moment it was accepted -- the quantity that has to trade
    before this order can. It is captured here (not reconstructed later) because
    it is only knowable at submit time.
    """

    order_id: int
    side: str
    price_ticks: int
    submitted_event: int
    queue_ahead_lots: int
    size_lots: int
    filled_lots: int = 0
    cancelled: bool = False


@dataclass(frozen=True)
class MarkoutRecord:
    """A resolved markout: signed midpoint drift in ticks after a maker fill."""

    order_id: int
    side: str
    fill_price_ticks: int
    fill_event: int
    markout_ticks: float


@dataclass
class SimulationResult:
    """Scalar metrics plus the raw curves and records the analytics layer reduces."""

    metrics: dict[str, float | int | str]
    pnl_curve: list[float] = field(default_factory=list)
    inventory_curve: list[int] = field(default_factory=list)
    quotes: list[QuoteRecord] = field(default_factory=list)
    markouts: list[MarkoutRecord] = field(default_factory=list)


class MarketSimulator:
    def __init__(self, strategy: FixedSpread | InventoryAware, config: SimulationConfig):
        if config.events <= 0 or config.quote_interval <= 0 or config.latency < 0:
            raise ValueError("invalid simulation configuration")
        self.strategy = strategy
        self.config = config
        self.rng = random.Random(config.seed)
        self.book = LimitOrderBook()
        self.next_order_id = 1
        self.fundamental = float(config.initial_price)
        self.inventory = 0
        self.cash = 0.0
        self.agent_orders: set[int] = set()
        self.external_orders: deque[int] = deque()
        self.pending_quotes: deque[PendingQuote] = deque()
        self.pending_markouts: deque[PendingMarkout] = deque()
        self.mid_history: deque[float] = deque(maxlen=80)
        self.pnl_curve: list[float] = []
        self.inventory_curve: list[int] = []
        self.markouts: list[float] = []
        # Per-quote and per-markout records feed the vectorized analytics layer.
        # They are appended, never aggregated here, so the event loop stays exact.
        self.quote_records: list[QuoteRecord] = []
        self.markout_records: list[MarkoutRecord] = []
        self._quotes_by_order: dict[int, QuoteRecord] = {}
        self.fills = 0
        self.filled_quantity = 0
        self.quotes_sent = 0
        self.quotes_accepted = 0
        self.post_only_rejections = 0
        self.cancels = 0
        self.max_abs_inventory = 0
        self._seed_book()

    def _new_id(self) -> int:
        order_id = self.next_order_id
        self.next_order_id += 1
        return order_id

    def _seed_book(self) -> None:
        for offset in range(1, 7):
            for side in (Side.BUY, Side.SELL):
                price = self.config.initial_price + side.sign * -offset
                self._add_external_limit(side, price, 18 + self.rng.randrange(10), timestamp=0)

    def _add_external_limit(self, side: Side, price: int, quantity: int, timestamp: int) -> None:
        order = Order(self._new_id(), "external", side, quantity, timestamp, price)
        submission = self.book.submit(order, post_only=True)
        if submission.resting_order_id is not None:
            self.external_orders.append(order.order_id)

    def _apply_agent_fill(self, fill: Fill, event: int) -> None:
        if fill.maker_owner != "agent":
            return
        signed_quantity = fill.maker_side.sign * fill.quantity
        self.inventory += signed_quantity
        self.cash -= signed_quantity * fill.price
        self.cash += self.config.maker_rebate * fill.quantity
        self.fills += 1
        self.filled_quantity += fill.quantity
        record = self._quotes_by_order.get(fill.maker_order_id)
        if record is not None:
            record.filled_lots += fill.quantity
        if fill.maker_order_id not in self.book.orders:
            self.agent_orders.discard(fill.maker_order_id)
        self.pending_markouts.append(
            PendingMarkout(
                event + self.config.markout_horizon,
                fill.maker_side,
                fill.price,
                order_id=fill.maker_order_id,
                fill_event=event,
            )
        )

    def _process_fills(self, fills: list[Fill], event: int) -> None:
        for fill in fills:
            self._apply_agent_fill(fill, event)

    def _apply_due_quote(self, event: int) -> None:
        while self.pending_quotes and self.pending_quotes[0].due_at <= event:
            quote = self.pending_quotes.popleft()
            for order_id in tuple(self.agent_orders):
                if self.book.cancel(order_id, owner="agent"):
                    self.cancels += 1
                    record = self._quotes_by_order.get(order_id)
                    if record is not None:
                        record.cancelled = True
                self.agent_orders.discard(order_id)

            for side, price in ((Side.BUY, quote.bid), (Side.SELL, quote.ask)):
                if price is None:
                    continue
                # Queue position is only observable *before* submitting: it is the
                # integer lot count already resting at this price on our side.
                queue_ahead = self.book.depth(side, price)
                order = Order(self._new_id(), "agent", side, quote.size, event, price)
                submission = self.book.submit(order, post_only=True)
                self.quotes_sent += 1
                if submission.resting_order_id is not None:
                    record = QuoteRecord(
                        order_id=order.order_id,
                        side=side.value,
                        price_ticks=price,
                        submitted_event=event,
                        queue_ahead_lots=queue_ahead,
                        size_lots=quote.size,
                    )
                    self.quote_records.append(record)
                    self._quotes_by_order[order.order_id] = record
                    self.agent_orders.add(order.order_id)
                    self.quotes_accepted += 1
                elif not submission.accepted:
                    self.post_only_rejections += 1
                self._process_fills(submission.fills, event)

    def _evolve_fundamental(self) -> None:
        draw = self.rng.random()
        if draw < 0.075:
            self.fundamental += 1.0
        elif draw < 0.15:
            self.fundamental -= 1.0

    def _replenish_liquidity(self, event: int) -> None:
        for _ in range(1 + int(self.rng.random() < 0.25)):
            side = Side.BUY if self.rng.random() < 0.5 else Side.SELL
            offset = 1 + self.rng.randrange(5)
            anchor = round(self.fundamental)
            if side is Side.BUY:
                ceiling = (self.book.best_ask - 1) if self.book.best_ask is not None else anchor
                price = min(anchor - offset, ceiling)
            else:
                floor = (self.book.best_bid + 1) if self.book.best_bid is not None else anchor
                price = max(anchor + offset, floor)
            self._add_external_limit(side, max(price, 1), 1 + self.rng.randrange(5), event)

        cancellation_budget = 1 + int(len(self.external_orders) > 1_200) * 3
        for _ in range(cancellation_budget):
            if self.external_orders and self.rng.random() < 0.55:
                order_id = self.external_orders.popleft()
                self.book.cancel(order_id, owner="external")

    def _send_noise_market_order(self, event: int) -> None:
        if self.rng.random() >= 0.38:
            return
        midpoint = self.book.midpoint or self.fundamental
        # Noise flow is mildly informed, so maker fills have adverse selection
        # without turning the experiment into a fully informed-taker model.
        imbalance = max(min((self.fundamental - midpoint) * 0.14, 1.25), -1.25)
        buy_probability = 1.0 / (1.0 + math.exp(-imbalance))
        side = Side.BUY if self.rng.random() < buy_probability else Side.SELL
        order = Order(
            self._new_id(),
            "noise",
            side,
            1 + self.rng.randrange(4),
            event,
            price=None,
        )
        self._process_fills(self.book.submit(order).fills, event)

    def _volatility(self) -> float:
        if len(self.mid_history) < 3:
            return 0.5
        history = list(self.mid_history)
        changes = [
            current - previous
            for previous, current in zip(history[:-1], history[1:], strict=True)
        ]
        return statistics.pstdev(changes) if len(changes) > 1 else 0.5

    def _schedule_quote(self, event: int) -> None:
        midpoint = self.book.midpoint
        if midpoint is None:
            return
        quote = self.strategy.quote(midpoint, self.inventory, self._volatility())
        self.pending_quotes.append(
            PendingQuote(
                due_at=event + self.config.latency,
                bid=quote.bid,
                ask=quote.ask,
                size=quote.size,
                observed_at=event,
            )
        )

    def _record_state(self, event: int) -> None:
        midpoint = self.book.midpoint or self.fundamental
        self.mid_history.append(midpoint)
        while self.pending_markouts and self.pending_markouts[0].due_at <= event:
            markout = self.pending_markouts.popleft()
            drift = markout.side.sign * (midpoint - markout.fill_price)
            self.markouts.append(drift)
            self.markout_records.append(
                MarkoutRecord(
                    order_id=markout.order_id,
                    side=markout.side.value,
                    fill_price_ticks=markout.fill_price,
                    fill_event=markout.fill_event,
                    markout_ticks=drift,
                )
            )
        self.pnl_curve.append(self.cash + self.inventory * midpoint)
        self.inventory_curve.append(self.inventory)
        self.max_abs_inventory = max(self.max_abs_inventory, abs(self.inventory))

    def run(self) -> dict[str, float | int | str]:
        """Run the event loop and return the scalar metric dict.

        Kept for backwards compatibility; :meth:`run_detailed` additionally
        returns the raw curves and records for the analytics layer.
        """
        return self.run_detailed().metrics

    def run_detailed(self) -> SimulationResult:
        for event in range(self.config.events):
            self._apply_due_quote(event)
            self._evolve_fundamental()
            self._replenish_liquidity(event)
            self._send_noise_market_order(event)
            if event % self.config.quote_interval == 0:
                self._schedule_quote(event)
            self._record_state(event)
            if event % 1_000 == 0:
                self.book.assert_invariants()

        final_mid = self.book.midpoint or self.fundamental
        final_pnl = self.cash + self.inventory * final_mid
        # Path statistics are reduced with numpy in analytics.py. The reductions
        # used there (running max, diff, exact int64 sum of squares, fsum means)
        # are order-independent, so these aggregates match the scalar loops they
        # replaced bit for bit.
        metrics: dict[str, float | int | str] = {
            "strategy": self.strategy.name,
            "events": self.config.events,
            "final_pnl_ticks": final_pnl,
            "pnl_path_ratio_100_event": analytics.path_ratio(self.pnl_curve, stride=100),
            "max_drawdown_ticks": analytics.max_drawdown_ticks(self.pnl_curve),
            "ending_inventory": self.inventory,
            "max_abs_inventory": self.max_abs_inventory,
            "inventory_rms": analytics.inventory_rms(self.inventory_curve),
            "fills": self.fills,
            "filled_quantity": self.filled_quantity,
            "mean_20_event_markout_ticks": analytics.fsum_mean(self.markouts),
            "quotes_sent": self.quotes_sent,
            "quote_acceptance_rate": self.quotes_accepted / max(self.quotes_sent, 1),
            "post_only_rejections": self.post_only_rejections,
            "cancels": self.cancels,
        }
        return SimulationResult(
            metrics=metrics,
            pnl_curve=list(self.pnl_curve),
            inventory_curve=list(self.inventory_curve),
            quotes=list(self.quote_records),
            markouts=list(self.markout_records),
        )
