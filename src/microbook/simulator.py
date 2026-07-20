"""Latency- and queue-aware synthetic exchange simulation."""

from __future__ import annotations

import math
import random
import statistics
from collections import deque
from dataclasses import dataclass

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
        if fill.maker_order_id not in self.book.orders:
            self.agent_orders.discard(fill.maker_order_id)
        self.pending_markouts.append(
            PendingMarkout(event + self.config.markout_horizon, fill.maker_side, fill.price)
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
                self.agent_orders.discard(order_id)

            for side, price in ((Side.BUY, quote.bid), (Side.SELL, quote.ask)):
                if price is None:
                    continue
                order = Order(self._new_id(), "agent", side, quote.size, event, price)
                submission = self.book.submit(order, post_only=True)
                self.quotes_sent += 1
                self._process_fills(submission.fills, event)
                if submission.resting_order_id is not None:
                    self.agent_orders.add(order.order_id)
                    self.quotes_accepted += 1
                elif not submission.accepted:
                    self.post_only_rejections += 1

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
            self.markouts.append(markout.side.sign * (midpoint - markout.fill_price))
        self.pnl_curve.append(self.cash + self.inventory * midpoint)
        self.inventory_curve.append(self.inventory)
        self.max_abs_inventory = max(self.max_abs_inventory, abs(self.inventory))

    def run(self) -> dict[str, float | int | str]:
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
        peak = -math.inf
        max_drawdown = 0.0
        for pnl in self.pnl_curve:
            peak = max(peak, pnl)
            max_drawdown = max(max_drawdown, peak - pnl)
        sampled = self.pnl_curve[::100]
        increments = [
            current - previous
            for previous, current in zip(sampled[:-1], sampled[1:], strict=True)
        ]
        increment_std = statistics.pstdev(increments) if len(increments) > 1 else 0.0
        sharpe = (
            statistics.mean(increments) / increment_std * math.sqrt(len(increments))
            if increment_std > 0
            else 0.0
        )
        inventory_rms = math.sqrt(
            sum(value * value for value in self.inventory_curve) / len(self.inventory_curve)
        )
        return {
            "strategy": self.strategy.name,
            "events": self.config.events,
            "final_pnl_ticks": final_pnl,
            "pnl_path_ratio_100_event": sharpe,
            "max_drawdown_ticks": max_drawdown,
            "ending_inventory": self.inventory,
            "max_abs_inventory": self.max_abs_inventory,
            "inventory_rms": inventory_rms,
            "fills": self.fills,
            "filled_quantity": self.filled_quantity,
            "mean_20_event_markout_ticks": statistics.mean(self.markouts)
            if self.markouts
            else 0.0,
            "quotes_sent": self.quotes_sent,
            "quote_acceptance_rate": self.quotes_accepted / max(self.quotes_sent, 1),
            "post_only_rejections": self.post_only_rejections,
            "cancels": self.cancels,
        }
