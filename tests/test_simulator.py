from microbook.experiment import ExperimentConfig, run_comparison
from microbook.simulator import MarketSimulator, SimulationConfig
from microbook.strategy import FixedSpread


def test_simulation_is_deterministic() -> None:
    config = SimulationConfig(events=2_000, seed=5)
    first = MarketSimulator(FixedSpread(), config).run()
    second = MarketSimulator(FixedSpread(), config).run()
    assert first == second


def test_inventory_aware_strategy_reduces_peak_inventory() -> None:
    result = run_comparison(ExperimentConfig(events=8_000, seed=2))
    assert (
        result["inventory_aware"]["max_abs_inventory"]
        < result["fixed_spread"]["max_abs_inventory"]
    )


def test_quotes_do_not_arrive_before_configured_latency() -> None:
    result = MarketSimulator(
        FixedSpread(),
        SimulationConfig(events=3, quote_interval=1, latency=3, seed=2),
    ).run()
    assert result["quotes_sent"] == 0
