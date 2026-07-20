from microbook.strategy import InventoryAware


def test_positive_inventory_skews_quotes_down() -> None:
    strategy = InventoryAware(risk_aversion=0.5)
    flat = strategy.quote(100.0, 0, 1.0)
    long = strategy.quote(100.0, 8, 1.0)
    assert long.bid < flat.bid
    assert long.ask < flat.ask


def test_inventory_limit_disables_risk_increasing_side() -> None:
    strategy = InventoryAware(inventory_limit=10)
    quote = strategy.quote(100.0, 10, 1.0)
    assert quote.bid is None
    assert quote.ask is not None

