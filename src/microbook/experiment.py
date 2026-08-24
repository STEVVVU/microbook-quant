"""Reproducible strategy comparison."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from . import analytics, plots
from .simulator import MarketSimulator, SimulationConfig, SimulationResult
from .strategy import FixedSpread, InventoryAware


@dataclass(frozen=True)
class ExperimentConfig:
    events: int = 30_000
    seed: int = 31
    quote_interval: int = 20
    latency: int = 3


def _write_parquet(frame: pa.Table | object, path: Path) -> None:
    table = (
        frame
        if isinstance(frame, pa.Table)
        else pa.Table.from_pandas(frame, preserve_index=False)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")


def run_comparison(
    config: ExperimentConfig = ExperimentConfig(),
    *,
    output_dir: str | Path | None = None,
    write_artifacts: bool = False,
) -> dict[str, object]:
    """Run both policies on the same seeded path and compare them.

    ``write_artifacts`` additionally emits the vectorized analytics tables as
    parquet plus matplotlib figures. It is off by default so the plain
    ``run_comparison`` call used by tests stays dependency-light and fast.
    """
    simulation = SimulationConfig(**asdict(config))
    runs: dict[str, SimulationResult] = {
        "fixed_spread": MarketSimulator(FixedSpread(), simulation).run_detailed(),
        "inventory_aware": MarketSimulator(InventoryAware(), simulation).run_detailed(),
    }
    fixed = runs["fixed_spread"].metrics
    inventory_aware = runs["inventory_aware"].metrics
    result: dict[str, object] = {
        "config": asdict(config),
        "fixed_spread": fixed,
        "inventory_aware": inventory_aware,
        "inventory_risk_reduction": 1.0
        - float(inventory_aware["max_abs_inventory"]) / max(float(fixed["max_abs_inventory"]), 1.0),
        "drawdown_reduction": 1.0
        - float(inventory_aware["max_drawdown_ticks"])
        / max(float(fixed["max_drawdown_ticks"]), 1e-12),
    }

    # Vectorized queue-position / adverse-selection summary per strategy. This is
    # the analysis-side counterpart to the integer-tick event loop: the loop
    # recorded per-quote facts, pandas reduces them.
    queue_summaries: dict[str, object] = {}
    for name, run in runs.items():
        quotes = analytics.quote_frame(run.quotes)
        markouts = analytics.markout_frame(run.markouts)
        summary = analytics.queue_position_outcomes(quotes, markouts)
        queue_summaries[name] = summary
        result.setdefault("queue_position", {})
        result["queue_position"][name] = [  # type: ignore[index]
            {
                "queue_bucket": str(row.queue_bucket),
                "orders": int(row.orders),
                "filled_orders": int(row.filled_orders),
                "filled_lots": int(row.filled_lots),
                "fill_probability": None
                if row.fill_probability != row.fill_probability
                else float(row.fill_probability),
                "mean_markout_ticks": float(row.mean_markout_ticks),
            }
            for row in summary.itertuples()
        ]

    if output_dir is not None:
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "comparison.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if write_artifacts:
            curves = {
                name: analytics.pnl_frame(run.pnl_curve, run.inventory_curve)
                for name, run in runs.items()
            }
            for name, run in runs.items():
                _write_parquet(
                    analytics.quote_frame(run.quotes).drop(columns=["queue_bucket"]),
                    destination / f"quotes_{name}.parquet",
                )
                _write_parquet(
                    analytics.markout_frame(run.markouts),
                    destination / f"markouts_{name}.parquet",
                )
            plots.plot_queue_position_fill_probability(
                queue_summaries["inventory_aware"],  # type: ignore[arg-type]
                destination / "queue_position_fill_probability.png",
                title="Inventory-aware maker: queue position vs fill probability and markout",
            )
            plots.plot_pnl_and_inventory(
                curves, destination / "pnl_inventory.png"
            )
    return result
