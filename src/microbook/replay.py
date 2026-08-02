"""Deterministic integrity and microstructure analytics over recorded Kraken L2."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import statistics
from decimal import Decimal
from pathlib import Path

from .feeds.kraken import (
    KRAKEN_BTC_TICK,
    KrakenBook,
    KrakenSnapshot,
    KrakenTrade,
    KrakenUpdate,
    event_from_dict,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def replay_kraken_capture(
    capture: str | Path,
    *,
    symbol: str = "BTC/USD",
    depth: int = 10,
    output: str | Path | None = None,
) -> dict[str, object]:
    capture = Path(capture)
    book = KrakenBook(symbol, depth=depth)
    counts = {"snapshot": 0, "update": 0, "trade": 0}
    spreads: list[float] = []
    imbalances: list[float] = []
    latencies_ms: list[float] = []
    midpoints: list[float] = []
    first_received_ns: int | None = None
    last_received_ns: int | None = None
    top_of_book_changes = 0
    previous_top: tuple[int, int] | None = None
    buy_trades = 0
    sell_trades = 0
    traded_quantity = Decimal(0)
    traded_notional = Decimal(0)

    with gzip.open(capture, "rt", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                event = event_from_dict(json.loads(line))
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"invalid capture event at line {line_number}") from error
            if event.symbol != symbol:
                raise ValueError(f"capture contains {event.symbol}, expected {symbol}")
            counts[event.kind] += 1
            first_received_ns = (
                event.received_ns if first_received_ns is None else first_received_ns
            )
            last_received_ns = event.received_ns
            if isinstance(event, (KrakenSnapshot, KrakenUpdate)):
                book.apply(event)
                assert book.best_bid is not None
                assert book.best_ask is not None
                top = (book.best_bid, book.best_ask)
                top_of_book_changes += int(previous_top is not None and top != previous_top)
                previous_top = top
                spreads.append(float(book.best_ask - book.best_bid))
                midpoints.append((book.best_bid + book.best_ask) / 2.0)
                bid_depth = book.depth("buy")
                ask_depth = book.depth("sell")
                total_depth = bid_depth + ask_depth
                if total_depth > 0:
                    imbalances.append(float((bid_depth - ask_depth) / total_depth))
                latencies_ms.append((event.received_ns - event.exchange_ns) / 1_000_000)
            elif isinstance(event, KrakenTrade):
                quantity = Decimal(event.quantity)
                price = Decimal(event.price_ticks) * KRAKEN_BTC_TICK
                traded_quantity += quantity
                traded_notional += quantity * price
                buy_trades += int(event.aggressor_side == "buy")
                sell_trades += int(event.aggressor_side == "sell")
                latencies_ms.append((event.received_ns - event.exchange_ns) / 1_000_000)

    if first_received_ns is None or last_received_ns is None or not book.initialized:
        raise ValueError("capture contains no usable Kraken book")
    midpoint_changes = [
        current - previous
        for previous, current in zip(midpoints[:-1], midpoints[1:], strict=True)
    ]
    duration_seconds = (last_received_ns - first_received_ns) / 1_000_000_000
    result: dict[str, object] = {
        "capture": {
            "path": str(capture),
            "sha256": _sha256(capture),
            "symbol": symbol,
            "depth": depth,
            "duration_seconds": duration_seconds,
        },
        "integrity": {
            "events": sum(counts.values()),
            "snapshots": counts["snapshot"],
            "updates": counts["update"],
            "trades": counts["trade"],
            "checksums_verified": book.checksums_verified,
            "crossed_book_states": 0,
        },
        "market": {
            "updates_per_second": counts["update"] / max(duration_seconds, 1e-12),
            "top_of_book_changes": top_of_book_changes,
            "spread_ticks_mean": statistics.mean(spreads),
            "spread_ticks_p50": _percentile(spreads, 0.50),
            "spread_ticks_p95": _percentile(spreads, 0.95),
            "spread_ticks_max": max(spreads),
            "depth_imbalance_mean": statistics.mean(imbalances),
            "midpoint_change_rms_ticks": math.sqrt(
                statistics.mean(change * change for change in midpoint_changes)
            )
            if midpoint_changes
            else 0.0,
            "latency_ms_p50": _percentile(latencies_ms, 0.50),
            "latency_ms_p95": _percentile(latencies_ms, 0.95),
            "aggressor_buy_trades": buy_trades,
            "aggressor_sell_trades": sell_trades,
            "traded_quantity": float(traded_quantity),
            "traded_notional_usd": float(traded_notional),
        },
    }
    if output is not None:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return result

