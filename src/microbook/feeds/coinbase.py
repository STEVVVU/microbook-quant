"""Normalize and reconstruct Coinbase Exchange's public level-2 feed."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, TypeAlias

DEFAULT_TICK_SIZE = Decimal("0.01")


def _timestamp_ns(value: str | None) -> int | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1_000_000_000)


def price_to_ticks(price: str, tick_size: Decimal = DEFAULT_TICK_SIZE) -> int:
    ticks = Decimal(price) / tick_size
    integral = ticks.to_integral_value()
    if ticks != integral:
        raise ValueError(f"price {price} is not aligned to tick size {tick_size}")
    return int(integral)


@dataclass(frozen=True)
class L2Snapshot:
    kind: Literal["snapshot"]
    product_id: str
    received_ns: int
    bids: tuple[tuple[int, float], ...]
    asks: tuple[tuple[int, float], ...]


@dataclass(frozen=True)
class L2Change:
    side: Literal["buy", "sell"]
    price_ticks: int
    size: float


@dataclass(frozen=True)
class L2Update:
    kind: Literal["l2update"]
    product_id: str
    received_ns: int
    exchange_ns: int | None
    changes: tuple[L2Change, ...]


@dataclass(frozen=True)
class Trade:
    kind: Literal["trade"]
    product_id: str
    received_ns: int
    exchange_ns: int | None
    trade_id: int
    price_ticks: int
    size: float
    aggressor_side: Literal["buy", "sell"]


CoinbaseEvent: TypeAlias = L2Snapshot | L2Update | Trade


def parse_message(message: dict[str, Any], received_ns: int) -> CoinbaseEvent | None:
    """Convert supported Coinbase messages to typed, integer-tick events."""

    message_type = message.get("type")
    product_id = str(message.get("product_id", ""))
    if message_type == "snapshot":
        if not product_id:
            raise ValueError("snapshot is missing product_id")
        return L2Snapshot(
            kind="snapshot",
            product_id=product_id,
            received_ns=received_ns,
            bids=tuple((price_to_ticks(price), float(size)) for price, size in message["bids"]),
            asks=tuple((price_to_ticks(price), float(size)) for price, size in message["asks"]),
        )
    if message_type == "l2update":
        if not product_id:
            raise ValueError("l2update is missing product_id")
        return L2Update(
            kind="l2update",
            product_id=product_id,
            received_ns=received_ns,
            exchange_ns=_timestamp_ns(message.get("time")),
            changes=tuple(
                L2Change(
                    side=side,
                    price_ticks=price_to_ticks(price),
                    size=float(size),
                )
                for side, price, size in message["changes"]
            ),
        )
    if message_type in {"match", "last_match"}:
        if not product_id:
            raise ValueError("trade is missing product_id")
        maker_side = message["side"]
        if maker_side not in {"buy", "sell"}:
            raise ValueError(f"invalid maker side {maker_side!r}")
        return Trade(
            kind="trade",
            product_id=product_id,
            received_ns=received_ns,
            exchange_ns=_timestamp_ns(message.get("time")),
            trade_id=int(message["trade_id"]),
            price_ticks=price_to_ticks(message["price"]),
            size=float(message["size"]),
            aggressor_side="sell" if maker_side == "buy" else "buy",
        )
    return None


def event_to_dict(event: CoinbaseEvent) -> dict[str, Any]:
    return asdict(event)


class CoinbaseBook:
    """Reconstruct full L2 state from one snapshot followed by updates."""

    def __init__(self, product_id: str = "BTC-USD") -> None:
        self.product_id = product_id
        self.bids: dict[int, float] = {}
        self.asks: dict[int, float] = {}
        self.initialized = False
        self.events_applied = 0

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

    def apply(self, event: L2Snapshot | L2Update) -> None:
        if event.product_id != self.product_id:
            raise ValueError(
                f"received {event.product_id}, expected product {self.product_id}"
            )
        if isinstance(event, L2Snapshot):
            self.bids = dict(event.bids)
            self.asks = dict(event.asks)
            self.initialized = True
        else:
            if not self.initialized:
                raise RuntimeError("cannot apply l2update before snapshot")
            for change in event.changes:
                levels = self.bids if change.side == "buy" else self.asks
                if change.size == 0.0:
                    levels.pop(change.price_ticks, None)
                elif change.size > 0.0:
                    levels[change.price_ticks] = change.size
                else:
                    raise ValueError("level size cannot be negative")
        self.events_applied += 1
        self.assert_invariants()

    def assert_invariants(self) -> None:
        if any(size <= 0 for size in (*self.bids.values(), *self.asks.values())):
            raise AssertionError("book contains non-positive depth")
        if self.best_bid is not None and self.best_ask is not None:
            if self.best_bid >= self.best_ask:
                raise AssertionError("reconstructed book is crossed")

    def depth(self, side: Literal["buy", "sell"], levels: int = 5) -> float:
        if levels <= 0:
            raise ValueError("levels must be positive")
        prices = sorted(
            self.bids if side == "buy" else self.asks,
            reverse=side == "buy",
        )
        book = self.bids if side == "buy" else self.asks
        return sum(book[price] for price in prices[:levels])

