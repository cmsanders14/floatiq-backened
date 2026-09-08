"""Build FloatIQ analog profiles. Writes require an explicit --write flag."""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from floatiq_core.analog_profiles import compute_market_profile
from floatiq_core.market_data import (
    MARKET_DATA_PROVIDER,
    download_market_data,
    download_market_data_range,
)


def read_tickers(args) -> list[str]:
    tickers = list(args.tickers or [])
    if args.ticker_file:
        tickers.extend(
            line.strip() for line in Path(args.ticker_file).read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    normalized = list(dict.fromkeys(ticker.upper() for ticker in tickers))
    invalid = [ticker for ticker in normalized if not re.fullmatch(r"[A-Z0-9.^-]{1,15}", ticker)]
    if invalid:
        raise ValueError(f"Invalid ticker(s): {', '.join(invalid)}")
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*")
    parser.add_argument("--ticker-file")
    parser.add_argument("--profile-type", choices=["current", "reference_era"], default="current")
    parser.add_argument("--era-label")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--period", default="1y")
    parser.add_argument("--interval", default="1d")
    parser.add_argument("--throttle-seconds", type=float, default=0.25)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        tickers = read_tickers(args)
    except ValueError as exc:
        parser.error(str(exc))
    if not tickers:
        parser.error("Supply --tickers or --ticker-file")
    if args.profile_type == "reference_era" and not (args.era_label and args.start and args.end):
        parser.error("Reference eras require --era-label, --start, and --end")
    if args.throttle_seconds < 0:
        parser.error("--throttle-seconds cannot be negative")

    database = None
    if args.write:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        if not url or not key:
            parser.error("--write requires SUPABASE_URL and SUPABASE_SERVICE_KEY")
        database = create_client(url, key)

    benchmark = (
        download_market_data_range("SPY", args.start, args.end, args.interval)
        if args.profile_type == "reference_era"
        else download_market_data("SPY", args.period, args.interval)
    )
    successes = 0
    failures = []
    for ticker in tickers:
        try:
            prices = (
                download_market_data_range(ticker, args.start, args.end, args.interval)
                if args.profile_type == "reference_era"
                else download_market_data(ticker, args.period, args.interval)
            )
            features = compute_market_profile(prices, benchmark)
            payload = {
                "ticker": ticker,
                "profile_type": args.profile_type,
                "profile_key": (
                    "current"
                    if args.profile_type == "current"
                    else re.sub(r"[^a-z0-9]+", "-", args.era_label.lower()).strip("-")
                ),
                "era_label": args.era_label,
                "window_start": str(prices.index.min().date()),
                "window_end": str(prices.index.max().date()),
                "features": features,
                "data_sources": [MARKET_DATA_PROVIDER.name],
            }
            if database is not None:
                database.table("market_analog_profiles").upsert(
                    payload,
                    on_conflict="ticker,profile_type,profile_key",
                ).execute()
            else:
                print(json.dumps(payload, sort_keys=True))
            successes += 1
        except Exception as exc:
            failures.append({"ticker": ticker, "error": str(exc)})
        if args.throttle_seconds:
            time.sleep(args.throttle_seconds)

    print(json.dumps({"successes": successes, "failures": failures}, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
