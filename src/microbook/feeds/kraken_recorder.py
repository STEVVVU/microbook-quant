"""Atomic recorder for Kraken's public v2 book and trade channels."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from .kraken import (
    KrakenBook,
    KrakenSnapshot,
    KrakenTrade,
    KrakenUpdate,
    event_to_dict,
    parse_kraken_message,
)

KRAKEN_WEBSOCKET_URL = "wss://ws.kraken.com/v2"


@dataclass(frozen=True)
class KrakenCaptureSummary:
    venue: str
    symbol: str
    depth: int
    events: int
    snapshots: int
    updates: int
    trades: int
    checksums_verified: int
    first_received_ns: int
    last_received_ns: int
    duration_seconds: float
    final_best_bid_ticks: int
    final_best_ask_ticks: int
    output: str
    sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def record_kraken_messages(
    messages: AsyncIterator[str],
    output: str | Path,
    *,
    symbol: str,
    depth: int,
    max_events: int,
) -> KrakenCaptureSummary:
    if max_events <= 0:
        raise ValueError("max_events must be positive")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".part")
    book = KrakenBook(symbol, depth=depth)
    counts = {"snapshot": 0, "update": 0, "trade": 0}
    first_received_ns: int | None = None
    last_received_ns: int | None = None
    events_recorded = 0

    try:
        with gzip.open(partial, "wt", encoding="utf-8", newline="\n") as destination:
            async for raw_message in messages:
                received_ns = time.time_ns()
                message = json.loads(raw_message, parse_float=Decimal)
                for event in parse_kraken_message(message, received_ns):
                    if event.symbol != symbol:
                        continue
                    if isinstance(event, (KrakenSnapshot, KrakenUpdate)):
                        book.apply(event)
                    elif isinstance(event, KrakenTrade) and not book.initialized:
                        continue
                    destination.write(
                        json.dumps(event_to_dict(event), separators=(",", ":")) + "\n"
                    )
                    counts[event.kind] += 1
                    events_recorded += 1
                    first_received_ns = (
                        event.received_ns
                        if first_received_ns is None
                        else first_received_ns
                    )
                    last_received_ns = event.received_ns
                    if events_recorded >= max_events:
                        break
                if events_recorded >= max_events:
                    break
        if not book.initialized or events_recorded == 0:
            raise RuntimeError("capture ended before a usable L2 snapshot arrived")
        if book.best_bid is None or book.best_ask is None:
            raise RuntimeError("capture ended with an incomplete book")
        os.replace(partial, output)
    finally:
        partial.unlink(missing_ok=True)

    assert first_received_ns is not None
    assert last_received_ns is not None
    return KrakenCaptureSummary(
        venue="Kraken",
        symbol=symbol,
        depth=depth,
        events=events_recorded,
        snapshots=counts["snapshot"],
        updates=counts["update"],
        trades=counts["trade"],
        checksums_verified=book.checksums_verified,
        first_received_ns=first_received_ns,
        last_received_ns=last_received_ns,
        duration_seconds=(last_received_ns - first_received_ns) / 1_000_000_000,
        final_best_bid_ticks=book.best_bid,
        final_best_ask_ticks=book.best_ask,
        output=str(output),
        sha256=_sha256(output),
    )


async def capture_kraken(
    output: str | Path,
    *,
    symbol: str = "BTC/USD",
    depth: int = 10,
    max_events: int = 5_000,
    duration_seconds: float = 60.0,
    url: str = KRAKEN_WEBSOCKET_URL,
) -> KrakenCaptureSummary:
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    try:
        import websockets
    except ImportError as error:
        raise RuntimeError("install the live extra: pip install -e '.[live]'") from error

    async with websockets.connect(
        url,
        max_size=64 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=20,
    ) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "method": "subscribe",
                    "params": {
                        "channel": "book",
                        "symbol": [symbol],
                        "depth": depth,
                        "snapshot": True,
                    },
                }
            )
        )
        await websocket.send(
            json.dumps(
                {
                    "method": "subscribe",
                    "params": {
                        "channel": "trade",
                        "symbol": [symbol],
                        "snapshot": False,
                    },
                }
            )
        )
        deadline = asyncio.get_running_loop().time() + duration_seconds

        async def messages() -> AsyncIterator[str]:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    yield await asyncio.wait_for(websocket.recv(), timeout=remaining)
                except TimeoutError:
                    break

        return await record_kraken_messages(
            messages(),
            output,
            symbol=symbol,
            depth=depth,
            max_events=max_events,
        )


def summary_json(summary: KrakenCaptureSummary) -> str:
    return json.dumps(asdict(summary), indent=2, sort_keys=True) + "\n"

