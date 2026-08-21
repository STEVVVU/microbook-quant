"""Tests for the simulator's per-quote / per-markout instrumentation.

These guard the architectural boundary: the event loop must keep prices and sizes
integral, and adding the recording must not have perturbed the seeded path.
"""

from microbook import analytics
from microbook.experiment import ExperimentConfig, run_comparison
from microbook.simulator import MarketSimulator, SimulationConfig
from microbook.strategy import FixedSpread, InventoryAware


def test_run_and_run_detailed_agree() -> None:
    config = SimulationConfig(events=2_000, seed=5)
    metrics = MarketSimulator(FixedSpread(), config).run()
    detailed = MarketSimulator(FixedSpread(), config).run_detailed()
    assert metrics == detailed.metrics


def test_detailed_run_is_deterministic_including_records() -> None:
    config = SimulationConfig(events=2_000, seed=5)
    first = MarketSimulator(InventoryAware(), config).run_detailed()
    second = MarketSimulator(InventoryAware(), config).run_detailed()
    assert first.metrics == second.metrics
    assert first.pnl_curve == second.pnl_curve
    assert first.inventory_curve == second.inventory_curve
    assert first.quotes == second.quotes
    assert first.markouts == second.markouts


def test_recorded_quotes_stay_integral() -> None:
    result = MarketSimulator(
        InventoryAware(), SimulationConfig(events=4_000, seed=11)
    ).run_detailed()
    assert result.quotes, "expected at least one resting agent quote"
    for record in result.quotes:
        # Prices are ticks and sizes are lots: never floats in the book path.
        assert isinstance(record.price_ticks, int) and not isinstance(record.price_ticks, bool)
        assert isinstance(record.queue_ahead_lots, int)
        assert isinstance(record.size_lots, int)
        assert isinstance(record.filled_lots, int)
        assert record.price_ticks > 0
        assert record.queue_ahead_lots >= 0
        assert 0 <= record.filled_lots <= record.size_lots
        assert record.side in {"buy", "sell"}
    assert all(isinstance(value, int) for value in result.inventory_curve)


def test_recorded_quote_count_matches_accepted_quotes() -> None:
    result = MarketSimulator(
        FixedSpread(), SimulationConfig(events=4_000, seed=7)
    ).run_detailed()
    # One record per quote that actually rested on the book.
    assert len(result.quotes) == result.metrics["quotes_sent"] - result.metrics[
        "post_only_rejections"
    ]


def test_filled_lots_reconcile_with_the_aggregate_fill_counters() -> None:
    result = MarketSimulator(
        InventoryAware(), SimulationConfig(events=6_000, seed=3)
    ).run_detailed()
    assert sum(record.filled_lots for record in result.quotes) == result.metrics[
        "filled_quantity"
    ]
    frame = analytics.quote_frame(result.quotes)
    assert int(frame["filled_lots"].sum()) == result.metrics["filled_quantity"]


def test_markout_records_agree_with_the_reported_mean() -> None:
    result = MarketSimulator(
        InventoryAware(), SimulationConfig(events=6_000, seed=3)
    ).run_detailed()
    expected = analytics.fsum_mean(record.markout_ticks for record in result.markouts)
    assert result.metrics["mean_20_event_markout_ticks"] == expected


def test_every_markout_maps_back_to_a_recorded_quote() -> None:
    result = MarketSimulator(
        InventoryAware(), SimulationConfig(events=6_000, seed=3)
    ).run_detailed()
    order_ids = {record.order_id for record in result.quotes}
    assert result.markouts
    assert all(record.order_id in order_ids for record in result.markouts)


def test_queue_position_summary_is_reported_in_the_comparison() -> None:
    result = run_comparison(ExperimentConfig(events=6_000, seed=2))
    assert set(result["queue_position"]) == {"fixed_spread", "inventory_aware"}
    for rows in result["queue_position"].values():
        assert rows
        assert sum(row["orders"] for row in rows) > 0
        for row in rows:
            assert row["filled_orders"] <= row["orders"]
            if row["fill_probability"] is not None:
                assert 0.0 <= row["fill_probability"] <= 1.0


def test_comparison_writes_parquet_and_figures_when_asked(tmp_path) -> None:
    run_comparison(
        ExperimentConfig(events=3_000, seed=2),
        output_dir=tmp_path,
        write_artifacts=True,
    )
    for name in ("comparison.json", "queue_position_fill_probability.png", "pnl_inventory.png"):
        assert (tmp_path / name).exists()
    for strategy in ("fixed_spread", "inventory_aware"):
        assert (tmp_path / f"quotes_{strategy}.parquet").exists()
        assert (tmp_path / f"markouts_{strategy}.parquet").exists()
