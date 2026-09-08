# FloatIQ Analytics Backend

FastAPI service that supplies historical candlestick-pattern statistics, position sizing,
market movers, subscription limits, journals, and morning briefs to the FloatIQ FlutterFlow app.

## Product tiers

The API is the source of truth for paid access; FlutterFlow must use these values for display,
but hiding a widget in FlutterFlow is not considered security.

| Tier | Price | Charts | Setup results | Win rates | Alerts and orders |
| --- | ---: | ---: | ---: | --- | --- |
| Free | $0 | 2 | 3 | One selected result per chart per UTC day | Position calculator |
| FloatIQ Pro | $19.99/month | 4 | 10 | All returned results | Trade alerts, Supernova Radar, and Discipline Coach |
| FloatIQ Elite | $39.99/month | 6 | 10 | All returned results | Supernova customization, custom alerts, bracket preview, and user-controlled locked mode |

Use `GET /api/subscription-entitlements` to populate plan cards and determine the signed-in
user's current limits. A Free user supplies `chart_slot` and `reveal_pattern_key` when choosing
the one result whose win rate will be shown for that chart that day. Guests must sign in before
a daily reveal can be recorded reliably.

## Local run

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn main:app --reload
```

Windows activation and start commands:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload
```

Health check: `GET /health`

## Environment variables

Copy `.env.example` to `.env` for local development. Configure the same values in Render:

- `SUPABASE_URL`: FloatIQ Supabase project URL.
- `SUPABASE_SERVICE_KEY`: Secret backend key. Never put it in GitHub or FlutterFlow.
- `CORS_ORIGINS`: Comma-separated production origins. `*` is suitable only during development.
- `MARKET_DATA_PROVIDER`: `yahoo` during limited testing; add a paid adapter before changing it.
- `RATE_LIMIT_ENABLED`: Enables the single-instance API limiter.
- `GENERAL_REQUESTS_PER_MINUTE`: Default `120` per client.
- `MARKET_DATA_REQUESTS_PER_MINUTE`: Default `30` per client for costly data endpoints.
- `MARKET_DATA_CACHE_MAX_ENTRIES`: Default `1000`; bounds process memory used by cached frames.
- `MARKET_DATA_CACHE_MAX_STALE_SECONDS`: Default `3600`; stale data is discarded after this age.
- `TRUST_PROXY_HEADERS`: Set only when the deployment proxy sanitizes `X-Forwarded-For`.
- `SUPERNOVA_FEED_ENABLED`: Keep `false` until the paid live-feed worker is populating fresh candidates.
- `SUPERNOVA_CACHE_MAX_AGE_SECONDS`: Reject cached Supernova candidates older than this; default `120`.

Recommended Render commands:

- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Health check path: `/health`

## Authentication

The public pattern endpoint works for guests and applies the free two-chart limit. A caller
cannot unlock a paid tier by supplying a `user_id` query parameter.

Authenticated requests must include the current Supabase access token:

```http
Authorization: Bearer <access-token>
```

The backend validates the token with Supabase and uses the verified user ID for subscription,
journal, morning-brief, and autonomous-tier lookups.

## FlutterFlow API contract

### Pattern probabilities

`GET /api/pattern-probabilities`

Query parameters:

- `ticker`: Stock symbol, such as `PLTR`.
- `timeframe`: `1d`, `5d`, `1mo`, `6mo`, `1y`, or `max`.
- `chart_interval`: `1m`, `5m`, `15m`, `1h`, or `1d`.
- `target_return_pct`: Directional intraday outcome threshold; defaults to `0.5`.

Important response paths:

- `$.ticker`
- `$.current_market_environment`
- `$.asset_categories`
- `$.market_cap_formatted`
- `$.max_allowed_charts`
- `$.patterns[:].pattern_headline`
- `$.patterns[:].overall_probability_win_rate`
- `$.patterns[:].thirty_day_recent_probability`
- `$.patterns[:].confluence_factor_rating`
- `$.patterns[:].sample_size_count`
- `$.patterns[:].confidence_interval_95`
- `$.patterns[:].small_sample_warning`
- `$.patterns[:].average_target_payout`
- `$.patterns[:].historical_adverse_drawdown`
- `$.patterns[:].mathematical_expectancy_score`
- `$.patterns[:].strategy_hot_badge`

When Yahoo data is temporarily unavailable, the response includes `data_status: unavailable`
and an empty `patterns` list. The frontend should display a temporary-data message instead of
claiming that no chart patterns exist.

### Other endpoints

