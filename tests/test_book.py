from microbook.book import LimitOrderBook, Order, Side


def test_price_time_priority() -> None:
    book = LimitOrderBook()
    book.submit(Order(1, "first", Side.SELL, 2, 1, 101))
    book.submit(Order(2, "second", Side.SELL, 2, 2, 101))
    result = book.submit(Order(3, "taker", Side.BUY, 3, 3))
    assert [(fill.maker_order_id, fill.quantity) for fill in result.fills] == [(1, 2), (2, 1)]
    assert book.orders[2].remaining == 1
    book.assert_invariants()


def test_post_only_rejects_cross_and_cancel_checks_owner() -> None:
    book = LimitOrderBook()
    book.submit(Order(1, "maker", Side.SELL, 2, 1, 101))
    rejected = book.submit(Order(2, "agent", Side.BUY, 1, 2, 101), post_only=True)
    assert not rejected.accepted
    assert not book.cancel(1, owner="wrong")
    assert book.cancel(1, owner="maker")


def test_partial_market_order_stops_at_empty_book() -> None:
    book = LimitOrderBook()
    book.submit(Order(1, "maker", Side.SELL, 2, 1, 101))
    result = book.submit(Order(2, "taker", Side.BUY, 5, 2))
    assert sum(fill.quantity for fill in result.fills) == 2
    assert book.best_ask is None

