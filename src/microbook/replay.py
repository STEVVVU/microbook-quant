"""Deterministic integrity and microstructure analytics over recorded Kraken L2.

Book reconstruction below stays event-by-event and integer-tick: each update is
applied in recorded order and its exchange CRC32 is verified against the decimal
quantity strings before the book is trusted. Only the *summary statistics* over
the resulting observation series are vectorized (see :mod:`microbook.analytics`).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from decimal import Decimal
from pathlib import Path

import numpy as np

from . import analytics
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
    """Linear-interpolated percentile. Delegates to the vectorized helper."""
    return analytics.percentile(values, fraction)


def replay_kraken_capture(
    capture: str | Path,
    *,
    symbol: str = "BTC/USD",
    depth: int = 10,
    output: str | Path | None = None,
    parquet_dir: str | Path | None = None,
) -> dict[str, object]:
    """Revalidate a capture and compute integrity + microstructure statistics.

    When ``parquet_dir`` is given, the capture is additionally re-encoded to
    columnar parquet (raw book/trade rows plus the reconstructed top-of-book
    series) for analysis. The JSONL capture remains the authoritative record.
    """
    capture = Path(capture)
    book = KrakenBook(symbol, depth=depth)
    counts = {"snapshot": 0, "update": 0, "trade": 0}
    spreads: list[float] = []
    imbalances: list[float] = []
    latencies_ms: list[float] = []
    midpoints: list[float] = []
    # Per-event reconstructed top of book, kept for the columnar analysis path.
    observations: list[dict[str, object]] = []
    first_received_ns: int | None = None
    last_received_ns: int | None = None
    top_of_book_changes = 0
    # Counted, not assumed. ``KrakenBook.apply`` already raises on a crossed book,
    # so this stays 0 on a clean capture -- but it is an independent observation of
    # each reconstructed top of book rather than a hardcoded zero, so the reported
    # figure is something the replay actually measured.
    crossed_book_states = 0
    previous_top: tuple[int, int] | None = None
    buy_trades = 0
    sell_trades = 0
    traded_quantity = Decimal(0)
    traded_notional = Decimal(0)

    with gzip.open(capture, "rt", encoding="utf-8") as source:
        for sequence, line in enumerate(source):
            line_number = sequence + 1
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
                crossed_book_states += int(book.best_bid >= book.best_ask)
                spread_ticks = book.best_ask - book.best_bid
                midpoint_ticks = (book.best_bid + book.best_ask) / 2.0
                spreads.append(float(spread_ticks))
                midpoints.append(midpoint_ticks)
                bid_depth = book.depth("buy")
                ask_depth = book.depth("sell")
                total_depth = bid_depth + ask_depth
                # Depth is Decimal off the wire; the imbalance ratio is the only
                # place it becomes float, and it is a statistic, not a price.
                imbalance = (
                    float((bid_depth - ask_depth) / total_depth) if total_depth > 0 else 0.0
                )
                if total_depth > 0:
                    imbalances.append(imbalance)
                latency_ms = (event.received_ns - event.exchange_ns) / 1_000_000
                latencies_ms.append(latency_ms)
                observations.append(
                    {
                        "sequence": sequence,
                        "received_ns": event.received_ns,
                        "exchange_ns": event.exchange_ns,
                        "latency_ms": latency_ms,
                        "best_bid_ticks": book.best_bid,
                        "best_ask_ticks": book.best_ask,
                        "spread_ticks": spread_ticks,
                        "midpoint_ticks": midpoint_ticks,
                        "bid_depth": float(bid_depth),
                        "ask_depth": float(ask_depth),
                        "depth_imbalance": imbalance,
                    }
                )
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
    # Vectorized reductions over the observation series. np.diff is one
    # subtraction per element, so the increment series is identical to the
    # scalar pairwise loop it replaced.
    midpoint_changes = np.diff(np.asarray(midpoints, dtype=np.float64))
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
            "crossed_book_states": crossed_book_states,
        },
        "market": {
            "updates_per_second": counts["update"] / max(duration_seconds, 1e-12),
            "top_of_book_changes": top_of_book_changes,
            "spread_ticks_mean": analytics.fsum_mean(spreads),
            "spread_ticks_p50": _percentile(spreads, 0.50),
            "spread_ticks_p95": _percentile(spreads, 0.95),
            "spread_ticks_max": max(spreads),
            "depth_imbalance_mean": analytics.fsum_mean(imbalances),
            "midpoint_change_rms_ticks": math.sqrt(
                analytics.fsum_mean(midpoint_changes * midpoint_changes)
            )
            if midpoint_changes.size
            else 0.0,
            "latency_ms_p50": _percentile(latencies_ms, 0.50),
            "latency_ms_p95": _percentile(latencies_ms, 0.95),
            "aggressor_buy_trades": buy_trades,
            "aggressor_sell_trades": sell_trades,
            "traded_quantity": float(traded_quantity),
            "traded_notional_usd": float(traded_notional),
        },
    }
    if parquet_dir is not None:
        # Columnar copy for analysis only; the gzip JSONL capture stays canonical.
        from . import storage

        written = storage.write_capture_parquet(capture, parquet_dir)
        written["top_of_book"] = storage.write_top_of_book_parquet(
            observations, Path(parquet_dir) / "top_of_book.parquet"
        )
        result["parquet"] = {name: str(path) for name, path in sorted(written.items())}

    if output is not None:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" pins LF on every platform. Without it Windows text mode
        # rewrites each \n as \r\n, so the same deterministic replay emits
        # different bytes on Windows and Linux -- which would make a
        # byte-for-byte `git diff --exit-code` check on this committed artifact
        # depend on the developer's core.autocrlf setting rather than the code.
        destination.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return result

