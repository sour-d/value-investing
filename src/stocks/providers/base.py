"""The provider interface, kept separate from any vendor implementation.

Exists so the sync logic can be tested against a fake and so adding a second
vendor later does not mean touching the sync code.
"""

from __future__ import annotations

from typing import Protocol

from .yahoo import Financials, PriceBar, Quote


class Provider(Protocol):
    """What the sync layer needs from a data source."""

    name: str

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Latest price, market cap and 52-week range per symbol."""

    def prices(self, symbols: list[str], start: str, end: str) -> dict[str, list[PriceBar]]:
        """Daily OHLCV per symbol, inclusive of both endpoints."""

    def financials(self, symbols: list[str]) -> dict[str, Financials]:
        """Annual statements per symbol, keyed by fiscal period."""

    def benchmark(self, index_code: str, start: str, end: str) -> list[PriceBar]:
        """Daily bars for a benchmark index."""