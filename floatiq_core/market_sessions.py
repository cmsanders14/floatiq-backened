"""Provider-neutral market-session, freshness, and ranking helpers.

Schedule inference is intentionally labeled as an estimate. A production feed's
reported session is authoritative because exchange holidays and broker-specific
overnight access cannot be derived safely from a weekday clock alone.
"""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


ASSET_TYPES = {"equity", "etf", "fund", "crypto", "forex", "futures"}
MARKET_SESSIONS = {
    "pre_market",
    "regular",
    "after_hours",
    "overnight",
    "weekend",
    "continuous",
    "closed",
}
ACTIVE_SESSIONS = MARKET_SESSIONS - {"closed"}
_EASTERN = ZoneInfo("America/New_York")


def normalize_asset_type(value: str) -> str:
    normalized = value.strip().lower()
    aliases = {"stock": "equity", "common_stock": "equity", "cryptocurrency": "crypto"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in ASSET_TYPES:
        raise ValueError(f"Unsupported asset type: {value!r}")
    return normalized


def normalize_market_session(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"premarket": "pre_market", "afterhours": "after_hours", "24_7": "continuous"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in MARKET_SESSIONS:
        raise ValueError(f"Unsupported market session: {value!r}")
    return normalized


def require_aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(timezone.utc)


def infer_market_session(
    observed_at: datetime,
    asset_type: str,
    reported_session: str | None = None,
) -> dict:
    """Return a provider session or a clearly labeled US-schedule estimate."""
    observed_utc = require_aware_utc(observed_at, "observed_at")
    normalized_asset = normalize_asset_type(asset_type)
    eastern = observed_utc.astimezone(_EASTERN)
    is_weekend = eastern.weekday() >= 5

    if reported_session:
        return {
            "market_session": normalize_market_session(reported_session),
            "session_source": "provider",
            "schedule_estimate": False,
            "is_weekend": is_weekend,
        }

    if normalized_asset == "crypto":
        return {
            "market_session": "continuous",
            "session_source": "asset_default",
            "schedule_estimate": True,
            "is_weekend": is_weekend,
        }

    if normalized_asset in {"forex", "futures"}:
        # Venue calendars differ; without a provider-reported session FloatIQ
        # must not claim that the instrument is presently tradable.
        return {
            "market_session": "closed" if eastern.weekday() == 5 else "overnight",
            "session_source": "asset_default",
            "schedule_estimate": True,
            "is_weekend": is_weekend,
        }

    weekday = eastern.weekday()
    clock = eastern.time().replace(tzinfo=None)
    if weekday == 5 or (weekday == 6 and clock < time(20, 0)):
        session = "closed"
    elif weekday == 6:
        session = "overnight"
    elif time(4, 0) <= clock < time(9, 30):
        session = "pre_market"
    elif time(9, 30) <= clock < time(16, 0):
        session = "regular"
    elif time(16, 0) <= clock < time(20, 0):
        session = "after_hours"
    elif weekday == 4 and clock >= time(20, 0):
        session = "closed"
    else:
        session = "overnight"
    return {
        "market_session": session,
        "session_source": "us_equity_schedule_estimate",
        "schedule_estimate": True,
        "is_weekend": is_weekend,
    }


def assess_market_availability(
    *,
    observed_at: datetime,
    now: datetime,
    asset_type: str,
    reported_session: str | None,
    provider_supports_session: bool,
    broker_supports_session: bool | None,
    max_fresh_age_seconds: int = 120,
) -> dict:
    """Separate chartability, freshness, and broker-dependent tradability."""
    if max_fresh_age_seconds < 15 or max_fresh_age_seconds > 3600:
        raise ValueError("max_fresh_age_seconds must be between 15 and 3600")
    observed_utc = require_aware_utc(observed_at, "observed_at")
    now_utc = require_aware_utc(now, "now")
    if observed_utc > now_utc + timedelta(seconds=30):
        raise ValueError("observed_at cannot be more than 30 seconds in the future")
    age_seconds = max(0.0, (now_utc - observed_utc).total_seconds())
    session = infer_market_session(observed_utc, asset_type, reported_session)

    if age_seconds <= max_fresh_age_seconds:
        data_status = "fresh"
    elif age_seconds <= max_fresh_age_seconds * 4:
        data_status = "delayed"
    else:
        data_status = "stale"

    market_session = session["market_session"]
    if data_status != "fresh":
        tradability_status = "not_confirmed_stale_data"
    elif market_session == "closed":
        tradability_status = "market_closed"
    elif not provider_supports_session:
        tradability_status = "provider_session_unconfirmed"
    elif broker_supports_session is False:
        tradability_status = "broker_session_unsupported"
    elif broker_supports_session is None:
        tradability_status = "broker_support_unknown"
    else:
        tradability_status = "potentially_tradable"

    return {
        **session,
        "chartable": True,
        "data_status": data_status,
        "data_age_seconds": round(age_seconds, 1),
        "provider_supports_session": provider_supports_session,
        "broker_supports_session": broker_supports_session,
        "tradability_status": tradability_status,
        "potentially_tradable": tradability_status == "potentially_tradable",
    }


def rank_session_candidate(
    *,
    relative_volume: float,
    average_dollar_volume: float,
    spread_bps: float,
    price_change_pct: float,
    range_breakout_pct: float,
    vwap_distance_pct: float,
) -> dict:
    """Return a transparent 0-100 opportunity score from bounded components."""
    numbers = {
        "relative_volume": relative_volume,
        "average_dollar_volume": average_dollar_volume,
        "spread_bps": spread_bps,
        "price_change_pct": price_change_pct,
        "range_breakout_pct": range_breakout_pct,
        "vwap_distance_pct": vwap_distance_pct,
    }
    if any(not math.isfinite(float(value)) for value in numbers.values()):
        raise ValueError("Candidate metrics must be finite")
    if relative_volume < 0 or average_dollar_volume < 0 or spread_bps < 0:
        raise ValueError("Volume, liquidity, and spread metrics cannot be negative")

    components = {
        "relative_volume": min(100.0, relative_volume / 5.0 * 100.0),
        "liquidity": min(100.0, average_dollar_volume / 10_000_000 * 100.0),
        "spread_quality": max(0.0, 100.0 - spread_bps / 150.0 * 100.0),
        "price_expansion": min(100.0, abs(price_change_pct) / 5.0 * 100.0),
        "range_breakout": min(100.0, abs(range_breakout_pct) / 3.0 * 100.0),
        "vwap_distance": min(100.0, abs(vwap_distance_pct) / 3.0 * 100.0),
    }
    weights = {
        "relative_volume": 0.25,
        "liquidity": 0.20,
        "spread_quality": 0.15,
        "price_expansion": 0.15,
        "range_breakout": 0.15,
        "vwap_distance": 0.10,
    }
    score = round(sum(components[key] * weights[key] for key in weights), 1)
    ordered = sorted(components, key=components.get, reverse=True)
    reasons = [f"strong_{key}" for key in ordered[:3] if components[key] >= 60]
    if not reasons:
        reasons = ["no_strong_confluence"]
    return {
        "ranking_score": score,
        "score_components": {key: round(value, 1) for key, value in components.items()},
        "reason_codes": reasons,
        "ranking_method": "floatiq_session_opportunity_v1",
        "prediction_claimed": False,
    }
