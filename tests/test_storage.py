"""Tests for the columnar (parquet) analysis path.

The load-bearing assertion here is the round-trip one: a book image read back out
of parquet must reproduce the exchange CRC32 exactly. That only holds if prices
stay integer ticks and quantities keep their original decimal *strings*, so this
test is what stops someone "simplifying" those columns to float64.
"""

import asyncio
import gzip
import json
from decimal import Decimal

import pyarrow.parquet as pq
import pytest

from microbook.feeds.kraken import KrakenBook, KrakenSnapshot, parse_kraken_message
from microbook.feeds.kraken_recorder import record_kraken_messages
from microbook.replay import replay_kraken_capture
from microbook.storage import (
    BOOK_SCHEMA,
    TRADE_SCHEMA,
    capture_to_tables,
    ticks_to_usd,
    write_capture_parquet,
)


async def _messages(payloads):
    for payload in payloads:
        yield json.dumps(payload)


def _snapshot_message():
    message = {
        "channel": "book",
        "type": "snapshot",
        "data": [
            {
                "symbol": "BTC/USD",
                "bids": [{"price": 100.0, "qty": 1.25}, {"price": 99.9, "qty": 2.0}],
                "asks": [{"price": 100.1, "qty": 0.5}],
                "checksum": 0,
                "timestamp": "2026-07-16T03:59:08Z",
            }
        ],
    }
    event = parse_kraken_message(
        json.loads(json.dumps(message), parse_float=Decimal), received_ns=1
    )[0]
    assert isinstance(event, KrakenSnapshot)
    book = KrakenBook()
    book.bids = {level.price_ticks: Decimal(level.quantity) for level in event.bids}
    book.asks = {level.price_ticks: Decimal(level.quantity) for level in event.asks}
    message["data"][0]["checksum"] = book.checksum()
    return message


def _trade_message():
    return {
        "channel": "trade",
        "type": "update",
        "data": [
            {
                "symbol": "BTC/USD",
                "side": "buy",
                "qty": 0.01,
                "price": 100.1,
                "trade_id": 9,
                "timestamp": "2026-07-16T03:59:09Z",
            }
        ],
    }


@pytest.fixture
def capture(tmp_path):
    path = tmp_path / "capture.jsonl.gz"
    asyncio.run(
        record_kraken_messages(
            _messages([_snapshot_message(), _trade_message()]),
            path,
            symbol="BTC/USD",
            depth=10,
            max_events=2,
        )
    )
    return path


def test_capture_to_tables_uses_integer_ticks_and_string_quantities(capture) -> None:
    tables = capture_to_tables(capture)
    book, trade = tables["book"], tables["trade"]
    assert book.schema == BOOK_SCHEMA
    assert trade.schema == TRADE_SCHEMA
    # One row per (event, side, level): 2 bids + 1 ask from the snapshot.
    assert book.num_rows == 3
    assert trade.num_rows == 1
    assert str(book.schema.field("price_ticks").type) == "int64"
    assert str(book.schema.field("quantity").type) == "string"
    assert str(trade.schema.field("price_ticks").type) == "int64"
    assert book.column("price_ticks").to_pylist() == [1_000, 999, 1_001]
    assert book.column("side").to_pylist() == ["bid", "bid", "ask"]


def test_parquet_round_trip_reproduces_the_exchange_checksum(capture) -> None:
    # Rebuild the book from the *parquet* copy and recompute the CRC32. This is
    # only possible because quantities survive as exact decimal strings.
    table = capture_to_tables(capture)["book"]
    rows = table.to_pylist()
    snapshot_rows = [row for row in rows if row["kind"] == "snapshot"]
    book = KrakenBook()
    book.bids = {
        row["price_ticks"]: Decimal(row["quantity"])
        for row in snapshot_rows
        if row["side"] == "bid"
    }
    book.asks = {
        row["price_ticks"]: Decimal(row["quantity"])
        for row in snapshot_rows
        if row["side"] == "ask"
    }
    assert book.checksum() == snapshot_rows[0]["checksum"]


def test_write_capture_parquet_is_readable_and_preserves_row_counts(capture, tmp_path) -> None:
    written = write_capture_parquet(capture, tmp_path / "columnar")
    assert set(written) == {"book", "trade"}
    for path in written.values():
        assert path.exists()
    assert pq.read_table(written["book"]).num_rows == 3
    assert pq.read_table(written["trade"]).num_rows == 1


def test_replay_can_emit_parquet_without_changing_its_metrics(capture, tmp_path) -> None:
    plain = replay_kraken_capture(capture)
    columnar = replay_kraken_capture(capture, parquet_dir=tmp_path / "columnar")
    assert columnar["integrity"] == plain["integrity"]
    assert columnar["market"] == plain["market"]
    top_of_book = pq.read_table(columnar["parquet"]["top_of_book"])
    # One reconstructed top-of-book row per book event (the trade is excluded).
    assert top_of_book.num_rows == plain["integrity"]["snapshots"] + plain["integrity"]["updates"]
    assert str(top_of_book.schema.field("best_bid_ticks").type) == "int64"
    assert str(top_of_book.schema.field("spread_ticks").type) == "int64"


def test_corrupt_capture_is_rejected_by_the_parquet_writer(tmp_path) -> None:
    bad = tmp_path / "bad.jsonl.gz"
    with gzip.open(bad, "wt", encoding="utf-8") as destination:
        destination.write('{"kind":"unknown"}\n')
    with pytest.raises(ValueError, match="invalid capture event"):
        capture_to_tables(bad)


def test_ticks_to_usd_is_exact_decimal() -> None:
    price = ticks_to_usd(1_198_431)
    assert price == Decimal("119843.1")
    assert isinstance(price, Decimal)
