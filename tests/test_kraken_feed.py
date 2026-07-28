from decimal import Decimal

import pytest

from microbook.feeds.kraken import KrakenBook, KrakenSnapshot, KrakenUpdate, parse_kraken_message


SNAPSHOT = {
    "channel": "book",
    "type": "snapshot",
    "data": [
        {
            "symbol": "BTC/USD",
            "bids": [
                {"price": Decimal("100.0"), "qty": Decimal("1.25000000")},
                {"price": Decimal("99.9"), "qty": Decimal("2.00000000")},
            ],
            "asks": [
                {"price": Decimal("100.1"), "qty": Decimal("0.50000000")},
                {"price": Decimal("100.2"), "qty": Decimal("3.00000000")},
            ],
            "checksum": 0,
            "timestamp": "2026-07-16T03:59:08.420296Z",
        }
    ],
}


def _with_checksum(message):
    events = parse_kraken_message(message, received_ns=10)
    event = events[0]
    book = KrakenBook(depth=10)
    if isinstance(event, KrakenSnapshot):
        book.bids = {
            level.price_ticks: Decimal(level.quantity) for level in event.bids
        }
        book.asks = {
            level.price_ticks: Decimal(level.quantity) for level in event.asks
        }
    message["data"][0]["checksum"] = book.checksum()
    return message


def test_snapshot_and_update_verify_exchange_checksums() -> None:
    snapshot = parse_kraken_message(_with_checksum(SNAPSHOT), received_ns=10)[0]
    assert isinstance(snapshot, KrakenSnapshot)
    book = KrakenBook()
    book.apply(snapshot)

    update_message = {
        "channel": "book",
        "type": "update",
        "data": [
            {
                "symbol": "BTC/USD",
                "bids": [{"price": Decimal("100.0"), "qty": Decimal("2.50000000")}],
                "asks": [{"price": Decimal("100.1"), "qty": Decimal("0.00000000")}],
                "checksum": 0,
                "timestamp": "2026-07-16T03:59:09Z",
            }
        ],
    }
    preview = parse_kraken_message(update_message, received_ns=20)[0]
    assert isinstance(preview, KrakenUpdate)
    preview_book = KrakenBook()
    preview_book.bids = dict(book.bids)
    preview_book.asks = dict(book.asks)
    preview_book.initialized = True
    preview_book._apply_levels(preview.bids, bid=True)
    preview_book._apply_levels(preview.asks, bid=False)
    preview_book._prune()
    update_message["data"][0]["checksum"] = preview_book.checksum()

    update = parse_kraken_message(update_message, received_ns=20)[0]
    book.apply(update)
    assert book.best_bid == 1_000
    assert book.best_ask == 1_002
    assert book.checksums_verified == 2


def test_checksum_mismatch_is_rejected() -> None:
    snapshot = parse_kraken_message(SNAPSHOT, received_ns=10)[0]
    assert isinstance(snapshot, KrakenSnapshot)
    bad = KrakenSnapshot(**{**snapshot.__dict__, "checksum": snapshot.checksum + 1})
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        KrakenBook().apply(bad)


def test_trade_batch_is_normalized() -> None:
    events = parse_kraken_message(
        {
            "channel": "trade",
            "type": "update",
            "data": [
                {
                    "symbol": "BTC/USD",
                    "side": "buy",
                    "qty": Decimal("0.00100000"),
                    "price": Decimal("100.1"),
                    "trade_id": 17,
                    "timestamp": "2026-07-16T03:59:10Z",
                }
            ],
        },
        received_ns=30,
    )
    assert len(events) == 1
    assert events[0].aggressor_side == "buy"
    assert events[0].price_ticks == 1_001

