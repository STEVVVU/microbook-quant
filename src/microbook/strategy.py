"""Market-making strategies with explicit inventory controls."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Quote:
    bid: int | None
    ask: int | None
    size: int


class FixedSpread:
    name = "fixed_spread"

    def __init__(self, *, half_spread: int = 1, size: int = 2) -> None:
        self.half_spread = half_spread
        self.size = size

    def quote(self, midpoint: float, inventory: int, volatility: float) -> Quote:
        del inventory, volatility
        return Quote(
            bid=math.floor(midpoint) - self.half_spread,
            ask=math.ceil(midpoint) + self.half_spread,
            size=self.size,
        )


class InventoryAware:
    """Avellaneda-Stoikov-inspired reservation-price market maker."""

    name = "inventory_aware"

    def __init__(
        self,
        *,
        base_half_spread: int = 1,
        size: int = 2,
        risk_aversion: float = 0.15,
        inventory_limit: int = 18,
    ) -> None:
        self.base_half_spread = base_half_spread
        self.size = size
        self.risk_aversion = risk_aversion
        self.inventory_limit = inventory_limit

    def quote(self, midpoint: float, inventory: int, volatility: float) -> Quote:
        risk_scale = max(volatility, 0.5)
        reservation = midpoint - self.risk_aversion * inventory * risk_scale
        half_spread = self.base_half_spread + int(volatility >= 1.1)
        bid = math.floor(reservation) - half_spread
        ask = math.ceil(reservation) + half_spread
        return Quote(
            bid=None if inventory >= self.inventory_limit else bid,
            ask=None if inventory <= -self.inventory_limit else ask,
            size=self.size,
        )
