import asyncio
import json
from decimal import Decimal

import pytest

from microbook.feeds.kraken import KrakenBook, KrakenSnapshot, parse_kraken_message
from microbook.feeds.kraken_recorder import record_kraken_messages
from microbook.replay import replay_kraken_capture


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
                "bids": [{"price": 100.0, "qty": 1.25}],
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


def test_replay_revalidates_capture_and_emits_metrics(tmp_path) -> None:
    capture = tmp_path / "capture.jsonl.gz"
    asyncio.run(
        record_kraken_messages(
            _messages([_snapshot_message()]),
            capture,
            symbol="BTC/USD",
            depth=10,
            max_events=1,
        )
    )
    output = tmp_path / "report.json"
    result = replay_kraken_capture(capture, output=output)

    assert result["integrity"]["events"] == 1
    assert result["integrity"]["checksums_verified"] == 1
    assert result["integrity"]["crossed_book_states"] == 0
    assert result["market"]["spread_ticks_mean"] == 1.0
    assert output.exists()


def test_replay_rejects_corrupt_event(tmp_path) -> None:
    capture = tmp_path / "bad.jsonl.gz"
    import gzip

    with gzip.open(capture, "wt", encoding="utf-8") as destination:
        destination.write('{"kind":"unknown"}\n')
    with pytest.raises(ValueError, match="invalid capture event"):
        replay_kraken_capture(capture)

