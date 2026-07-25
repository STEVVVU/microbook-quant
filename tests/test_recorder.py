import asyncio
import gzip
import json

import pytest

from microbook.feeds.recorder import record_messages


async def _messages(payloads):
    for payload in payloads:
        yield json.dumps(payload)


def test_recorder_writes_normalized_atomic_capture(tmp_path) -> None:
    output = tmp_path / "capture.jsonl.gz"
    payloads = [
        {"type": "subscriptions", "channels": []},
        {
            "type": "snapshot",
            "product_id": "BTC-USD",
            "bids": [["100.00", "1.0"]],
            "asks": [["100.01", "2.0"]],
        },
        {
            "type": "l2update",
            "product_id": "BTC-USD",
            "changes": [["buy", "100.00", "1.5"]],
        },
        {
            "type": "match",
            "product_id": "BTC-USD",
            "trade_id": 7,
            "price": "100.01",
            "size": "0.1",
            "side": "sell",
        },
    ]

    summary = asyncio.run(
        record_messages(
            _messages(payloads),
            output,
            product_id="BTC-USD",
            max_events=3,
        )
    )

    assert summary.events == 3
    assert summary.snapshots == 1
    assert summary.updates == 1
    assert summary.trades == 1
    assert summary.final_best_bid_ticks == 10_000
    assert summary.final_best_ask_ticks == 10_001
    assert len(summary.sha256) == 64
    with gzip.open(output, "rt", encoding="utf-8") as source:
        assert [json.loads(line)["kind"] for line in source] == [
            "snapshot",
            "l2update",
            "trade",
        ]
    assert not output.with_suffix(".gz.part").exists()


def test_recorder_rejects_stream_without_snapshot(tmp_path) -> None:
    output = tmp_path / "capture.jsonl.gz"
    with pytest.raises(RuntimeError, match="snapshot"):
        asyncio.run(
            record_messages(
                _messages([{"type": "heartbeat"}]),
                output,
                product_id="BTC-USD",
                max_events=1,
            )
        )
    assert not output.exists()

