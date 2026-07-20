"""Reproducible strategy comparison."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .simulator import MarketSimulator, SimulationConfig
from .strategy import FixedSpread, InventoryAware


@dataclass(frozen=True)
class ExperimentConfig:
    events: int = 30_000
    seed: int = 31
    quote_interval: int = 20
    latency: int = 3


def run_comparison(
    config: ExperimentConfig = ExperimentConfig(), *, output_dir: str | Path | None = None
) -> dict[str, object]:
    simulation = SimulationConfig(**asdict(config))
    fixed = MarketSimulator(FixedSpread(), simulation).run()
    inventory_aware = MarketSimulator(InventoryAware(), simulation).run()
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
    if output_dir is not None:
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "comparison.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return result
