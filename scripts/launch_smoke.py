"""Read-only, bounded launch probe for local or deployed FloatIQ instances."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def fetch(base_url: str, path: str) -> tuple[int, dict, dict]:
    request = Request(base_url.rstrip("/") + path, headers={"User-Agent": "FloatIQ-Launch-Probe/1"})
    try:
        with urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
            return response.status, dict(response.headers), body
    except HTTPError as exc:
        return exc.code, dict(exc.headers), {"error": exc.read().decode("utf-8")[:300]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--requests", type=int, default=12)
    parser.add_argument(
        "--require-database",
        action="store_true",
        help="Fail when the readiness probe cannot complete its bounded database read.",
    )
    args = parser.parse_args()
    request_count = min(50, max(1, args.requests))
    failures = []
    try:
        for path in ("/health", "/ready", "/openapi.json"):
            status, headers, body = fetch(args.base_url, path)
            headers = {key.lower(): value for key, value in headers.items()}
            if status != 200:
                failures.append(f"{path} returned {status}")
            if headers.get("x-content-type-options") != "nosniff":
                failures.append(f"{path} is missing X-Content-Type-Options")
            serialized = json.dumps(body).lower()
            if any(prefix in serialized for prefix in ("sk_live_", "whsec_", "sb_secret_")):
                failures.append(f"{path} appears to expose a secret-named field")
            if (
                path == "/ready"
                and args.require_database
                and not body.get("database", {}).get("reachable", False)
            ):
                failures.append("/ready reports that the database is not reachable")

        with ThreadPoolExecutor(max_workers=min(8, request_count)) as pool:
            results = list(pool.map(lambda _index: fetch(args.base_url, "/health"), range(request_count)))
        unexpected = [status for status, _headers, _body in results if status != 200]
        if unexpected:
            failures.append(f"bounded health burst returned statuses {sorted(set(unexpected))}")
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        failures.append(f"probe could not reach or decode the service: {exc}")

    print(json.dumps({
        "base_url": args.base_url,
        "requests": request_count,
        "passed": not failures,
        "failures": failures,
    }, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
