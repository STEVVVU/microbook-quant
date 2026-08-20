"""Vectorized post-trade and microstructure analytics.

Design boundary -- read this before "optimizing" anything
=========================================================

``book.py`` and the ``simulator.py`` event loop are deliberately **scalar and
integer**.  Prices are integer ticks, sizes are integer lots, and every event is
sequenced one at a time.  That is not a performance oversight:

* price-time priority, queue position and partial fills are *order dependent*,
  so there is no correct way to vectorize them across events;
* float prices make tick comparisons ("is this order at the touch?", "would this
  post-only quote cross?") ambiguous, and a 1-ulp error there silently changes
  which order is the maker.

This module is the other side of that boundary.  It consumes what a completed
run or a verified capture *recorded* -- PnL curves, fill records, per-event book
observations -- and reduces them with numpy/pandas, which is where vectorization
actually pays and where a reviewer expects to see it.

Numeric discipline across the boundary
--------------------------------------

Values arrive already in tick units (integers, or exact halves for midpoints).
Where money is *summed* we use :func:`math.fsum`, which is exactly rounded and
therefore order independent, instead of a pairwise numpy reduction whose result
depends on how the array is chunked.  ``np.maximum.accumulate`` and ``np.diff``
are used freely because they perform selection and a single subtraction per
element, so they are bitwise identical to the equivalent scalar loop.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

import numpy as np
import pandas as pd

# Queue-ahead buckets, in lots resting in front of an agent quote at submit
# time. Deliberately coarse and left-closed so the "joined an empty level"
# case (zero lots ahead) is its own bucket -- that is the interesting one.
QUEUE_BUCKET_EDGES: tuple[float, ...] = (0.0, 1.0, 3.0, 6.0, 12.0, 25.0, math.inf)
QUEUE_BUCKET_LABELS: tuple[str, ...] = ("0", "1-2", "3-5", "6-11", "12-24", "25+")


def fsum_mean(values: Iterable[float]) -> float:
    """Order-independent mean.

    Used wherever the quantity is money (ticks of PnL, markout, rebate) so an
    aggregate never depends on reduction order or array chunking.
    """
    array = np.asarray(list(values), dtype=np.float64)
    if array.size == 0:
        return 0.0
    return math.fsum(array.tolist()) / array.size


def population_std(values: Sequence[float]) -> float:
    """Population standard deviation with an ``fsum``-stabilised two-pass mean."""
    array = np.asarray(list(values), dtype=np.float64)
    if array.size < 2:
        return 0.0
    mean = math.fsum(array.tolist()) / array.size
    deviations = array - mean
    return math.sqrt(math.fsum((deviations * deviations).tolist()) / array.size)


def percentile(values: Sequence[float], fraction: float) -> float:
    """Linear-interpolated percentile, empty-safe.

    ``np.quantile(..., method="linear")`` is the same estimator the stdlib
    implementation this replaced used: position ``f * (n - 1)`` into the sorted
    sample with linear interpolation between neighbours.
    """
    array = np.asarray(list(values), dtype=np.float64)
    if array.size == 0:
        return 0.0
    return float(np.quantile(array, fraction, method="linear"))


def pnl_frame(pnl_curve: Sequence[float], inventory_curve: Sequence[int]) -> pd.DataFrame:
    """Per-event marked PnL, inventory and running drawdown.

    ``np.maximum.accumulate`` reproduces the scalar running-peak loop exactly
    (it selects, it does not accumulate arithmetic error), and the drawdown is a
    single subtraction per element, so this frame is bitwise identical to the
    event-by-event computation it replaced.
    """
    pnl = np.asarray(list(pnl_curve), dtype=np.float64)
    inventory = np.asarray(list(inventory_curve), dtype=np.int64)
    running_peak = np.maximum.accumulate(pnl) if pnl.size else pnl
    return pd.DataFrame(
        {
            "event": np.arange(pnl.size, dtype=np.int64),
            "pnl_ticks": pnl,
            "inventory_lots": inventory,
            "running_peak_ticks": running_peak,
            "drawdown_ticks": running_peak - pnl,
        }
    )


def max_drawdown_ticks(curve: pd.DataFrame | Sequence[float]) -> float:
    """Largest peak-to-trough decline in marked PnL, in ticks."""
    if isinstance(curve, pd.DataFrame):
        drawdowns = curve["drawdown_ticks"].to_numpy(dtype=np.float64)
    else:
        pnl = np.asarray(list(curve), dtype=np.float64)
        drawdowns = np.maximum.accumulate(pnl) - pnl if pnl.size else pnl
    if drawdowns.size == 0:
        return 0.0
    return float(max(0.0, drawdowns.max()))


def inventory_rms(inventory_curve: Sequence[int]) -> float:
    """Root-mean-square inventory in lots.

    Inventory is integral, so the sum of squares is computed in exact int64 --
    no float rounding enters before the final divide.
    """
    inventory = np.asarray(list(inventory_curve), dtype=np.int64)
    if inventory.size == 0:
        return 0.0
    return math.sqrt(int(inventory @ inventory) / inventory.size)


def path_ratio(pnl_curve: Sequence[float], *, stride: int = 100) -> float:
    """``sqrt(N) * mean(increment) / std(increment)`` on strided PnL increments.

    A path-comparison statistic, not an annualized Sharpe ratio.
    """
    sampled = np.asarray(list(pnl_curve), dtype=np.float64)[::stride]
    increments = np.diff(sampled)
    deviation = population_std(increments)
    if deviation <= 0.0:
        return 0.0
    return fsum_mean(increments) / deviation * math.sqrt(increments.size)


def quote_frame(records: Iterable[object]) -> pd.DataFrame:
    """One row per agent quote that rested on the book.

    Expects :class:`microbook.simulator.QuoteRecord` instances.  ``queue_ahead_lots``
    is the quantity that was already resting in front of the quote in its FIFO
    queue at submit time -- the microstructure quantity that actually determines
    whether a passive order trades.
    """
    rows = [
        {
            "order_id": record.order_id,
            "side": record.side,
            "price_ticks": record.price_ticks,
            "submitted_event": record.submitted_event,
            "queue_ahead_lots": record.queue_ahead_lots,
            "size_lots": record.size_lots,
            "filled_lots": record.filled_lots,
            "cancelled": record.cancelled,
        }
        for record in records
    ]
    frame = pd.DataFrame(
        rows,
        columns=[
            "order_id",
            "side",
            "price_ticks",
            "submitted_event",
            "queue_ahead_lots",
            "size_lots",
            "filled_lots",
            "cancelled",
        ],
    )
    if frame.empty:
        frame = frame.astype(
            {
                "order_id": "int64",
                "side": "object",
                "price_ticks": "int64",
                "submitted_event": "int64",
                "queue_ahead_lots": "int64",
                "size_lots": "int64",
                "filled_lots": "int64",
                "cancelled": "bool",
            }
        )
    frame["filled"] = frame["filled_lots"] > 0
    frame["fully_filled"] = frame["filled_lots"] >= frame["size_lots"]
    frame["queue_bucket"] = pd.cut(
        frame["queue_ahead_lots"],
        bins=list(QUEUE_BUCKET_EDGES),
        labels=list(QUEUE_BUCKET_LABELS),
        right=False,
    )
    return frame


def markout_frame(records: Iterable[object]) -> pd.DataFrame:
    """One row per resolved markout.

    Expects :class:`microbook.simulator.MarkoutRecord` instances.  A negative
    markout means the midpoint moved against the maker after the fill, i.e.
    adverse selection.
    """
    rows = [
        {
            "order_id": record.order_id,
            "side": record.side,
            "fill_price_ticks": record.fill_price_ticks,
            "fill_event": record.fill_event,
            "markout_ticks": record.markout_ticks,
        }
        for record in records
    ]
    return pd.DataFrame(
        rows,
        columns=["order_id", "side", "fill_price_ticks", "fill_event", "markout_ticks"],
    )


def queue_position_outcomes(
    quotes: pd.DataFrame, markouts: pd.DataFrame | None = None
) -> pd.DataFrame:
    """Fill probability and post-fill markout as a function of queue position.

    This is the join a market maker actually cares about: being deep in the
    queue lowers fill probability, and the fills you *do* get from deep in the
    queue are the ones where the level was swept, so they are the adversely
    selected ones.
    """
    frame = quotes
    if markouts is not None and not markouts.empty:
        per_order = (
            markouts.groupby("order_id", observed=True)["markout_ticks"]
            .agg(lambda values: fsum_mean(values))
            .rename("markout_ticks")
        )
        frame = quotes.merge(per_order, on="order_id", how="left")
    else:
        frame = quotes.assign(markout_ticks=np.nan)

    grouped = frame.groupby("queue_bucket", observed=False)
    summary = pd.DataFrame(
        {
            "orders": grouped.size(),
            "filled_orders": grouped["filled"].sum(),
            "filled_lots": grouped["filled_lots"].sum(),
            "mean_queue_ahead_lots": grouped["queue_ahead_lots"].apply(fsum_mean),
            "mean_markout_ticks": grouped["markout_ticks"].apply(
                lambda values: fsum_mean(values.dropna())
            ),
        }
    )
    summary["fill_probability"] = np.where(
        summary["orders"] > 0, summary["filled_orders"] / summary["orders"].clip(lower=1), np.nan
    )
    return summary.reset_index()


def latency_histogram(latencies_ms: Sequence[float], *, bins: int = 30) -> pd.DataFrame:
    """Receive-latency histogram (exchange timestamp to local receipt)."""
    array = np.asarray(list(latencies_ms), dtype=np.float64)
    if array.size == 0:
        return pd.DataFrame(columns=["bin_left_ms", "bin_right_ms", "count"])
    counts, edges = np.histogram(array, bins=bins)
    return pd.DataFrame(
        {"bin_left_ms": edges[:-1], "bin_right_ms": edges[1:], "count": counts}
    )