- `GET /api/subscription-entitlements`
- `GET /api/setup-search`
- `POST /api/tools/large-trade-score` (Pro or Elite; provisional scoring pending live-feed calibration)
- `GET /api/tools/compounding-scenario` (educational paper-challenge math)
- `POST /api/research/analog-search` (hidden experimental historical-company comparison)
- `GET /api/scanners/premade-3pct-scalp`
- `GET /api/scanners/supernova-radar` (default Pro/Elite preset; returns no simulated candidates)
- `POST /api/tools/supernova-score` (transparent Pro/Elite scoring for supplied metrics)
- `GET /api/scanners/supernova-settings` (Elite saved alert thresholds)
- `PUT /api/scanners/supernova-settings` (Elite saved alert thresholds)
- `GET /api/calculate-position`
- `GET /api/market-movers`
- `GET /api/market-alerts` (Pro or Elite and authentication required)
- `GET /api/alert-rules` (Elite and authentication required)
- `POST /api/alert-rules` (Elite and authentication required)
- `POST /api/orders/bracket-preview` (Elite and authentication required; never submits an order)
- `GET /api/discipline/settings` (Pro or Elite)
- `PUT /api/discipline/settings` (Pro or Elite; locked mode is Elite-only)
- `POST /api/discipline/evaluate-order` (compares an order with user-authored rules)
- `POST /api/discipline/review-trade` (scores process separately from outcome)
- `GET /api/discipline/reviews`
- `GET /api/discipline/summary` (average score, disciplined streak, and recurring rule conflicts)
- `POST /api/legal/acceptances` (records the accepted document version; grants no trading authority)
- `GET /api/legal/acceptances`
- `GET /api/user-journal-summary` (authentication required)
- `GET /api/user-morning-brief` (authentication required)
- `POST /api/execute-autonomous-token` (Elite and authentication required; live submission remains disabled)

## Supabase migration

Run `migrations/000_foundation_schema.sql` first in a new Supabase project. It creates the core
subscription, scan-cache, journal, and ticker-history tables referenced by the original endpoints.

Run `migrations/001_product_tiers_and_alerts.sql` in the Supabase SQL editor before wiring the
new calls into FlutterFlow. It adds persistent daily usage, market alert events, and user alert
rules. These tables intentionally have Row Level Security enabled without direct-client access;
all access goes through the authenticated API so a modified FlutterFlow request cannot bypass a
paid tier.

`ALERT_DATA_CONTRACT.md` defines two evidence levels. An institutional-scale market print can be
confirmed from market data without identifying the buyer. A CEO or other insider is named only
when an authoritative public filing separately verifies that purchase.

Run `migrations/002_market_analog_profiles.sql` to add the precomputed feature profiles used by
the experimental analog search. Keep `ANALOG_SEARCH_ENABLED=false` in production until reference
eras are curated, the market universe has been populated, and result quality has been reviewed.
When enabled, the endpoint currently requires the Elite tier internally; that mapping can be
changed before launch without rewriting the comparison engine.

Run `migrations/003_discipline_coach.sql` to add Discipline Coach settings, evaluation history,
trade-process reviews, and versioned legal-acceptance records. Read
`PRODUCT_LANGUAGE_GUARDRAILS.md` before wiring labels or notifications into FlutterFlow. The
backend deliberately describes calculated values as user-supplied and does not claim that a
preview is a recommendation or that an unconnected broker order was submitted.

Run `migrations/004_atomic_feature_usage.sql` after migration 001. Its service-only database
function makes a Free user's daily chart reveal atomic, so two simultaneous requests cannot claim
different win rates.

Run `migrations/005_subscription_lifecycle_and_timestamps.sql` after migration 004. It adds payment-lifecycle
fields used to downgrade expired or inactive subscriptions and maintains mutable-table timestamps.

Run `migrations/006_supernova_radar.sql` after migration 005. It creates the service-only cache
for fresh Supernova candidates. Keep `SUPERNOVA_FEED_ENABLED=false` until a licensed feed supplies
time-adjusted relative volume, acceleration, VWAP, breakout, spread, and liquidity measurements.

Run `migrations/007_broker_integration_foundation.sql` after migration 006. It adds broker-neutral
connection references, idempotent order intents, and an append-only order-event audit trail. It
does not store broker tokens and does not enable live submission. Read
`BROKER_INTEGRATION_READINESS.md` before beginning any provider application or OAuth work.

The ingestion worker is also dry-run by default:

```bash
python workers/sync_supernova_candidates.py \
  --input demo/supernova_feed_sample.jsonl \
  --source-name demo
```

Only add `--write` for a real licensed-feed export after migration 006 and credential setup. Rows
classified as Normal or Suppressed are not published, invalid rows fail visibly, and no demo row
may be written to production.

## Analog profile worker

The worker is dry-run by default and prints profiles without changing Supabase:

```bash
python workers/build_analog_profiles.py --tickers TSLA AAPL NVDA
```

After migration 002 and environment configuration, add `--write` to upsert profiles. A curated
reference era also requires `--profile-type reference_era`, `--era-label`, `--start`, and `--end`.
Yahoo remains a limited test provider; do not run a 6,000-symbol production job until the paid
provider adapter and licensed universe are configured.

See `FLUTTERFLOW_WIRING_GUIDE.md` for the API calls, response paths, status handling, and screen
behavior. `demo/demo_fixtures.json` is local UI data and must never be inserted into production.

## Deployment security checklist

1. Create a replacement Supabase secret key.
2. Add the replacement to Render as `SUPABASE_SERVICE_KEY`.
3. Deploy and verify `/health` and `/api/pattern-probabilities`.
4. Revoke the previously exposed key in Supabase.
5. Commit the code that removes credentials from the repository.
6. Set `CORS_ORIGINS` to the production FlutterFlow domain.
7. Send the Supabase access token in the Authorization header for signed-in API calls.

The current Yahoo-backed data source is suitable for limited testing, but it can be rate-limited
and is not appropriate for scanning roughly 6,000 US stocks in real time. The planned paid market
data feed should replace it before production-scale scanning.

Pattern percentages are historical estimates based on the available sample and should not be
presented as guaranteed outcomes or standalone buy/sell signals.
