"""Validate and optionally publish paid-feed Supernova observations.

Dry-run is the default. Each JSONL row must contain a unique ``source_event_id`` plus
the fields accepted by ``SupernovaCandidateInput``.
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


def build_supernova_cache_payload(record: dict, source_name: str) -> dict | None:
    clean_source = source_name.strip()
    if not clean_source or len(clean_source) > 80:
        raise ValueError("Source name must contain 1 to 80 characters")
    event_id = str(record.get("source_event_id", "")).strip()
    if not event_id or len(event_id) > 200:
        raise ValueError("Each observation requires a source_event_id of 1 to 200 characters")

    candidate_fields = set(floatiq.SupernovaCandidateInput.model_fields)
    candidate = floatiq.SupernovaCandidateInput(**{
        key: value for key, value in record.items() if key in candidate_fields
    })
    result = floatiq.score_supernova_candidate(candidate)
    if result["stage"] in {"NORMAL", "SUPPRESSED"}:
        return None
    metrics = result["metrics"]
    return {
        "ticker": result["ticker"],
        "stage": result["stage"],
        "score": result["score"],
        "direction": result["direction"],
        "price": candidate.price,
        "relative_volume_at_time": candidate.relative_volume_at_time,
        "volume_acceleration": candidate.volume_acceleration,
        "price_change_pct": candidate.price_change_pct,
        "range_breakout_pct": candidate.range_breakout_pct,
        "vwap_distance_pct": candidate.vwap_distance_pct,
        "average_dollar_volume": candidate.average_dollar_volume,
        "spread_bps": candidate.spread_bps,
        "float_turnover_pct": candidate.float_turnover_pct,
        "follow_through_confirmed": candidate.follow_through_confirmed,
        "market_session": metrics["market_session"].strip().lower(),
        "score_components": result["components"],
        "source_name": clean_source,
        "source_event_id": event_id,
        "detected_at": record.get("detected_at") or datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
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
    parser.add_argument("--write", action="store_true", help="Upsert validated candidates to Supabase")
    args = parser.parse_args()
    if not args.input.is_file():
        parser.error("--input must point to an existing JSONL file")
    if not re.fullmatch(r"[A-Za-z0-9 ._-]{1,80}", args.source_name.strip()):
        parser.error("--source-name contains unsupported characters")

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
    skipped = 0
    failures = []
    for index, record in enumerate(read_jsonl(args.input), start=1):
        try:
            payload = build_supernova_cache_payload(record, args.source_name)
            if payload is None:
                skipped += 1
                continue
            if database is None:
                print(json.dumps(payload, sort_keys=True))
            else:
                if payload["ticker"] == "DEMO":
                    raise ValueError("The DEMO ticker cannot be written to Supabase")
                database.table("supernova_scan_cache").upsert(
                    payload, on_conflict="ticker"
                ).execute()
            published += 1
        except (ValidationError, ValueError, TypeError) as exc:
            failures.append({"line": index, "error": str(exc)})

    print(json.dumps({
        "published": published,
        "skipped_normal_or_suppressed": skipped,
        "failures": failures,
        "write_enabled": args.write,
    }, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
