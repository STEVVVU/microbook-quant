import asyncio
import gzip
import json
from decimal import Decimal

from microbook.feeds.kraken import KrakenBook, KrakenSnapshot, parse_kraken_message
from microbook.feeds.kraken_recorder import record_kraken_messages


async def _messages(payloads):
    for payload in payloads:
        yield json.dumps(payload)


def _valid_snapshot():
    message = {
        "channel": "book",
        "type": "snapshot",
        "data": [
            {
                "symbol": "BTC/USD",
                "bids": [{"price": 100.0, "qty": 1.25}],
                "asks": [{"price": 100.1, "qty": 0.5}],
                "checksum": 0,
                "timestamp": "2026-07-16T03:59:08Z",
            }
        ],
    }
    event = parse_kraken_message(
        json.loads(json.dumps(message), parse_float=Decimal),
        received_ns=1,
    )[0]
    assert isinstance(event, KrakenSnapshot)
    book = KrakenBook()
    book.bids = {level.price_ticks: Decimal(level.quantity) for level in event.bids}
    book.asks = {level.price_ticks: Decimal(level.quantity) for level in event.asks}
    message["data"][0]["checksum"] = book.checksum()
    return message


def test_kraken_recorder_verifies_and_writes_capture(tmp_path) -> None:
    output = tmp_path / "capture.jsonl.gz"
    snapshot = _valid_snapshot()
    trade = {
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

    summary = asyncio.run(
        record_kraken_messages(
            _messages([snapshot, trade]),
            output,
            symbol="BTC/USD",
            depth=10,
            max_events=2,
        )
    )

    assert summary.venue == "Kraken"
    assert summary.events == 2
    assert summary.checksums_verified == 1
    assert summary.snapshots == 1
    assert summary.trades == 1
    assert len(summary.sha256) == 64
    with gzip.open(output, "rt", encoding="utf-8") as source:
        assert [json.loads(line)["kind"] for line in source] == ["snapshot", "trade"]

