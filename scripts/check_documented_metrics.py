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
import re
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

# The queue-position series quoted in the README: the resume bullet and the
# "Queue position drives fills and adverse selection" table both read the
# inventory_aware policy, so that is the series named in the prose and the one
# checked here. The fixed_spread series is deliberately NOT the documented one --
# its buckets differ (its front bucket is 0.8, not 1.00), which is exactly why the
# prose has to say which series it is quoting.
QUEUE_POSITION_SERIES = "inventory_aware"

# (queue_bucket, orders, filled_orders, fill_probability, mean_markout_ticks).
# Counts are exact; the two statistics carry the usual fsum tolerance. Every row
# is a value the README table prints, so drift here is a README defect.
EXPECTED_QUEUE_POSITION: tuple[tuple[str, int, int, float, float], ...] = (
    ("0", 4, 4, 1.0, -0.375),
    ("1-2", 5, 3, 0.6, 1.3333333333333333),
    ("3-5", 12, 8, 0.6666666666666666, 0.03125),
    ("6-11", 29, 9, 0.3103448275862069, -0.6666666666666666),
    ("12-24", 74, 9, 0.12162162162162163, -0.3333333333333333),
    ("25+", 2876, 0, 0.0, 0.0),
)

# The prose claim about the *shape* of that series, asserted as a property rather
# than as numbers, so it keeps holding if the seeded path is ever regenerated.
#
# The README used to claim the decline was monotonic. It never was: the 3-5 bucket
# sits above the 1-2 bucket, and the README's own table three sections lower
# printed those true values, so the document contradicted itself. The prose now
# says "a strong decline, but not monotone", monotone from the 3-5 bucket down.
# Both halves of that are checked below, in both directions:
#
#   * if the data ever becomes monotone, the "not monotone" prose goes stale and
#     this fails -- an understated claim is still a wrong claim;
#   * if the tail ever stops declining, the narrower claim fails too;
#   * and any prose that re-asserts monotonicity fails on the text itself, which
#     is what actually caught nothing before this guard existed.
QUEUE_MONOTONE_FROM_BUCKET = "3-5"

# The bullet's endpoints, as the README renders them. Checking the literal string
# is what ties the prose to the artifact: the old bullet said "1.00 at the front"
# while quoting a series whose front bucket was 0.8.
QUEUE_ENDPOINT_PROSE = "**1.00 at the front of the queue to 0.00 past 25 lots deep**"

# Any surviving positive monotonicity claim. "not monotone" is fine; "falls
# monotonically" is not.
MONOTONE_WORD = re.compile(r"monoton\w*", re.IGNORECASE)
NEGATED_BEFORE = re.compile(r"\bnot\W{0,3}$", re.IGNORECASE)


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


def _check_queue_position(comparison: dict, failures: list[str]) -> int:
    """Check the documented queue-position series and the prose claims about it.

    Three separate things are verified, because the bug this guards against had
    three separate parts: a wrong front-bucket number, a monotonicity claim the
    data never supported, and a README that disagreed with its own table.

    Returns the number of assertions performed so ``main`` can report a count.
    """
    series = comparison["queue_position"][QUEUE_POSITION_SERIES]
    by_bucket = {row["queue_bucket"]: row for row in series}
    checked = 0

    # 1. Every value the README table prints.
    for bucket, orders, filled, probability, markout in EXPECTED_QUEUE_POSITION:
        checked += 4
        row = by_bucket.get(bucket)
        if row is None:
            failures.append(
                f"queue_position.{QUEUE_POSITION_SERIES}: bucket {bucket!r} is missing "
                f"(got {sorted(by_bucket)})"
            )
            continue
        prefix = f"queue_position.{QUEUE_POSITION_SERIES}[{bucket}]"
        if row["orders"] != orders:
            failures.append(f"{prefix}.orders: expected {orders}, got {row['orders']}")
        if row["filled_orders"] != filled:
            failures.append(
                f"{prefix}.filled_orders: expected {filled}, got {row['filled_orders']}"
            )
        for metric, expected in (
            ("fill_probability", probability),
            ("mean_markout_ticks", markout),
        ):
            actual = row[metric]
            if not math.isclose(actual, expected, abs_tol=1e-12, rel_tol=0.0):
                failures.append(f"{prefix}.{metric}: expected {expected}, got {actual}")

    # 2. The shape claim: not monotone overall, monotone from QUEUE_MONOTONE_FROM_BUCKET
    #    down. Asserted as a property of the data, so it survives a regenerated path.
    probabilities = [row["fill_probability"] for row in series]
    checked += 2
    if all(b <= a for a, b in zip(probabilities, probabilities[1:])):
        failures.append(
            f"queue_position.{QUEUE_POSITION_SERIES}.fill_probability is now monotone "
            f"non-increasing ({probabilities}), but the README says it is not -- "
            "the prose is understating the result and must be updated"
        )
    buckets = [row["queue_bucket"] for row in series]
    if QUEUE_MONOTONE_FROM_BUCKET not in buckets:
        failures.append(
            f"queue_position.{QUEUE_POSITION_SERIES}: bucket "
            f"{QUEUE_MONOTONE_FROM_BUCKET!r} is missing, so the README's "
            "'uninterrupted from the 3-5 bucket down' claim cannot be checked"
        )
    else:
        tail = probabilities[buckets.index(QUEUE_MONOTONE_FROM_BUCKET) :]
        if not all(b <= a for a, b in zip(tail, tail[1:])):
            failures.append(
                f"queue_position.{QUEUE_POSITION_SERIES}.fill_probability no longer "
                f"declines without interruption from the {QUEUE_MONOTONE_FROM_BUCKET} "
                f"bucket down ({tail}); the README claims it does"
            )

    if not README.exists():  # running the script outside a checkout
        return checked
    text = README.read_text(encoding="utf-8")

    # 3. The prose itself. The endpoints the bullet prints have to be the endpoints
    #    of the series it names, and no surviving sentence may claim monotonicity.
    checked += 3
    front = probabilities[0]
    back = probabilities[-1]
    expected_prose = f"**{front:.2f} at the front of the queue to {back:.2f} past 25 lots deep**"
    if expected_prose != QUEUE_ENDPOINT_PROSE:
        failures.append(
            f"queue_position.{QUEUE_POSITION_SERIES}: endpoints are now {front} and "
            f"{back}, so the README bullet should read {expected_prose!r}, not "
            f"{QUEUE_ENDPOINT_PROSE!r}"
        )
    if text.count(QUEUE_ENDPOINT_PROSE) != 1:
        failures.append(
            f"README.md contains {QUEUE_ENDPOINT_PROSE!r} "
            f"{text.count(QUEUE_ENDPOINT_PROSE)} time(s), expected 1 (resume highlights)"
        )
    if QUEUE_POSITION_SERIES not in text:
        failures.append(
            f"README.md never names the {QUEUE_POSITION_SERIES!r} series, so a reader "
            "cannot tell which of the two policies the quoted fill probabilities are"
        )

    for match in MONOTONE_WORD.finditer(text):
        checked += 1
        if not NEGATED_BEFORE.search(text[max(0, match.start() - 24) : match.start()]):
            line = text.count("\n", 0, match.start()) + 1
            failures.append(
                f"README.md:{line} asserts {match.group(0)!r} without negation; the "
                f"measured {QUEUE_POSITION_SERIES} fill probabilities are "
                f"{probabilities}, which are not monotone"
            )

    return checked


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
        checked += _check_queue_position(comparison, failures)

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
