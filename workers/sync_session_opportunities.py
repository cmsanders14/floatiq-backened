"""Validate and optionally publish session-aware market observations.

Dry-run is the default. The worker accepts only provider observations with an
explicit event ID and never infers that a broker supports a trading session.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError
from supabase import create_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from main import module as floatiq


def build_session_cache_payload(
    record: dict,
    source_name: str,
    *,
    now: datetime | None = None,
) -> dict | None:
    clean_source = source_name.strip()
    if not re.fullmatch(r"[A-Za-z0-9 ._-]{1,80}", clean_source):
        raise ValueError("Source name contains unsupported characters")
    event_id = str(record.get("source_event_id", "")).strip()
    if not event_id or len(event_id) > 200:
        raise ValueError("Each observation requires a source_event_id of 1 to 200 characters")
    if "broker_supports_session" in record:
        raise ValueError("Market-data observations cannot assert broker session support")

    candidate = floatiq.SessionOpportunityInput(**{
        key: value
        for key, value in record.items()
        if key in floatiq.SessionOpportunityInput.model_fields
    })
    effective_now = now or datetime.now(timezone.utc)
    scored = floatiq.score_session_opportunity(
        candidate,
        now=effective_now,
    )
    if scored["data_status"] != "fresh":
        return None
    return {
        "ticker": scored["ticker"],
        "asset_type": scored["asset_type"],
        "venue": scored["venue"],
        "price": scored["price"],
        "relative_volume": candidate.relative_volume,
        "average_dollar_volume": candidate.average_dollar_volume,
        "spread_bps": candidate.spread_bps,
        "price_change_pct": candidate.price_change_pct,
        "range_breakout_pct": candidate.range_breakout_pct,
        "vwap_distance_pct": candidate.vwap_distance_pct,
        "observed_at": candidate.observed_at.isoformat(),
        "received_at": (candidate.received_at or effective_now).isoformat(),
        "market_session": scored["market_session"],
        "provider_supports_session": candidate.provider_supports_session,
        "ranking_score": scored["ranking_score"],
        "score_components": scored["score_components"],
        "reason_codes": scored["reason_codes"],
        "algorithm_version": scored["algorithm_version"],
        "market_regime": scored["market_regime"],
        "regime_tags": scored["regime_tags"],
        "regime_basis": scored["regime_basis"],
        "source_name": clean_source,
        "source_event_id": event_id,
        "updated_at": effective_now.isoformat(),
    }


def read_jsonl(path: Path) -> list[dict]:
    records = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on line {line_number}: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"Line {line_number} must contain a JSON object")
        records.append(value)
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path, help="Paid-feed JSONL export")
    parser.add_argument("--source-name", required=True)
    parser.add_argument("--write", action="store_true", help="Upsert validated rows to Supabase")
    args = parser.parse_args()
    if not args.input.is_file():
        parser.error("--input must point to an existing JSONL file")

    database = None
    if args.write:
        if args.source_name.strip().lower() == "demo":
            parser.error("Demo observations cannot be written to Supabase")
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        if not url or not key:
            parser.error("--write requires SUPABASE_URL and SUPABASE_SERVICE_KEY")
        database = create_client(url, key)

    published = 0
    skipped_stale = 0
    failures = []
    for index, record in enumerate(read_jsonl(args.input), start=1):
        try:
            payload = build_session_cache_payload(record, args.source_name)
            if payload is None:
                skipped_stale += 1
                continue
            if database is None:
                print(json.dumps(payload, sort_keys=True))
            else:
                if payload["ticker"] == "DEMO":
                    raise ValueError("The DEMO ticker cannot be written to Supabase")
                database.table("session_opportunity_cache").upsert(
                    payload, on_conflict="ticker"
                ).execute()
            published += 1
        except (ValidationError, ValueError, TypeError) as exc:
            failures.append({"line": index, "error": str(exc)})

    print(json.dumps({
        "published": published,
        "skipped_stale": skipped_stale,
        "failures": failures,
        "write_enabled": args.write,
    }, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
