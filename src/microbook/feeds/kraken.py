"""Kraken v2 public book normalization with exchange checksum validation."""

from __future__ import annotations

import zlib
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, TypeAlias

KRAKEN_BTC_TICK = Decimal("0.1")


def _timestamp_ns(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1_000_000_000)


def _price_to_ticks(price: Decimal, tick_size: Decimal) -> int:
    ticks = price / tick_size
    integral = ticks.to_integral_value()
    if ticks != integral:
        raise ValueError(f"price {price} is not aligned to tick size {tick_size}")
    return int(integral)


@dataclass(frozen=True)
class KrakenLevel:
    price_ticks: int
    quantity: str


@dataclass(frozen=True)
class KrakenSnapshot:
    kind: Literal["snapshot"]
    symbol: str
    received_ns: int
    exchange_ns: int
    bids: tuple[KrakenLevel, ...]
    asks: tuple[KrakenLevel, ...]
    checksum: int


@dataclass(frozen=True)
class KrakenUpdate:
    kind: Literal["update"]
    symbol: str
    received_ns: int
    exchange_ns: int
    bids: tuple[KrakenLevel, ...]
    asks: tuple[KrakenLevel, ...]
    checksum: int


@dataclass(frozen=True)
class KrakenTrade:
    kind: Literal["trade"]
    symbol: str
    received_ns: int
    exchange_ns: int
    trade_id: int
    price_ticks: int
    quantity: str
    aggressor_side: Literal["buy", "sell"]


KrakenEvent: TypeAlias = KrakenSnapshot | KrakenUpdate | KrakenTrade


def parse_kraken_message(
    message: dict[str, Any],
    received_ns: int,
    *,
    tick_size: Decimal = KRAKEN_BTC_TICK,
) -> tuple[KrakenEvent, ...]:
    channel = message.get("channel")
    message_type = message.get("type")
    if channel == "book" and message_type in {"snapshot", "update"}:
        payload = message["data"][0]

        def levels(side: str) -> tuple[KrakenLevel, ...]:
            return tuple(
                KrakenLevel(
                    price_ticks=_price_to_ticks(Decimal(str(level["price"])), tick_size),
                    quantity=str(level["qty"]),
                )
                for level in payload[side]
            )

        event_type = KrakenSnapshot if message_type == "snapshot" else KrakenUpdate
        return (
            event_type(
                kind=message_type,
                symbol=payload["symbol"],
                received_ns=received_ns,
                exchange_ns=_timestamp_ns(payload["timestamp"]),
                bids=levels("bids"),
                asks=levels("asks"),
                checksum=int(payload["checksum"]),
            ),
        )
    if channel == "trade" and message_type in {"snapshot", "update"}:
        return tuple(
            KrakenTrade(
                kind="trade",
                symbol=trade["symbol"],
                received_ns=received_ns,
                exchange_ns=_timestamp_ns(trade["timestamp"]),
                trade_id=int(trade["trade_id"]),
                price_ticks=_price_to_ticks(Decimal(str(trade["price"])), tick_size),
                quantity=str(trade["qty"]),
                aggressor_side=trade["side"],
            )
            for trade in message["data"]
        )
    return ()


def event_to_dict(event: KrakenEvent) -> dict[str, Any]:
    return asdict(event)


def event_from_dict(payload: dict[str, Any]) -> KrakenEvent:
    kind = payload["kind"]
    if kind in {"snapshot", "update"}:
        event_type = KrakenSnapshot if kind == "snapshot" else KrakenUpdate
        return event_type(
            kind=kind,
            symbol=payload["symbol"],
            received_ns=int(payload["received_ns"]),
            exchange_ns=int(payload["exchange_ns"]),
            bids=tuple(KrakenLevel(**level) for level in payload["bids"]),
            asks=tuple(KrakenLevel(**level) for level in payload["asks"]),
            checksum=int(payload["checksum"]),
        )
    if kind == "trade":
        return KrakenTrade(
            kind="trade",
            symbol=payload["symbol"],
            received_ns=int(payload["received_ns"]),
            exchange_ns=int(payload["exchange_ns"]),
            trade_id=int(payload["trade_id"]),
            price_ticks=int(payload["price_ticks"]),
            quantity=payload["quantity"],
            aggressor_side=payload["aggressor_side"],
        )
    raise ValueError(f"unsupported Kraken event kind {kind!r}")


