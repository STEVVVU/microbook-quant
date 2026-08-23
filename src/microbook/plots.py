"""Matplotlib figures for run and capture diagnostics.

Plotting is strictly downstream of the integer-tick engine: everything here
consumes the pandas frames built in :mod:`microbook.analytics`. Uses the
non-interactive ``Agg`` backend so figures render identically headless in CI.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (must follow the backend selection)
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

_GRID = {"alpha": 0.25, "linewidth": 0.6}


def _finish(figure: plt.Figure, output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.tight_layout()
    figure.savefig(path, dpi=140)
    plt.close(figure)
    return path


def plot_queue_position_fill_probability(
    summary: pd.DataFrame, output: str | Path, *, title: str | None = None
) -> Path:
    """Fill probability and post-fill markout versus queue position.

    The core microstructure result: quotes that join deeper in the FIFO queue
    fill less often, and the deep-queue fills that *do* happen are the ones
    where the level got swept -- so they carry worse markouts.
    """
    figure, (top, bottom) = plt.subplots(2, 1, figsize=(8.0, 6.4), sharex=True)
    buckets = summary["queue_bucket"].astype(str).to_numpy()
    positions = np.arange(len(buckets))

    probabilities = summary["fill_probability"].to_numpy(dtype=np.float64)
    top.bar(positions, probabilities, color="#2b6cb0", width=0.68)
    top.set_ylabel("fill probability")
    # Headroom above 1.0 so the n= annotation on a 100%-fill bucket does not
    # collide with the title.
    top.set_ylim(0.0, 1.12)
    top.set_title(title or "Queue position vs fill probability and adverse selection")
    top.grid(axis="y", **_GRID)
    for position, probability, orders in zip(
        positions, probabilities, summary["orders"].to_numpy(), strict=True
    ):
        if np.isfinite(probability):
            top.annotate(
                f"n={int(orders)}",
                (position, probability),
                textcoords="offset points",
                xytext=(0, 4),
                ha="center",
                fontsize=8,
                color="#4a5568",
            )

    markouts = summary["mean_markout_ticks"].to_numpy(dtype=np.float64)
    colors = ["#c53030" if value < 0 else "#2f855a" for value in markouts]
    bottom.bar(positions, markouts, color=colors, width=0.68)
    bottom.axhline(0.0, color="#1a202c", linewidth=0.8)
    bottom.set_ylabel("mean 20-event markout (ticks)")
    bottom.set_xlabel("lots resting ahead in queue at submit time")
    bottom.set_xticks(positions)
    bottom.set_xticklabels(buckets)
    bottom.grid(axis="y", **_GRID)
    bottom.annotate(
        "negative = midpoint moved against the maker (adverse selection)",
        (0.01, 0.03),
        xycoords="axes fraction",
        fontsize=8,
        color="#4a5568",
    )
    return _finish(figure, output)


def plot_pnl_and_inventory(
    curves: dict[str, pd.DataFrame], output: str | Path, *, title: str | None = None
) -> Path:
    """Marked PnL, drawdown and inventory paths for each strategy."""
    figure, axes = plt.subplots(3, 1, figsize=(9.0, 7.6), sharex=True)
    pnl_axis, drawdown_axis, inventory_axis = axes
    palette = {"fixed_spread": "#2b6cb0", "inventory_aware": "#b7791f"}

    for name, frame in curves.items():
        color = palette.get(name, None)
        events = frame["event"].to_numpy()
        pnl_axis.plot(events, frame["pnl_ticks"], label=name, color=color, linewidth=1.0)
        drawdown_axis.plot(events, frame["drawdown_ticks"], color=color, linewidth=1.0)
        inventory_axis.plot(events, frame["inventory_lots"], color=color, linewidth=0.9)

    pnl_axis.set_ylabel("marked PnL (ticks)")
    pnl_axis.set_title(title or "Simulated marked PnL, drawdown and inventory")
    pnl_axis.legend(loc="upper left", fontsize=9)
    pnl_axis.grid(**_GRID)
    drawdown_axis.set_ylabel("drawdown (ticks)")
    drawdown_axis.grid(**_GRID)
    inventory_axis.axhline(0.0, color="#1a202c", linewidth=0.8)
    inventory_axis.set_ylabel("inventory (lots)")
    inventory_axis.set_xlabel("event")
    inventory_axis.grid(**_GRID)
    figure.text(
        0.01,
        0.005,
        "Simulated on a synthetic path. Not live performance.",
        fontsize=8,
        color="#4a5568",
    )
    return _finish(figure, output)


def plot_capture_microstructure(
    top_of_book: pd.DataFrame, output: str | Path, *, title: str | None = None
) -> Path:
    """Spread, depth imbalance and receive-latency histogram for a real capture."""
    figure, axes = plt.subplots(3, 1, figsize=(9.0, 7.6))
    spread_axis, imbalance_axis, latency_axis = axes
    sequence = top_of_book["sequence"].to_numpy()

    spread_axis.step(
        sequence, top_of_book["spread_ticks"], where="post", color="#2b6cb0", linewidth=0.9
    )
    spread_axis.set_ylabel("spread (ticks)")
    spread_axis.set_yscale("log")
    spread_axis.set_title(title or "Kraken BTC/USD depth-10 capture microstructure")
    spread_axis.grid(**_GRID)

    imbalance_axis.plot(
        sequence, top_of_book["depth_imbalance"], color="#805ad5", linewidth=0.9
    )
    imbalance_axis.axhline(0.0, color="#1a202c", linewidth=0.8)
    imbalance_axis.set_ylabel("depth imbalance")
    imbalance_axis.set_ylim(-1.05, 1.05)
    imbalance_axis.set_xlabel("book event sequence")
    imbalance_axis.grid(**_GRID)

    latency_axis.hist(
        top_of_book["latency_ms"].to_numpy(dtype=np.float64),
        bins=40,
        color="#2f855a",
        edgecolor="white",
        linewidth=0.4,
    )
    latency_axis.set_ylabel("book events")
    latency_axis.set_xlabel("exchange-to-receipt latency (ms)")
    latency_axis.grid(axis="y", **_GRID)
    return _finish(figure, output)
