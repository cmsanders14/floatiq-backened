"""Replaceable market-data provider boundary with short-lived response caching."""

import os
import threading
import time
from collections import OrderedDict
from typing import Protocol

import pandas as pd
import yfinance as yf


class MarketDataProvider(Protocol):
    name: str

    def download(self, ticker: str, period: str, interval: str) -> pd.DataFrame: ...

    def download_range(
        self, ticker: str, start: str, end: str, interval: str
    ) -> pd.DataFrame: ...


class YahooMarketDataProvider:
    """Limited testing provider; not intended for a production 6,000-symbol scan."""

    name = "yahoo_testing"

    def download(self, ticker: str, period: str, interval: str) -> pd.DataFrame:
        return yf.download(
            ticker,
            period=period,
            interval=interval,
            auto_adjust=False,
            progress=False,
            threads=False,
        )

    def download_range(self, ticker: str, start: str, end: str, interval: str) -> pd.DataFrame:
        return yf.download(
            ticker,
            start=start,
            end=end,
            interval=interval,
            auto_adjust=False,
            progress=False,
            threads=False,
        )


def build_market_data_provider() -> MarketDataProvider:
    provider_name = os.getenv("MARKET_DATA_PROVIDER", "yahoo").strip().lower()
    if provider_name == "yahoo":
        return YahooMarketDataProvider()
    raise RuntimeError(
        f"Unsupported MARKET_DATA_PROVIDER={provider_name!r}. Add the paid provider adapter first."
    )


MARKET_DATA_PROVIDER = build_market_data_provider()
MARKET_DATA_CACHE_MAX_ENTRIES = max(1, int(os.getenv("MARKET_DATA_CACHE_MAX_ENTRIES", "1000")))
MARKET_DATA_CACHE_MAX_STALE_SECONDS = max(
    300, int(os.getenv("MARKET_DATA_CACHE_MAX_STALE_SECONDS", "3600"))
)
_MARKET_DATA_CACHE: OrderedDict[
    tuple[str, str, str], tuple[float, pd.DataFrame]
] = OrderedDict()
_MARKET_DATA_CACHE_LOCK = threading.Lock()


def download_market_data(ticker: str, period: str, interval: str) -> pd.DataFrame:
    """Cache provider responses and reuse a stale success during a temporary outage."""
    cache_key = (ticker.upper(), period, interval)
    now = time.monotonic()
    ttl_seconds = 30 if interval.endswith(("m", "h")) else 300
    with _MARKET_DATA_CACHE_LOCK:
        cached = _MARKET_DATA_CACHE.get(cache_key)
        if cached:
            _MARKET_DATA_CACHE.move_to_end(cache_key)
    if cached and now - cached[0] < ttl_seconds:
        return cached[1].copy()

    data = MARKET_DATA_PROVIDER.download(ticker, period, interval)
    if data is not None and not data.empty:
        with _MARKET_DATA_CACHE_LOCK:
            _MARKET_DATA_CACHE[cache_key] = (now, data.copy())
            _MARKET_DATA_CACHE.move_to_end(cache_key)
            while len(_MARKET_DATA_CACHE) > MARKET_DATA_CACHE_MAX_ENTRIES:
                _MARKET_DATA_CACHE.popitem(last=False)
        return data
    if cached and now - cached[0] <= MARKET_DATA_CACHE_MAX_STALE_SECONDS:
        return cached[1].copy()
    if cached:
        with _MARKET_DATA_CACHE_LOCK:
            _MARKET_DATA_CACHE.pop(cache_key, None)
    return pd.DataFrame()


def download_market_data_range(
    ticker: str, start: str, end: str, interval: str = "1d"
) -> pd.DataFrame:
    return MARKET_DATA_PROVIDER.download_range(ticker, start, end, interval)
