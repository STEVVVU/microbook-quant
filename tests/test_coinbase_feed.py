import pytest

from microbook.feeds.coinbase import (
    CoinbaseBook,
    L2Snapshot,
    L2Update,
    Trade,
    parse_message,
    price_to_ticks,
)


def test_snapshot_and_updates_reconstruct_integer_tick_book() -> None:
    snapshot = parse_message(
        {
            "type": "snapshot",
            "product_id": "BTC-USD",
            "bids": [["100.00", "1.5"], ["99.99", "2.0"]],
            "asks": [["100.01", "1.25"], ["100.02", "3.0"]],
        },
        received_ns=10,
    )
    assert isinstance(snapshot, L2Snapshot)
    book = CoinbaseBook()
    book.apply(snapshot)
    assert book.best_bid == 10_000
    assert book.best_ask == 10_001

    update = parse_message(
        {
            "type": "l2update",
            "product_id": "BTC-USD",
            "time": "2026-01-02T03:04:05.123456Z",
            "changes": [
                ["buy", "100.00", "0"],
                ["buy", "99.99", "4.5"],
                ["sell", "100.01", "2.25"],
            ],
        },
        received_ns=20,
    )
    assert isinstance(update, L2Update)
    book.apply(update)
    assert book.best_bid == 9_999
    assert book.bids[9_999] == 4.5
    assert book.asks[10_001] == 2.25
    assert book.depth("sell", levels=2) == 5.25


def test_match_side_is_normalized_to_aggressor() -> None:
    event = parse_message(
        {
            "type": "match",
            "product_id": "BTC-USD",
            "trade_id": 42,
            "price": "100.01",
            "size": "0.25",
            "side": "sell",
            "time": "2026-01-02T03:04:05Z",
        },
        received_ns=30,
    )
    assert isinstance(event, Trade)
    assert event.aggressor_side == "buy"
    assert event.price_ticks == 10_001


def test_update_before_snapshot_and_misaligned_prices_are_rejected() -> None:
    book = CoinbaseBook()
    update = L2Update(
        kind="l2update",
        product_id="BTC-USD",
        received_ns=1,
        exchange_ns=None,
        changes=(),
    )
    with pytest.raises(RuntimeError, match="before snapshot"):
        book.apply(update)
    with pytest.raises(ValueError, match="not aligned"):
        price_to_ticks("100.001")