def _checksum_text(value: Decimal) -> str:
    return format(value, "f").replace(".", "").lstrip("0") or "0"


class KrakenBook:
    """Maintain a fixed-depth Kraken book and validate every exchange CRC32."""

    def __init__(
        self,
        symbol: str = "BTC/USD",
        *,
        depth: int = 10,
        tick_size: Decimal = KRAKEN_BTC_TICK,
    ) -> None:
        if depth <= 0:
            raise ValueError("depth must be positive")
        self.symbol = symbol
        self.depth_limit = depth
        self.tick_size = tick_size
        self.bids: dict[int, Decimal] = {}
        self.asks: dict[int, Decimal] = {}
        self.initialized = False
        self.events_applied = 0
        self.checksums_verified = 0

    @property
    def best_bid(self) -> int | None:
        return max(self.bids, default=None)

    @property
    def best_ask(self) -> int | None:
        return min(self.asks, default=None)

    def _apply_levels(self, levels: tuple[KrakenLevel, ...], *, bid: bool) -> None:
        book = self.bids if bid else self.asks
        for level in levels:
            quantity = Decimal(level.quantity)
            if quantity == 0:
                book.pop(level.price_ticks, None)
            elif quantity > 0:
                book[level.price_ticks] = quantity
            else:
                raise ValueError("level quantity cannot be negative")

    def _prune(self) -> None:
        bid_prices = sorted(self.bids, reverse=True)
        ask_prices = sorted(self.asks)
        self.bids = {price: self.bids[price] for price in bid_prices[: self.depth_limit]}
        self.asks = {price: self.asks[price] for price in ask_prices[: self.depth_limit]}

    def apply(self, event: KrakenSnapshot | KrakenUpdate) -> None:
        if event.symbol != self.symbol:
            raise ValueError(f"received {event.symbol}, expected {self.symbol}")
        if isinstance(event, KrakenSnapshot):
            self.bids.clear()
            self.asks.clear()
            self.initialized = True
        elif not self.initialized:
            raise RuntimeError("cannot apply update before snapshot")
        self._apply_levels(event.bids, bid=True)
        self._apply_levels(event.asks, bid=False)
        self._prune()
        self.assert_invariants()
        actual_checksum = self.checksum()
        if actual_checksum != event.checksum:
            raise RuntimeError(
                f"Kraken checksum mismatch: expected {event.checksum}, computed {actual_checksum}"
            )
        self.events_applied += 1
        self.checksums_verified += 1

    def checksum(self) -> int:
        levels: list[tuple[int, Decimal]] = []
        levels.extend((price, self.asks[price]) for price in sorted(self.asks)[:10])
        levels.extend((price, self.bids[price]) for price in sorted(self.bids, reverse=True)[:10])
        payload = "".join(
            _checksum_text(Decimal(price) * self.tick_size) + _checksum_text(quantity)
            for price, quantity in levels
        )
        return zlib.crc32(payload.encode())

    def assert_invariants(self) -> None:
        if len(self.bids) > self.depth_limit or len(self.asks) > self.depth_limit:
            raise AssertionError("book exceeds subscribed depth")
        if any(quantity <= 0 for quantity in (*self.bids.values(), *self.asks.values())):
            raise AssertionError("book contains non-positive depth")
        if self.best_bid is not None and self.best_ask is not None:
            if self.best_bid >= self.best_ask:
                raise AssertionError("reconstructed book is crossed")

    def depth(self, side: Literal["buy", "sell"]) -> Decimal:
        book = self.bids if side == "buy" else self.asks
        return sum(book.values(), start=Decimal(0))

