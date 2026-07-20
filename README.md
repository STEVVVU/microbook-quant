# MicroBook Quant

A queue- and latency-aware market-making research environment built around a
real price-time-priority matching engine.

Most portfolio backtests assume a fill when a bar touches a price. MicroBook
models the harder questions: how much quantity was ahead in the queue, whether
a cancel arrived in time, which resting order was the maker, what inventory was
carried, and whether a fill was followed by adverse price movement.

## What it demonstrates

- **Exchange mechanics:** integer ticks, FIFO queues, partial fills, market
  orders, owner-checked cancellation, and post-only rejection.
- **Discrete-event simulation:** agent actions arrive after configurable
  latency and can only trade against book state at arrival time.
- **Synthetic microstructure:** a latent fundamental, mildly informed noise
  flow, limit-order replenishment, cancellation, and endogenous top-of-book.
- **Market-making research:** a fixed-spread baseline versus an
  Avellaneda-Stoikov-inspired reservation-price strategy.
- **Execution analytics:** maker rebates, 20-event markouts, fill quantity,
  quote acceptance, inventory RMS, peak inventory, drawdown, and marked PnL.
- **Reproducibility:** seeded order flow, no external data, exact invariant
  checks every 1,000 events, tests, and CI.

## Resume highlights

- Built a deterministic price-time-priority matching engine supporting FIFO
  queues, partial fills, market orders, post-only rejection, and owner-checked
  cancellation.
- Developed a latency- and queue-aware market-making simulator with synthetic
  order flow, maker rebates, markouts, inventory accounting, and paired
  strategy evaluation.
- Implemented an inventory-aware quoting policy that reduced peak inventory by
  **61.9%** and maximum drawdown by **76.6%** over a 30,000-event benchmark.

## Event path

```text
latent fundamental ----> external limit / market order flow
                                  |
                                  v
                       price-time-priority book
                                  |
                    fills + queue-position outcome
                                  |
         +------------------------+-----------------------+
         |                                                |
 inventory / cash / markout                    delayed quote actions
         |                                                ^
         +----> strategy observes midpoint + volatility --+
                         configurable latency
```

## Canonical experiment

Python 3.11, 30,000 events, seed 31, quote refresh every 20 events,
three-event action latency.

| Metric | Fixed spread | Inventory aware |
| --- | ---: | ---: |
| Final marked PnL (ticks) | 848.64 | 99.12 |
| Maximum drawdown (ticks) | 531.98 | **124.47** |
| Peak absolute inventory | 21 | **8** |
| RMS inventory | 12.43 | **4.65** |
| Ending inventory | -15 | **-6** |
| Maker fills | 48 | 42 |

The inventory-aware policy reduces peak inventory by **61.9%** and maximum
drawdown by **76.6%**. It does not dominate the baseline: the fixed-spread
policy earns more on this path while carrying materially larger directional
exposure. That is the point of reporting marked PnL and risk together instead
of selecting only a favorable return statistic.

The checked-in [`artifacts/comparison.json`](artifacts/comparison.json) contains
the full result, including quote acceptance and post-fill markouts.

## Run it

No runtime dependencies are required.

```powershell
$env:PYTHONPATH = "src"
python -m microbook --events 30000 --seed 31 --output artifacts
```

For development:

```powershell
python -m pip install -e ".[dev]"
pytest
ruff check .
```

## Repository map

```text
src/microbook/
  book.py        matching engine and invariants
  simulator.py   exchange event loop, latency, accounting, markouts
  strategy.py    fixed and inventory-aware quote policies
  experiment.py  reproducible paired comparison
tests/            FIFO, partial fill, cancellation, latency, and risk tests
```

## Modeling choices

Prices are integer ticks and quantities are integer lots. The simulator uses a
latent fundamental only to bias public order flow; the strategy cannot observe
it. Both strategies receive the same seeded event process. Maker PnL includes a
small per-lot rebate and is marked to the contemporaneous midpoint.

The `pnl_path_ratio_100_event` field is
`sqrt(N) * mean(increment) / std(increment)` on 100-event marked-PnL
increments. It is a path-comparison statistic, not an annualized Sharpe ratio.

## Research context

The project direction is informed by the queue-position and latency realism in
[hftbacktest](https://github.com/nkaz001/hftbacktest) and by classic
inventory-aware market-making models. The implementation here is standalone,
small enough to audit, and deliberately exposes the exchange mechanics rather
than hiding them behind a backtesting framework.
