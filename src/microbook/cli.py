from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .experiment import ExperimentConfig, run_comparison
from .feeds.kraken_recorder import capture_kraken, summary_json
from .replay import replay_kraken_capture


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in {"simulate", "capture", "replay"}:
        argv.insert(0, "simulate")
    parser = argparse.ArgumentParser(description="Run queue-aware market-making experiments")
    subparsers = parser.add_subparsers(dest="command", required=True)
    simulate = subparsers.add_parser("simulate", help="run the synthetic paired experiment")
    simulate.add_argument("--events", type=int, default=30_000)
    simulate.add_argument("--seed", type=int, default=31)
    simulate.add_argument("--quote-interval", type=int, default=20)
    simulate.add_argument("--latency", type=int, default=3)
    simulate.add_argument("--output", default="artifacts")
    simulate.add_argument(
        "--artifacts",
        action="store_true",
        help="also write parquet analytics tables and matplotlib figures",
    )
    capture = subparsers.add_parser("capture", help="record the public Kraken L2 feed")
    capture.add_argument("--symbol", default="BTC/USD")
    capture.add_argument("--depth", type=int, default=10)
    capture.add_argument("--events", type=int, default=5_000)
    capture.add_argument("--seconds", type=float, default=60.0)
    capture.add_argument("--output", default="captures/kraken-btc-usd.jsonl.gz")
    replay = subparsers.add_parser("replay", help="validate and analyze a Kraken capture")
    replay.add_argument("capture")
    replay.add_argument("--symbol", default="BTC/USD")
    replay.add_argument("--depth", type=int, default=10)
    replay.add_argument("--output", default="artifacts/kraken_replay.json")
    replay.add_argument(
        "--parquet-dir",
        default=None,
        help="also re-encode the capture to columnar parquet in this directory",
    )
    replay.add_argument(
        "--figure",
        default=None,
        help="also write a microstructure figure (spread, imbalance, latency) to this path",
    )
    args = parser.parse_args(argv)

    if args.command == "simulate":
        result = run_comparison(
            ExperimentConfig(
                events=args.events,
                seed=args.seed,
                quote_interval=args.quote_interval,
                latency=args.latency,
            ),
            output_dir=args.output,
            write_artifacts=args.artifacts,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "capture":
        result = asyncio.run(
            capture_kraken(
                args.output,
                symbol=args.symbol,
                depth=args.depth,
                max_events=args.events,
                duration_seconds=args.seconds,
            )
        )
        print(summary_json(result), end="")
    else:
        result = replay_kraken_capture(
            args.capture,
            symbol=args.symbol,
            depth=args.depth,
            output=args.output,
            parquet_dir=args.parquet_dir,
        )
        if args.figure is not None:
            if args.parquet_dir is None:
                parser.error("--figure requires --parquet-dir")
            import pandas as pd

            from .plots import plot_capture_microstructure

            top_of_book = pd.read_parquet(result["parquet"]["top_of_book"])
            plot_capture_microstructure(top_of_book, args.figure)
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0
