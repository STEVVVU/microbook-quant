"""Atomic recorder for Coinbase's public WebSocket feed."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path

from .coinbase import CoinbaseBook, L2Snapshot, L2Update, Trade, event_to_dict, parse_message

COINBASE_WEBSOCKET_URL = "wss://ws-feed.exchange.coinbase.com"


@dataclass(frozen=True)
class CaptureSummary:
    product_id: str
    events: int
    snapshots: int
    updates: int
    trades: int
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


async def record_messages(
    messages: AsyncIterator[str],
    output: str | Path,
    *,
    product_id: str,
    max_events: int,
) -> CaptureSummary:
    """Normalize an async message stream and commit a complete gzip atomically."""

    if max_events <= 0:
        raise ValueError("max_events must be positive")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".part")
    book = CoinbaseBook(product_id)
    counts = {"snapshot": 0, "l2update": 0, "trade": 0}
    first_received_ns: int | None = None
    last_received_ns: int | None = None
    events = 0

    try:
        with gzip.open(partial, "wt", encoding="utf-8", newline="\n") as destination:
            async for raw_message in messages:
                received_ns = time.time_ns()
                message = json.loads(raw_message)
                event = parse_message(message, received_ns)
                if event is None or event.product_id != product_id:
                    continue
                if isinstance(event, (L2Snapshot, L2Update)):
                    book.apply(event)
                elif isinstance(event, Trade) and not book.initialized:
                    continue
                destination.write(json.dumps(event_to_dict(event), separators=(",", ":")) + "\n")
                counts[event.kind] += 1
                events += 1
                first_received_ns = (
                    event.received_ns if first_received_ns is None else first_received_ns
                )
                last_received_ns = event.received_ns
                if events >= max_events:
                    break
        if not book.initialized or events == 0:
            raise RuntimeError("capture ended before a usable L2 snapshot arrived")
        if book.best_bid is None or book.best_ask is None:
            raise RuntimeError("capture ended with an incomplete book")
        os.replace(partial, output)
    finally:
        partial.unlink(missing_ok=True)

    assert first_received_ns is not None
    assert last_received_ns is not None
    return CaptureSummary(
        product_id=product_id,
        events=events,
        snapshots=counts["snapshot"],
        updates=counts["l2update"],
        trades=counts["trade"],
        first_received_ns=first_received_ns,
        last_received_ns=last_received_ns,
        duration_seconds=(last_received_ns - first_received_ns) / 1_000_000_000,
        final_best_bid_ticks=book.best_bid,
        final_best_ask_ticks=book.best_ask,
        output=str(output),
        sha256=_sha256(output),
    )


async def capture_coinbase(
    output: str | Path,
    *,
    product_id: str = "BTC-USD",
    max_events: int = 5_000,
    duration_seconds: float = 60.0,
    url: str = COINBASE_WEBSOCKET_URL,
) -> CaptureSummary:
    """Connect to Coinbase Exchange and record normalized L2 and trade events."""

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
                    "type": "subscribe",
                    "product_ids": [product_id],
                    "channels": ["level2", "matches", "heartbeat"],
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

        return await record_messages(
            messages(),
            output,
            product_id=product_id,
            max_events=max_events,
        )


def summary_json(summary: CaptureSummary) -> str:
    return json.dumps(asdict(summary), indent=2, sort_keys=True) + "\n"

