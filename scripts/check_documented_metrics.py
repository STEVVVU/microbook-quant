#!/usr/bin/env python
"""Fail if a replay result disagrees with the numbers documented in the README.

The README quotes specific measured figures from the committed capture. Those are
credibility-critical: a stale number is worse than no number. This script is the
mechanical guard -- CI replays the capture and then checks the result against the
table below, so a change that moves a documented figure breaks the build instead
of silently rotting the README.

If a change *legitimately* moves one of these, update both this file and the
README in the same commit.

Usage:
    python scripts/check_documented_metrics.py <replay_result.json>
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


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    result = json.loads(Path(argv[0]).read_text(encoding="utf-8"))

    failures: list[str] = []

    actual_sha = result["capture"]["sha256"]
    if actual_sha != EXPECTED_SHA256:
        failures.append(f"capture.sha256: expected {EXPECTED_SHA256}, got {actual_sha}")
    actual_duration = result["capture"]["duration_seconds"]
    if not math.isclose(actual_duration, EXPECTED_DURATION_SECONDS, abs_tol=1e-9):
        failures.append(
            f"capture.duration_seconds: expected {EXPECTED_DURATION_SECONDS}, got {actual_duration}"
        )

    for section, metric, expected, tolerance in EXPECTED:
        actual = result[section][metric]
        if tolerance == 0:
            if actual != expected:
                failures.append(f"{section}.{metric}: expected {expected}, got {actual}")
        elif not math.isclose(actual, expected, abs_tol=tolerance, rel_tol=0.0):
            failures.append(
                f"{section}.{metric}: expected {expected} (+/-{tolerance}), got {actual}"
            )

    if failures:
        print("Documented metrics no longer match the replay result:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        print(
            "\nIf this change is intended, update README.md AND "
            "scripts/check_documented_metrics.py together.",
            file=sys.stderr,
        )
        return 1

    print(f"All {len(EXPECTED) + 2} documented metrics match the replay result.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
