"""Read-only Alpaca stock bars; contains no account or order API calls."""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd


class AlpacaDataError(RuntimeError):
    pass


class AlpacaMarketDataProvider:
    name = "alpaca"
    _intervals = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "1h": "1Hour", "1d": "1Day"}
    _days = {"1d": 1, "5d": 5, "7d": 7, "1mo": 31, "6mo": 183, "1y": 366}

    def __init__(self, key=None, secret=None, feed=None, opener=urlopen):
        self._key = key or os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY")
        self._secret = secret or os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
        self.feed = feed or os.getenv("ALPACA_DATA_FEED", "iex")
        if not self._key or not self._secret:
            raise AlpacaDataError("Alpaca API key and secret environment variables are required")
        if self.feed not in {"iex", "sip"}:
            raise AlpacaDataError("ALPACA_DATA_FEED must be iex or sip")
        self._opener = opener

    def download(self, ticker, period, interval):
        end = datetime.now(timezone.utc)
        if period == "max":
            start = datetime(2016, 1, 1, tzinfo=timezone.utc)
        elif period in self._days:
            start = end - timedelta(days=self._days[period])
        else:
            raise AlpacaDataError("Unsupported history period")
        return self.download_range(ticker, start.isoformat(), end.isoformat(), interval)

    def download_range(self, ticker, start, end, interval):
        symbol = ticker.strip().upper()
        return self.download_many_range([symbol], start, end, interval)[symbol]

    def download_many_range(self, tickers, start, end, interval, max_pages=100):
        symbols = list(dict.fromkeys(t.strip().upper() for t in tickers))
        if not symbols or len(symbols) > 50 or any(not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", s) for s in symbols):
            raise AlpacaDataError("Unsupported Alpaca stock symbol")
        if interval not in self._intervals:
            raise AlpacaDataError("Unsupported bar interval")
        params = dict(symbols=",".join(symbols), timeframe=self._intervals[interval], start=start,
                      end=end, feed=self.feed, adjustment="all", sort="asc", limit=10000)
        rows, seen_tokens = {symbol: [] for symbol in symbols}, set()
        for _ in range(max_pages):
            request = Request("https://data.alpaca.markets/v2/stocks/bars?" + urlencode(params),
                              headers={"APCA-API-KEY-ID": self._key,
                                       "APCA-API-SECRET-KEY": self._secret,
                                       "Accept": "application/json"})
            try:
                with self._opener(request, timeout=20) as response:
                    payload = json.load(response)
            except HTTPError as exc:
                # Do not include response bodies or credential-bearing request headers.
                raise AlpacaDataError(f"Alpaca data request failed (HTTP {exc.code})") from None
            except (URLError, TimeoutError, ValueError):
                raise AlpacaDataError("Alpaca data request unavailable or invalid") from None
            if not isinstance(payload, dict) or not isinstance(payload.get("bars"), dict):
                raise AlpacaDataError("Alpaca returned an invalid bars response")
            for symbol in symbols:
                page = payload["bars"].get(symbol, [])
                if not isinstance(page, list):
                    raise AlpacaDataError("Alpaca returned invalid symbol bars")
                rows[symbol].extend(page)
            token = payload.get("next_page_token")
            if not token:
                break
            if not isinstance(token, str) or token in seen_tokens:
                raise AlpacaDataError("Alpaca returned an invalid pagination token")
            seen_tokens.add(token)
            params["page_token"] = token
        else:
            raise AlpacaDataError("History request is too large; narrow the date range")
        return {symbol: self._frame(values) for symbol, values in rows.items()}

    def _frame(self, rows):
        columns = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
        if not rows:
            return pd.DataFrame(columns=columns, index=pd.DatetimeIndex([], tz="UTC"))
        try:
            frame = pd.DataFrame(rows)
            frame.index = pd.to_datetime(frame["t"], utc=True, errors="raise")
            frame = frame.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
            frame["Adj Close"] = frame["Close"]
            frame = frame[columns].apply(pd.to_numeric, errors="raise")
            if frame.isna().any().any() or frame.isin([float("inf"), float("-inf")]).any().any():
                raise ValueError("Invalid numerical bar")
            if (frame[["Open", "High", "Low", "Close"]] <= 0).any().any() or (frame["Volume"] < 0).any():
                raise ValueError("Invalid price or volume")
            if ((frame["High"] < frame[["Open", "Close", "Low"]].max(axis=1)) |
                    (frame["Low"] > frame[["Open", "Close", "High"]].min(axis=1))).any():
                raise ValueError("Inconsistent OHLC bar")
            frame = frame[~frame.index.duplicated(keep="last")].sort_index()
            frame.attrs["provider"] = "alpaca"
            frame.attrs["feed"] = self.feed
            return frame
        except (KeyError, TypeError, ValueError):
            raise AlpacaDataError("Alpaca returned malformed price bars") from None
