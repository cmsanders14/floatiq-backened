"""Feature generation for current and curated historical market-analog windows."""

import math

import numpy as np
import pandas as pd


def compute_market_profile(
    prices: pd.DataFrame,
    benchmark_prices: pd.DataFrame | None = None,
    fundamentals: dict | None = None,
) -> dict:
    if prices is None or prices.empty:
        raise ValueError("Price history is empty")
    frame = prices.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    if "Close" not in frame or "Volume" not in frame:
        raise ValueError("Price history must include Close and Volume")
    close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    volume = pd.to_numeric(frame["Volume"], errors="coerce").reindex(close.index).dropna()
    common_index = close.index.intersection(volume.index)
    close = close.loc[common_index]
    volume = volume.loc[common_index]
    if len(close) < 40 or close.iloc[0] <= 0:
        raise ValueError("At least 40 valid observations are required")

    returns = close.pct_change().dropna()
    total_return_pct = ((close.iloc[-1] / close.iloc[0]) - 1) * 100
    volatility_pct = returns.std(ddof=1) * math.sqrt(252) * 100
    drawdown = (close / close.cummax()) - 1
    max_drawdown_pct = abs(drawdown.min()) * 100
    first_volume = volume.iloc[:20].mean()
    last_volume = volume.iloc[-20:].mean()
    volume_growth_pct = ((last_volume / first_volume) - 1) * 100 if first_volume > 0 else 0
    time_axis = np.arange(len(close), dtype=float)
    trend_strength = float(np.corrcoef(time_axis, np.log(close.to_numpy(dtype=float)))[0, 1])

    relative_strength_pct = None
    if benchmark_prices is not None and not benchmark_prices.empty:
        benchmark = benchmark_prices.copy()
        if isinstance(benchmark.columns, pd.MultiIndex):
            benchmark.columns = benchmark.columns.get_level_values(0)
        benchmark_close = pd.to_numeric(benchmark.get("Close"), errors="coerce").dropna()
        if len(benchmark_close) >= 2 and benchmark_close.iloc[0] > 0:
            benchmark_return = ((benchmark_close.iloc[-1] / benchmark_close.iloc[0]) - 1) * 100
            relative_strength_pct = total_return_pct - benchmark_return

    fundamentals = fundamentals or {}
    profile = {
        "total_return_pct": round(float(total_return_pct), 4),
        "annualized_volatility_pct": round(float(volatility_pct), 4),
        "max_drawdown_pct": round(float(max_drawdown_pct), 4),
        "volume_growth_pct": round(float(volume_growth_pct), 4),
        "trend_strength": round(float(trend_strength), 6),
        "relative_strength_pct": (
            round(float(relative_strength_pct), 4) if relative_strength_pct is not None else None
        ),
        "market_cap": fundamentals.get("market_cap"),
        "revenue_growth_pct": fundamentals.get("revenue_growth_pct"),
        "gross_margin_pct": fundamentals.get("gross_margin_pct"),
        "short_float_pct": fundamentals.get("short_float_pct"),
        "observation_count": len(close),
    }
    return profile

