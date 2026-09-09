"""Objective launch controls for regimes, feed health, and statistical claims."""

from __future__ import annotations

import math
from datetime import datetime, timezone


MARKET_REGIMES = {
    "bullish_trend",
    "bearish_trend",
    "range_bound",
    "high_volatility",
    "insufficient_data",
}


def _finite_number(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def classify_market_regime(
    *,
    benchmark_change_pct: float | None,
    benchmark_above_20d_ma: bool | None,
    market_breadth_pct: float | None,
    volatility_percentile: float | None,
    liquidity_status: str | None,
) -> dict:
    """Classify supplied market-wide observations without inventing missing data."""
    change = _finite_number(benchmark_change_pct, "benchmark_change_pct")
    breadth = _finite_number(market_breadth_pct, "market_breadth_pct")
    volatility = _finite_number(volatility_percentile, "volatility_percentile")
    if breadth is not None and not 0 <= breadth <= 100:
        raise ValueError("market_breadth_pct must be between 0 and 100")
    if volatility is not None and not 0 <= volatility <= 100:
        raise ValueError("volatility_percentile must be between 0 and 100")
    normalized_liquidity = (liquidity_status or "").strip().lower()
    if normalized_liquidity and normalized_liquidity not in {"normal", "thin", "stressed"}:
        raise ValueError("liquidity_status must be normal, thin, or stressed")

    inputs = {
        "benchmark_change_pct": change,
        "benchmark_above_20d_ma": benchmark_above_20d_ma,
        "market_breadth_pct": breadth,
        "volatility_percentile": volatility,
        "liquidity_status": normalized_liquidity or None,
    }
    if any(value is None for value in inputs.values()):
        return {
            "market_regime": "insufficient_data",
            "regime_tags": [],
            "regime_basis": inputs,
            "regime_prediction_claimed": False,
        }

    tags = []
    if volatility >= 80 or normalized_liquidity == "stressed":
        regime = "high_volatility"
        tags.append("risk_conditions_elevated")
    elif change >= 0.5 and benchmark_above_20d_ma and breadth >= 60:
        regime = "bullish_trend"
        tags.append("broad_positive_participation")
    elif change <= -0.5 and not benchmark_above_20d_ma and breadth <= 40:
        regime = "bearish_trend"
        tags.append("broad_negative_participation")
    else:
        regime = "range_bound"
        tags.append("mixed_market_participation")
    if normalized_liquidity == "thin":
        tags.append("thin_liquidity")
    if volatility >= 80:
        tags.append("volatility_percentile_high")
    return {
        "market_regime": regime,
        "regime_tags": tags,
        "regime_basis": inputs,
        "regime_prediction_claimed": False,
    }


def statistical_claim_status(
    *, successes: int, total: int, minimum_sample_size: int = 30
) -> dict:
    """Gate displayed win-rate claims until the declared evidence floor is met."""
    if total < 0 or successes < 0 or successes > total:
        raise ValueError("successes and total are inconsistent")
    if minimum_sample_size < 2:
        raise ValueError("minimum_sample_size must be at least 2")
    display_allowed = total >= minimum_sample_size
    return {
        "sample_size": total,
        "minimum_sample_size_required": minimum_sample_size,
        "win_rate_display_allowed": display_allowed,
        "statistical_claim_status": "displayable" if display_allowed else "insufficient_sample",
        "observed_win_rate_pct": round(successes / total * 100, 2) if display_allowed else None,
    }


def assess_feed_batch(
    records: list[dict],
    *,
    now: datetime,
    max_age_seconds: int,
    maximum_stale_fraction: float = 0.50,
) -> dict:
    """Halt publication for empty, malformed, duplicated, future, or mostly stale batches."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    if max_age_seconds < 15:
        raise ValueError("max_age_seconds must be at least 15")
    if not 0 <= maximum_stale_fraction <= 1:
        raise ValueError("maximum_stale_fraction must be between 0 and 1")

    seen_events = set()
    stale_count = duplicate_count = invalid_count = future_count = 0
    for record in records:
        event_key = (
            str(record.get("source_name") or "").strip(),
            str(record.get("source_event_id") or "").strip(),
        )
        row_invalid = False
        try:
            price = float(record.get("price"))
            price_valid = math.isfinite(price) and price > 0
        except (TypeError, ValueError):
            price_valid = False
        if not all(event_key) or not record.get("ticker") or not price_valid:
            row_invalid = True
        if event_key in seen_events:
            duplicate_count += 1
        seen_events.add(event_key)
        try:
            observed_at = datetime.fromisoformat(str(record["observed_at"]).replace("Z", "+00:00"))
            if observed_at.tzinfo is None or observed_at.utcoffset() is None:
                raise ValueError
            age = (now.astimezone(timezone.utc) - observed_at.astimezone(timezone.utc)).total_seconds()
            if age < -30:
                future_count += 1
            elif age > max_age_seconds:
                stale_count += 1
        except (KeyError, TypeError, ValueError):
            row_invalid = True
        if row_invalid:
            invalid_count += 1

    total = len(records)
    stale_fraction = stale_count / total if total else 1.0
    reasons = []
    if not total:
        reasons.append("no_observations")
    if invalid_count:
        reasons.append("invalid_observations")
    if duplicate_count:
        reasons.append("duplicate_source_events")
    if future_count:
        reasons.append("future_timestamps")
    if stale_fraction > maximum_stale_fraction:
        reasons.append("stale_fraction_exceeded")
    return {
        "publication_halted": bool(reasons),
        "health_status": "halted" if reasons else "healthy",
        "reason_codes": reasons,
        "observation_count": total,
        "stale_count": stale_count,
        "duplicate_count": duplicate_count,
        "invalid_count": invalid_count,
        "future_count": future_count,
        "stale_fraction": round(stale_fraction, 4),
    }


def beta_access_status(
    *, user_id: str, public_launch_enabled: bool, beta_mode_enabled: bool, allowlist: set[str]
) -> dict:
    if public_launch_enabled:
        return {"access_allowed": True, "release_channel": "public"}
    if beta_mode_enabled and user_id in allowlist:
        return {"access_allowed": True, "release_channel": "invite_only_beta"}
    return {
        "access_allowed": False,
        "release_channel": "invite_only_beta" if beta_mode_enabled else "prelaunch",
    }
