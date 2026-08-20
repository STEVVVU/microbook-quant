"""Tests for the vectorized analytics layer.

The important ones here are the *parity* tests: they pin the numpy/pandas
reductions to the scalar stdlib results they replaced, so a future refactor of
the analysis layer cannot silently move a published number.
"""

import math
import statistics

import numpy as np
import pandas as pd
import pytest

from microbook import analytics
from microbook.simulator import MarkoutRecord, QuoteRecord


def test_fsum_mean_is_order_independent() -> None:
    # Classic catastrophic-cancellation ordering: a naive left-to-right sum gives
    # a different answer depending on permutation; fsum does not.
    values = [1e16, 1.0, -1e16, 1.0, 0.5, -0.5, 3.25]
    forward = analytics.fsum_mean(values)
    backward = analytics.fsum_mean(list(reversed(values)))
    assert forward == backward
    # Exact total is 5.25; a naive float sum loses the 1.0 terms next to 1e16.
    assert forward == pytest.approx(5.25 / len(values), rel=0, abs=1e-12)
    assert np.mean(values) != forward


def test_fsum_mean_and_population_std_match_stdlib() -> None:
    values = [0.1, 0.2, 0.30000000000001, -4.5, 17.25, 0.0, -0.125]
    assert analytics.fsum_mean(values) == pytest.approx(statistics.mean(values), abs=1e-12)
    assert analytics.population_std(values) == pytest.approx(
        statistics.pstdev(values), abs=1e-12
    )


def test_fsum_mean_and_std_handle_degenerate_input() -> None:
    assert analytics.fsum_mean([]) == 0.0
    assert analytics.population_std([]) == 0.0
    assert analytics.population_std([4.0]) == 0.0
    assert analytics.percentile([], 0.5) == 0.0


def test_percentile_matches_the_scalar_estimator_it_replaced() -> None:
    def scalar_percentile(values: list[float], fraction: float) -> float:
        ordered = sorted(values)
        position = fraction * (len(ordered) - 1)
        lower, upper = math.floor(position), math.ceil(position)
        if lower == upper:
            return ordered[lower]
        weight = position - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    values = [5.0, 1.0, 3.0, 9.0, 2.0, 7.0, 8.0, 4.0]
    for fraction in (0.0, 0.25, 0.5, 0.75, 0.95, 1.0):
        assert analytics.percentile(values, fraction) == pytest.approx(
            scalar_percentile(values, fraction), abs=1e-12
        )


def test_max_drawdown_matches_running_peak_loop() -> None:
    curve = [0.0, 5.0, 3.0, 9.0, 1.0, 4.0, -2.0, 6.0]
    peak, expected = -math.inf, 0.0
    for value in curve:
        peak = max(peak, value)
        expected = max(expected, peak - value)
    assert analytics.max_drawdown_ticks(curve) == expected
    frame = analytics.pnl_frame(curve, [0] * len(curve))
    assert analytics.max_drawdown_ticks(frame) == expected


def test_max_drawdown_and_rms_are_empty_safe() -> None:
    assert analytics.max_drawdown_ticks([]) == 0.0
    assert analytics.inventory_rms([]) == 0.0
    assert analytics.path_ratio([]) == 0.0


def test_inventory_rms_uses_exact_integer_sum_of_squares() -> None:
    inventory = [3, -7, 0, 12, -12, 5]
    expected = math.sqrt(sum(value * value for value in inventory) / len(inventory))
    assert analytics.inventory_rms(inventory) == expected


def test_pnl_frame_shape_and_drawdown_columns() -> None:
    frame = analytics.pnl_frame([0.0, 2.0, 1.0], [0, 1, -1])
    assert list(frame.columns) == [
        "event",
        "pnl_ticks",
        "inventory_lots",
        "running_peak_ticks",
        "drawdown_ticks",
    ]
    assert frame["running_peak_ticks"].tolist() == [0.0, 2.0, 2.0]
    assert frame["drawdown_ticks"].tolist() == [0.0, 0.0, 1.0]
    # Inventory stays integral all the way into the frame.
    assert frame["inventory_lots"].dtype == np.int64


