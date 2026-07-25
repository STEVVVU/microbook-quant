from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .experiment import ExperimentConfig, run_comparison
from .feeds.recorder import capture_coinbase, summary_json


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in {"simulate", "capture"}:
        argv.insert(0, "simulate")
    parser = argparse.ArgumentParser(description="Run queue-aware market-making experiments")
    subparsers = parser.add_subparsers(dest="command", required=True)
    simulate = subparsers.add_parser("simulate", help="run the synthetic paired experiment")
    simulate.add_argument("--events", type=int, default=30_000)
    simulate.add_argument("--seed", type=int, default=31)
    simulate.add_argument("--quote-interval", type=int, default=20)
    simulate.add_argument("--latency", type=int, default=3)
    simulate.add_argument("--output", default="artifacts")
    capture = subparsers.add_parser("capture", help="record the public Coinbase L2 feed")
    capture.add_argument("--product", default="BTC-USD")
    capture.add_argument("--events", type=int, default=5_000)
    capture.add_argument("--seconds", type=float, default=60.0)
    capture.add_argument("--output", default="captures/coinbase-btc-usd.jsonl.gz")
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
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        result = asyncio.run(
            capture_coinbase(
                args.output,
                product_id=args.product,
                max_events=args.events,
                duration_seconds=args.seconds,
            )
        )
        print(summary_json(result), end="")
    return 0
