"""On-demand, bounded setup scanning. No database writes or order calls."""
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .alpaca_data import AlpacaDataError, AlpacaMarketDataProvider

DEFAULT_SYMBOLS = "AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,AMD,AVGO,PLTR,SMCI,INTC,MU,ORCL,CRM,NFLX,UBER,COIN,HOOD,SOFI,JPM,BAC,GS,XOM,CVX,WMT,COST,DIS,BA,GE"
BAR_SECONDS = 900
MAX_AGE_SECONDS = 1200
NY = ZoneInfo("America/New_York")
SUPPORTED = ["bull", "bear", "engulfing", "breakout", "breakdown"]


def detect_setups(symbol, frame, now, feed):
    if frame.empty:
        return None
    # Discard unfinished candles. A newly fetched old bar is still old.
    end_times = frame.index + timedelta(seconds=BAR_SECONDS)
    completed = frame.loc[end_times <= now].copy()
    if completed.empty:
        return None
    local = completed.index.tz_convert(NY)
    minutes = local.hour * 60 + local.minute
    completed = completed.loc[(local.dayofweek < 5) & (minutes >= 570) & (minutes < 960)]
    if len(completed) < 21:
        return None
    observed = completed.index[-1] + timedelta(seconds=BAR_SECONDS)
    age = (now - observed).total_seconds()
    if age < 0 or age > MAX_AGE_SECONDS or observed.tz_convert(NY).date() != now.astimezone(NY).date():
        return None
    latest, previous = completed.iloc[-1], completed.iloc[-2]
    baseline = completed.iloc[-21:-1]
    labels = []
    contiguous = (completed.index[-1] - completed.index[-2]).total_seconds() == BAR_SECONDS
    if contiguous and previous.Close < previous.Open and latest.Close > latest.Open and latest.Open <= previous.Close and latest.Close >= previous.Open:
        labels.append("Bullish Engulfing")
    if contiguous and previous.Close > previous.Open and latest.Close < latest.Open and latest.Open >= previous.Close and latest.Close <= previous.Open:
        labels.append("Bearish Engulfing")
    if latest.Close > baseline.High.max():
        labels.append("Bullish Breakout")
    if latest.Close < baseline.Low.min():
        labels.append("Bearish Breakdown")
    mean_volume = float(baseline.Volume.mean())
    return {
        "ticker": symbol, "pattern_detected": "; ".join(labels),
        "current_price": float(latest.Close),
        "current_rvol": round(float(latest.Volume) / mean_volume, 3) if mean_volume > 0 else None,
        "rvol_basis": "latest_bar_vs_previous_20_regular_session_bars",
        "market_session": "regular", "updated_at": observed.isoformat(),
        "observed_at": observed.isoformat(), "bar_started_at": completed.index[-1].isoformat(),
        "interval": "15m", "source": "alpaca", "feed": feed,
        "gap_fill_probability": None, "sample_size_count": 0,
        "statistical_claim_status": "not_estimated", "win_rate_display_allowed": False,
    }


class SetupScanner:
    def __init__(self, provider_factory=AlpacaMarketDataProvider, symbols=None):
        self.provider_factory = provider_factory
        self.symbols = list(dict.fromkeys(s.strip().upper() for s in (symbols or os.getenv("SETUP_SCAN_SYMBOLS", DEFAULT_SYMBOLS).split(",")) if s.strip()))
        if not 1 <= len(self.symbols) <= 50:
            raise ValueError("SETUP_SCAN_SYMBOLS must contain 1 to 50 symbols")
        self._lock = threading.Lock()
        self._frames = None
        self._refreshed = 0
        self._failed_until = 0
        self._feed = None

    def search(self, setup, now=None):
        now = now or datetime.now(timezone.utc)
        query = setup.lower().strip()
        aliases = {"bullish": "bull", "bearish": "bear", "bullish engulfing": "bullish engulfing", "bearish engulfing": "bearish engulfing"}
        query = aliases.get(query, query)
        if not any(term in query for term in SUPPORTED):
            return [], self._metadata(now, "unsupported_setup", 0)
        if not self._lock.acquire(timeout=2):
            raise AlpacaDataError("Setup scan is refreshing; retry shortly")
        try:
            clock = time.monotonic()
            if clock < self._failed_until:
                raise AlpacaDataError("Setup data temporarily unavailable; retry shortly")
            if self._frames is None or clock - self._refreshed >= 30:
                try:
                    provider = self.provider_factory()
                    frames = provider.download_many_range(self.symbols, (now-timedelta(days=5)).isoformat(), now.isoformat(), "15m", max_pages=5)
                    self._frames, self._feed = frames, provider.feed
                    self._refreshed = time.monotonic()
                except Exception:
                    self._frames = None
                    self._failed_until = time.monotonic() + 30
                    raise AlpacaDataError("Setup data temporarily unavailable; retry shortly") from None
            rows = [detect_setups(s, f, now, self._feed) for s, f in self._frames.items()]
            fresh = [r for r in rows if r is not None]
            matches = [r for r in fresh if query in r["pattern_detected"].lower()]
            matches.sort(key=lambda r: (-(r["current_rvol"] or 0), r["ticker"]))
            status = "available" if matches else "no_matching_setups" if fresh else "no_fresh_bars"
            return matches, self._metadata(now, status, len(fresh))
        finally:
            self._lock.release()

    def _metadata(self, now, status, count):
        return {"checked_at": now.isoformat(), "data_status": status,
                "source": "alpaca", "feed": self._feed, "scan_interval": "15m",
                "scan_mode": "on_demand", "coverage": "configured_symbol_list",
                "symbols_requested": len(self.symbols), "symbols_with_fresh_bars": count,
                "supported_setups": SUPPORTED, "max_record_age_seconds": MAX_AGE_SECONDS,
                "message": {"available": "Setups detected on completed 15-minute candles.",
                            "no_matching_setups": "No matching setups in the scanned stocks.",
                            "no_fresh_bars": "No fresh completed regular-session bars. The market may be closed or data unavailable.",
                            "unsupported_setup": "Supported searches: bull, bear, engulfing, breakout, breakdown."}[status]}


SCANNER = SetupScanner()