def test_path_ratio_matches_the_scalar_formula() -> None:
    curve = [float(index % 37) * 1.5 for index in range(1_000)]
    sampled = curve[::100]
    increments = [b - a for a, b in zip(sampled[:-1], sampled[1:], strict=True)]
    deviation = statistics.pstdev(increments)
    expected = statistics.mean(increments) / deviation * math.sqrt(len(increments))
    assert analytics.path_ratio(curve, stride=100) == pytest.approx(expected, abs=1e-9)


def test_path_ratio_is_zero_for_a_flat_curve() -> None:
    assert analytics.path_ratio([7.0] * 500, stride=100) == 0.0


def _quote(order_id: int, queue_ahead: int, filled: int, size: int = 2) -> QuoteRecord:
    return QuoteRecord(
        order_id=order_id,
        side="buy",
        price_ticks=10_000,
        submitted_event=order_id,
        queue_ahead_lots=queue_ahead,
        size_lots=size,
        filled_lots=filled,
    )


def test_quote_frame_derives_fill_flags_and_queue_buckets() -> None:
    frame = analytics.quote_frame(
        [_quote(1, 0, 2), _quote(2, 4, 1), _quote(3, 30, 0), _quote(4, 1, 0)]
    )
    assert frame["filled"].tolist() == [True, True, False, False]
    assert frame["fully_filled"].tolist() == [True, False, False, False]
    # Buckets are left-closed, so "joined an empty level" is its own bucket.
    assert frame["queue_bucket"].astype(str).tolist() == ["0", "3-5", "25+", "1-2"]
    # Prices and sizes never become floats on the way into the frame.
    assert frame["price_ticks"].dtype == np.int64
    assert frame["queue_ahead_lots"].dtype == np.int64


def test_quote_frame_is_empty_safe_with_typed_columns() -> None:
    frame = analytics.quote_frame([])
    assert frame.empty
    assert "queue_bucket" in frame.columns
    assert frame["price_ticks"].dtype == np.int64


def test_queue_position_outcomes_reports_fill_probability_per_bucket() -> None:
    quotes = analytics.quote_frame(
        [
            _quote(1, 0, 2),
            _quote(2, 0, 2),
            _quote(3, 0, 0),
            _quote(4, 30, 0),
            _quote(5, 30, 0),
        ]
    )
    markouts = analytics.markout_frame(
        [
            MarkoutRecord(1, "buy", 10_000, 5, -1.5),
            MarkoutRecord(2, "buy", 10_000, 6, -0.5),
        ]
    )
    summary = analytics.queue_position_outcomes(quotes, markouts)
    summary["queue_bucket"] = summary["queue_bucket"].astype(str)
    summary = summary.set_index("queue_bucket")
    assert summary.loc["0", "orders"] == 3
    assert summary.loc["0", "filled_orders"] == 2
    assert summary.loc["0", "fill_probability"] == pytest.approx(2 / 3)
    assert summary.loc["0", "mean_markout_ticks"] == pytest.approx(-1.0)
    assert summary.loc["25+", "fill_probability"] == pytest.approx(0.0)
    # An untouched bucket reports no orders rather than being dropped.
    assert summary.loc["6-11", "orders"] == 0


def test_queue_position_outcomes_without_markouts() -> None:
    quotes = analytics.quote_frame([_quote(1, 0, 2)])
    summary = analytics.queue_position_outcomes(quotes)
    assert summary["orders"].sum() == 1
    assert summary["mean_markout_ticks"].tolist() == [0.0] * len(summary)


def test_markout_frame_columns_and_empty_case() -> None:
    frame = analytics.markout_frame([MarkoutRecord(1, "sell", 10_001, 3, 0.5)])
    assert frame["markout_ticks"].tolist() == [0.5]
    assert analytics.markout_frame([]).empty


def test_latency_histogram_counts_every_observation() -> None:
    latencies = [1.0, 1.5, 2.0, 8.0, 8.5, 40.0]
    frame = analytics.latency_histogram(latencies, bins=4)
    assert frame["count"].sum() == len(latencies)
    assert (frame["bin_right_ms"] > frame["bin_left_ms"]).all()
    assert isinstance(frame, pd.DataFrame)
    assert analytics.latency_histogram([]).empty
