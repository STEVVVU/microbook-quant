# MicroBook Quant

A queue- and latency-aware market-making research environment built around a
price-time-priority matching engine and checksum-verified real exchange data.

Most portfolio backtests assume a fill when a bar touches a price. MicroBook
models the harder questions: how much quantity was ahead in the queue, whether
a cancel arrived in time, which resting order was the maker, what inventory was
carried, and whether a fill was followed by adverse price movement.

## What it demonstrates

- **Exchange mechanics:** integer ticks, FIFO queues, partial fills, market
  orders, owner-checked cancellation, and post-only rejection.
- **Real market data:** public Kraken v2 BTC/USD snapshots, updates, and trades
  are normalized to integer ticks and recorded atomically as gzip JSONL.
- **Feed integrity:** every snapshot and update is reconstructed and checked
  against Kraken's top-ten-level CRC32 before it is accepted.
- **Discrete-event simulation:** agent actions arrive after configurable
  latency and can only trade against book state at arrival time.
- **Synthetic microstructure:** a latent fundamental, mildly informed noise
  flow, limit-order replenishment, cancellation, and endogenous top-of-book.
- **Market-making research:** a fixed-spread baseline versus an
  Avellaneda-Stoikov-inspired reservation-price strategy.
- **Execution analytics:** maker rebates, 20-event markouts, fill quantity,
  quote acceptance, inventory RMS, peak inventory, drawdown, and marked PnL.
- **Reproducibility:** deterministic replay revalidates checksums, book
  invariants, latency, spread, depth imbalance, and trade statistics.

## Resume highlights

- Built a deterministic price-time-priority matching engine supporting FIFO
  queues, partial fills, market orders, post-only rejection, and owner-checked
  cancellation.
- Engineered a public Kraken v2 feed handler that recorded and replayed **1,829
  real BTC/USD L2/trade events**, verified **1,807 exchange CRC32s**, and
  observed zero crossed-book states.
- Developed latency- and queue-aware market-making research that reduced peak
  inventory by **61.9%** and simulated maximum drawdown by **76.6%** over a
  paired 30,000-event experiment.

## Real-feed evidence

The checked-in capture contains 89.45 seconds of public BTC/USD depth-10 and
trade data recorded on July 16, 2026.

| Integrity / market metric | Result |
| --- | ---: |
| Normalized events | 1,829 |
| L2 updates | 1,806 |
| Trades | 22 |
| Exchange CRC32s verified | **1,807** |
| Crossed-book states | **0** |
| Update throughput | 20.19 events/sec |
| Spread, p50 / p95 | 1 / 1 tick |
| Receive latency, p50 / p95 | 79.30 / 94.52 ms |

[`artifacts/kraken_l2_sample.jsonl.gz`](artifacts/kraken_l2_sample.jsonl.gz)
is identified by SHA-256
`97bd818113f75a9d44517969d64c72e08ba0bacb3a749f420e8fbdbf6cbf979e`.
[`artifacts/kraken_replay.json`](artifacts/kraken_replay.json) is regenerated
from that capture and does not trust stored summary values.

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

## Simulated strategy experiment

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

The inventory-aware policy reduces peak inventory by **61.9%** and simulated
maximum drawdown by **76.6%**. It does not dominate the baseline: the
fixed-spread policy earns more on this synthetic path while carrying materially
larger directional exposure. No simulated PnL is presented as live performance.

The checked-in [`artifacts/comparison.json`](artifacts/comparison.json) contains
the full result, including quote acceptance and post-fill markouts.

## Run it

Synthetic simulation and replay have no runtime dependencies.

```powershell
$env:PYTHONPATH = "src"
python -m microbook replay artifacts/kraken_l2_sample.jsonl.gz
python -m microbook --events 30000 --seed 31 --output artifacts
```

To record a fresh public sample:

```powershell
python -m pip install -e ".[live]"
microbook capture --symbol "BTC/USD" --depth 10 --seconds 90
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
  feeds/
    kraken.py    normalized events and CRC32 book reconstruction
    kraken_recorder.py atomic public WebSocket recorder
    coinbase.py  authenticated Coinbase L2 normalization
  replay.py      deterministic integrity and microstructure analytics
  simulator.py   exchange event loop, latency, accounting, markouts
  strategy.py    fixed and inventory-aware quote policies
  experiment.py  reproducible paired comparison
tests/            FIFO, partial fill, cancellation, latency, and risk tests
```

## Modeling choices

Real feed prices are converted to exact integer ticks while decimal quantity
strings are retained for exchange checksum reproduction. The synthetic
simulator uses integer prices and lots. Its latent fundamental biases public
order flow but is never visible to either strategy.

The `pnl_path_ratio_100_event` field is
`sqrt(N) * mean(increment) / std(increment)` on 100-event marked-PnL
increments. It is a path-comparison statistic, not an annualized Sharpe ratio.

## Research context

The project direction is informed by the queue-position and latency realism in
[hftbacktest](https://github.com/nkaz001/hftbacktest) and by classic
inventory-aware market-making models. The implementation here is standalone,
small enough to audit, and deliberately exposes the exchange mechanics rather
than hiding them behind a backtesting framework.
