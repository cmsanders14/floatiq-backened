"""Validated scanner-cache writes. Importing or running this module never writes demo data."""

import os
import re
import math
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()


def get_supabase_client() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY are required for database writes")
    return create_client(url, key)


def build_scan_cache_payload(calculated_data: dict) -> dict:
    if calculated_data.get("status") != "success":
        raise ValueError("Scanner payload status must be success")
    required = {
        "ticker",
        "pattern",
        "live_price",
        "relative_volume_rvol",
        "gap_fill_prob",
    }
    missing = sorted(required.difference(calculated_data))
    if missing:
        raise ValueError(f"Scanner payload is missing: {', '.join(missing)}")

    ticker = str(calculated_data["ticker"]).strip().upper()
    if not re.fullmatch(r"[A-Z0-9.^-]{1,15}", ticker):
        raise ValueError("Ticker format is invalid")
    pattern = str(calculated_data["pattern"]).strip()
    if not pattern or len(pattern) > 100:
        raise ValueError("Pattern must contain 1 to 100 characters")
    price = float(calculated_data["live_price"])
    relative_volume = float(calculated_data["relative_volume_rvol"])
    historical_rate = float(calculated_data["gap_fill_prob"])
    if not all(math.isfinite(value) for value in (price, relative_volume, historical_rate)):
        raise ValueError("Scanner numeric metrics must be finite")
    if price <= 0:
        raise ValueError("Live price must be positive")
    if relative_volume < 0:
        raise ValueError("Relative volume cannot be negative")
    if not 0 <= historical_rate <= 100:
        raise ValueError("Historical rate must be between 0 and 100")

    return {
        "ticker": ticker,
        "pattern_detected": pattern,
        "current_price": price,
        "current_rvol": relative_volume,
        # Existing database column name retained for FlutterFlow compatibility.
        "gap_fill_probability": historical_rate,
        "market_session": str(calculated_data.get("market_session", "regular")),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def push_calculated_metrics_to_cloud(
    calculated_data: dict,
    database: Client | None = None,
) -> dict:
    """Validate and upsert one real scanner result; raise on failed persistence."""
    payload = build_scan_cache_payload(calculated_data)
    client = database or get_supabase_client()
    response = client.table("live_scan_cache").upsert(
        payload, on_conflict="ticker"
    ).execute()
    return {
        "status": "success",
        "ticker": payload["ticker"],
        "rows": response.data or [],
    }


if __name__ == "__main__":
    print(
        "database_sync.py is import-only. Pass a validated real scanner payload to "
        "push_calculated_metrics_to_cloud(); no demo data was written."
    )
