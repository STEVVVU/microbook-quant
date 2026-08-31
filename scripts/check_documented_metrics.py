#!/usr/bin/env python
"""Fail if a pipeline result disagrees with the numbers documented in the README.

The README quotes specific measured figures from the committed capture and from
the seeded strategy comparison. Those are credibility-critical: a stale number is
worse than no number. This script is the mechanical guard -- CI replays the
capture, re-runs the comparison, and then checks both results against the tables
below, so a change that moves a documented figure breaks the build instead of
silently rotting the README.

If a change *legitimately* moves one of these, update both this file and the
README in the same commit.

Usage:
    python scripts/check_documented_metrics.py <replay_result.json> [comparison.json]

The comparison argument is optional so a bare replay can still be checked on its
own, but CI passes both.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

# (section, metric, expected, absolute tolerance)
#
# Integrity counts are exact -- they are event counts and CRC32 verifications, so
# any drift is a real regression. Statistics carry a small tolerance: they are
# reduced with math.fsum, which is exactly rounded but rounds once more than
# statistics.mean's exact-rational path, so a 1-ulp difference is expected and
# acceptable for a non-monetary statistic. See the README "Numerics" section.
EXPECTED: tuple[tuple[str, str, float, float], ...] = (
    ("integrity", "events", 1_829, 0),
    ("integrity", "snapshots", 1, 0),
    ("integrity", "updates", 1_806, 0),
    ("integrity", "trades", 22, 0),
    ("integrity", "checksums_verified", 1_807, 0),
    ("integrity", "crossed_book_states", 0, 0),
    ("market", "updates_per_second", 20.18908899532869, 1e-9),
    ("market", "top_of_book_changes", 12, 0),
    ("market", "spread_ticks_mean", 2.1394576646375207, 1e-9),
    ("market", "spread_ticks_p50", 1.0, 0),
    ("market", "spread_ticks_p95", 1.0, 0),
    ("market", "spread_ticks_max", 117.0, 0),
    ("market", "depth_imbalance_mean", -0.5335132040751038, 1e-12),
    ("market", "midpoint_change_rms_ticks", 2.0416483037572295, 1e-9),
    ("market", "latency_ms_p50", 79.30282, 1e-9),
    ("market", "latency_ms_p95", 94.5170672, 1e-9),
    ("market", "aggressor_buy_trades", 9, 0),
    ("market", "aggressor_sell_trades", 13, 0),
    ("market", "traded_quantity", 0.13212592, 1e-12),
    ("market", "traded_notional_usd", 8533.146950703, 1e-9),
)

EXPECTED_SHA256 = "97bd818113f75a9d44517969d64c72e08ba0bacb3a749f420e8fbdbf6cbf979e"
EXPECTED_DURATION_SECONDS = 89.4542592

# The seeded strategy comparison (artifacts/comparison.json), README "Simulated
# strategy experiment" table. Same contract as EXPECTED above: (section, metric,
# expected, absolute tolerance), where section is a top-level key of the
# comparison result. Counts and integer inventories are exact; tick PnL carries a
# float tolerance because it accumulates rebates in float.
EXPECTED_COMPARISON: tuple[tuple[str, str, float, float], ...] = (
    ("fixed_spread", "final_pnl_ticks", 848.637999999919, 1e-9),
    ("fixed_spread", "max_drawdown_ticks", 531.9840000000404, 1e-9),
    ("fixed_spread", "max_abs_inventory", 21, 0),
    ("fixed_spread", "inventory_rms", 12.432306034414264, 1e-12),
    ("fixed_spread", "ending_inventory", -15, 0),
    ("fixed_spread", "fills", 48, 0),
    ("inventory_aware", "final_pnl_ticks", 99.12400000001799, 1e-9),
    ("inventory_aware", "max_drawdown_ticks", 124.46599999999307, 1e-9),
    ("inventory_aware", "max_abs_inventory", 8, 0),
    ("inventory_aware", "inventory_rms", 4.651408388864603, 1e-12),
    ("inventory_aware", "ending_inventory", -6, 0),
    ("inventory_aware", "fills", 42, 0),
)

# The two resume-headline percentages. These are the most visible numbers in the
# repo, so they are checked three ways: the full-precision ratio the pipeline
# computes, the rounded percent that ratio implies, and the literal string the
# README actually prints. The last check is what catches a README/artifact
# divergence in either direction -- checking the artifact against a constant in
# this file alone would not notice that the prose says something else.
# (metric, expected ratio, tolerance, README percent)
EXPECTED_HEADLINE_PERCENTS: tuple[tuple[str, float, float, float], ...] = (
    ("inventory_risk_reduction", 0.6190476190476191, 1e-12, 61.9),
    ("drawdown_reduction", 0.7660343168215893, 1e-12, 76.6),
)


README = Path(__file__).resolve().parent.parent / "README.md"


def _check_readme_prints(percent: float, failures: list[str]) -> None:
    """Fail unless the README actually prints ``percent`` as a bold percentage.

    The headline percentages appear twice in the README (resume highlights and the
    experiment section). Asserting on the rendered ``**61.9%**`` string keeps the
    prose itself in the guard: editing the artifact and the table above without
    touching the README -- or vice versa -- is then a build failure rather than a
    silent inconsistency.
    """
    if not README.exists():  # running the script outside a checkout
        return
    needle = f"**{percent:.1f}%**"
    occurrences = README.read_text(encoding="utf-8").count(needle)
    if occurrences != 2:
        failures.append(
            f"README.md contains {needle} {occurrences} time(s), expected 2 "
            "(resume highlights + simulated strategy experiment)"
        )


def _check_table(
    result: dict, table: tuple[tuple[str, str, float, float], ...], failures: list[str]
) -> None:
    """Append a failure line for every metric in ``table`` that does not match."""
    for section, metric, expected, tolerance in table:
        actual = result[section][metric]
        if tolerance == 0:
            if actual != expected:
                failures.append(f"{section}.{metric}: expected {expected}, got {actual}")
        elif not math.isclose(actual, expected, abs_tol=tolerance, rel_tol=0.0):
            failures.append(
                f"{section}.{metric}: expected {expected} (+/-{tolerance}), got {actual}"
            )


def main(argv: list[str]) -> int:
    if not 1 <= len(argv) <= 2:
        print(__doc__, file=sys.stderr)
        return 2
    result = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    comparison = (
        json.loads(Path(argv[1]).read_text(encoding="utf-8")) if len(argv) == 2 else None
    )

    failures: list[str] = []

    actual_sha = result["capture"]["sha256"]
    if actual_sha != EXPECTED_SHA256:
        failures.append(f"capture.sha256: expected {EXPECTED_SHA256}, got {actual_sha}")
    actual_duration = result["capture"]["duration_seconds"]
    if not math.isclose(actual_duration, EXPECTED_DURATION_SECONDS, abs_tol=1e-9):
        failures.append(
            f"capture.duration_seconds: expected {EXPECTED_DURATION_SECONDS}, got {actual_duration}"
        )

    _check_table(result, EXPECTED, failures)

    checked = len(EXPECTED) + 2
    if comparison is not None:
        _check_table(comparison, EXPECTED_COMPARISON, failures)
        checked += len(EXPECTED_COMPARISON) + len(EXPECTED_HEADLINE_PERCENTS)
        for metric, expected, tolerance, readme_percent in EXPECTED_HEADLINE_PERCENTS:
            actual = comparison[metric]
            if not math.isclose(actual, expected, abs_tol=tolerance, rel_tol=0.0):
                failures.append(
                    f"{metric}: expected {expected} (+/-{tolerance}), got {actual}"
                )
            # The README prints this as a one-decimal percent, so that is what has
            # to still be true of the measured ratio.
            actual_percent = round(actual * 100.0, 1)
            if actual_percent != readme_percent:
                failures.append(
                    f"{metric}: README says {readme_percent}%, "
                    f"pipeline now measures {actual_percent}%"
                )
            _check_readme_prints(readme_percent, failures)

    if failures:
        print("Documented metrics no longer match the pipeline result:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        print(
            "\nIf this change is intended, update README.md AND "
            "scripts/check_documented_metrics.py together.",
            file=sys.stderr,
        )
        return 1

    scope = "replay + comparison results" if comparison is not None else "replay result"
    print(f"All {checked} documented metrics match the {scope}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
