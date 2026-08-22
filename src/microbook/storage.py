"""Columnar (parquet) storage for verified captures and run outputs.

Why two formats
===============

``gzip`` JSONL stays the **capture** format. It is append-only, one self-describing
record per line, survives a truncated write, and preserves the exact decimal
quantity *strings* Kraken used -- which is a hard requirement, because the CRC32
checksum is computed over those strings and re-serializing through a float would
break byte-level checksum reproduction.

Parquet is the **analysis** format: columnar, typed, compressed, and directly
loadable into pandas/arrow. This mirrors how desks actually store tick data --
raw immutable capture on one side, columnar analytical store on the other.

Type discipline
---------------

Prices cross into parquet as ``int64`` **ticks**, never floats. Quantities that
came off the wire as decimal strings stay ``string`` so the capture remains
checksum-reproducible from the parquet copy alone; a derived ``float64`` column is
added alongside purely for convenience in aggregation. Timestamps stay ``int64``
nanoseconds rather than a lossy datetime cast.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .feeds.kraken import (
    KRAKEN_BTC_TICK,
    KrakenSnapshot,
    KrakenTrade,
    KrakenUpdate,
    event_from_dict,
)

# One row per (event, side, level). Prices are integer ticks; `quantity` keeps the
# exchange's original decimal string so checksums stay reproducible from parquet.
BOOK_SCHEMA = pa.schema(
    [
        pa.field("sequence", pa.int64(), nullable=False),
        pa.field("kind", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("received_ns", pa.int64(), nullable=False),
        pa.field("exchange_ns", pa.int64(), nullable=False),
        pa.field("checksum", pa.uint32(), nullable=False),
        pa.field("side", pa.string(), nullable=False),
        pa.field("level", pa.int32(), nullable=False),
        pa.field("price_ticks", pa.int64(), nullable=False),
        pa.field("quantity", pa.string(), nullable=False),
        pa.field("quantity_float", pa.float64(), nullable=False),
    ]
)

# One row per trade print.
TRADE_SCHEMA = pa.schema(
    [
        pa.field("sequence", pa.int64(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("received_ns", pa.int64(), nullable=False),
        pa.field("exchange_ns", pa.int64(), nullable=False),
        pa.field("trade_id", pa.int64(), nullable=False),
        pa.field("price_ticks", pa.int64(), nullable=False),
        pa.field("quantity", pa.string(), nullable=False),
        pa.field("quantity_float", pa.float64(), nullable=False),
        pa.field("aggressor_side", pa.string(), nullable=False),
    ]
)

# One row per event that carried a book image, with the reconstructed top of book.
TOP_OF_BOOK_SCHEMA = pa.schema(
    [
        pa.field("sequence", pa.int64(), nullable=False),
        pa.field("received_ns", pa.int64(), nullable=False),
        pa.field("exchange_ns", pa.int64(), nullable=False),
        pa.field("latency_ms", pa.float64(), nullable=False),
        pa.field("best_bid_ticks", pa.int64(), nullable=False),
        pa.field("best_ask_ticks", pa.int64(), nullable=False),
        pa.field("spread_ticks", pa.int64(), nullable=False),
        pa.field("midpoint_ticks", pa.float64(), nullable=False),
        pa.field("bid_depth", pa.float64(), nullable=False),
        pa.field("ask_depth", pa.float64(), nullable=False),
        pa.field("depth_imbalance", pa.float64(), nullable=False),
    ]
)


def iter_capture_events(capture: str | Path) -> Iterator[tuple[int, object]]:
    """Yield ``(sequence, event)`` from a gzip-JSONL capture, in recorded order."""
    path = Path(capture)
    with gzip.open(path, "rt", encoding="utf-8") as source:
        for sequence, line in enumerate(source):
            try:
                yield sequence, event_from_dict(json.loads(line))
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"invalid capture event at line {sequence + 1}") from error


def capture_to_tables(capture: str | Path) -> dict[str, pa.Table]:
    """Convert a gzip-JSONL capture into ``book`` and ``trade`` arrow tables.

    Purely a re-encoding: no checksum revalidation and no book reconstruction
    happen here. Use :func:`microbook.replay.replay_kraken_capture` for that --
    keeping the two separate means the parquet writer can never launder a
    corrupt capture into a clean-looking analytical store.
    """
    book_rows: list[dict[str, object]] = []
    trade_rows: list[dict[str, object]] = []

    for sequence, event in iter_capture_events(capture):
        if isinstance(event, (KrakenSnapshot, KrakenUpdate)):
            for side, levels in (("bid", event.bids), ("ask", event.asks)):
                for level_index, level in enumerate(levels):
                    book_rows.append(
                        {
                            "sequence": sequence,
                            "kind": event.kind,
                            "symbol": event.symbol,
                            "received_ns": event.received_ns,
                            "exchange_ns": event.exchange_ns,
                            "checksum": event.checksum,
                            "side": side,
                            "level": level_index,
                            "price_ticks": level.price_ticks,
                            "quantity": level.quantity,
                            "quantity_float": float(Decimal(level.quantity)),
                        }
                    )
        elif isinstance(event, KrakenTrade):
            trade_rows.append(
                {
                    "sequence": sequence,
                    "symbol": event.symbol,
                    "received_ns": event.received_ns,
                    "exchange_ns": event.exchange_ns,
                    "trade_id": event.trade_id,
                    "price_ticks": event.price_ticks,
                    "quantity": event.quantity,
                    "quantity_float": float(Decimal(event.quantity)),
                    "aggressor_side": event.aggressor_side,
                }
            )

    return {
        "book": pa.Table.from_pylist(book_rows, schema=BOOK_SCHEMA),
        "trade": pa.Table.from_pylist(trade_rows, schema=TRADE_SCHEMA),
    }


def write_capture_parquet(
    capture: str | Path, output_dir: str | Path, *, compression: str = "zstd"
) -> dict[str, Path]:
    """Write ``book.parquet`` and ``trade.parquet`` beside each other.

    Returns the written paths. ``zstd`` is the default because tick data is
    highly repetitive at the column level and it decompresses faster than gzip.
    """
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, table in capture_to_tables(capture).items():
        path = destination / f"{name}.parquet"
        pq.write_table(table, path, compression=compression)
        written[name] = path
    return written


def top_of_book_table(observations: list[dict[str, object]]) -> pa.Table:
    """Build the reconstructed top-of-book table from replay observations."""
    return pa.Table.from_pylist(observations, schema=TOP_OF_BOOK_SCHEMA)


def write_top_of_book_parquet(
    observations: list[dict[str, object]],
    output: str | Path,
    *,
    compression: str = "zstd",
) -> Path:
    """Persist the reconstructed per-event top of book as parquet."""
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(top_of_book_table(observations), path, compression=compression)
    return path


def ticks_to_usd(price_ticks: int, *, tick_size: Decimal = KRAKEN_BTC_TICK) -> Decimal:
    """Exact tick -> USD conversion for reporting only.

    Returns :class:`~decimal.Decimal`, not ``float``: this exists so a parquet
    consumer can render human-readable prices without ever introducing a float
    price into a comparison.
    """
    return Decimal(price_ticks) * tick_size
