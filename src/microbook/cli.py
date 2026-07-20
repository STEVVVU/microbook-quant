from __future__ import annotations

import argparse
import json

from .experiment import ExperimentConfig, run_comparison


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run queue-aware market-making experiments")
    parser.add_argument("--events", type=int, default=30_000)
    parser.add_argument("--seed", type=int, default=31)
    parser.add_argument("--quote-interval", type=int, default=20)
    parser.add_argument("--latency", type=int, default=3)
    parser.add_argument("--output", default="artifacts")
    args = parser.parse_args(argv)
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
    return 0
