"""Smoke tests for the matplotlib figures.

These assert the figures render to a non-empty PNG headlessly, which is what CI
needs to know. They deliberately do not assert on pixels.
"""

import pandas as pd

from microbook import analytics, plots
from microbook.simulator import MarkoutRecord, QuoteRecord


def _png_header(path) -> bytes:
    return path.read_bytes()[:8]


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_queue_position_figure_renders(tmp_path) -> None:
    quotes = analytics.quote_frame(
        [
            QuoteRecord(1, "buy", 10_000, 0, 0, 2, filled_lots=2),
            QuoteRecord(2, "sell", 10_002, 0, 5, 2, filled_lots=0),
            QuoteRecord(3, "buy", 9_999, 20, 30, 2, filled_lots=1),
        ]
    )
    markouts = analytics.markout_frame(
        [MarkoutRecord(1, "buy", 10_000, 5, -0.5), MarkoutRecord(3, "buy", 9_999, 25, 1.5)]
    )
    summary = analytics.queue_position_outcomes(quotes, markouts)
    output = plots.plot_queue_position_fill_probability(summary, tmp_path / "queue.png")
    assert output.exists()
    assert _png_header(output) == PNG_MAGIC


def test_pnl_and_inventory_figure_renders(tmp_path) -> None:
    curves = {
        "fixed_spread": analytics.pnl_frame([0.0, 1.0, -2.0, 4.0], [0, 1, 2, -1]),
        "inventory_aware": analytics.pnl_frame([0.0, 0.5, 0.25, 1.0], [0, 1, 0, -1]),
    }
    output = plots.plot_pnl_and_inventory(curves, tmp_path / "pnl.png")
    assert output.exists()
    assert _png_header(output) == PNG_MAGIC


def test_capture_microstructure_figure_renders(tmp_path) -> None:
    top_of_book = pd.DataFrame(
        {
            "sequence": [0, 1, 2, 3],
            "spread_ticks": [1, 1, 3, 1],
            "depth_imbalance": [0.1, -0.4, 0.6, -0.2],
            "latency_ms": [70.5, 82.0, 91.25, 78.0],
        }
    )
    output = plots.plot_capture_microstructure(top_of_book, tmp_path / "capture.png")
    assert output.exists()
    assert _png_header(output) == PNG_MAGIC
