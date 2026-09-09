# FloatIQ Analytics Backend

FastAPI service that supplies historical candlestick-pattern statistics, position sizing,
market movers, subscription limits, journals, and morning briefs to the FloatIQ FlutterFlow app.

Production API: `https://api.floatiqanalytics.com`

The legacy Render hostname remains enabled as a fallback, but new client configuration should use
the branded API hostname.

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

Readiness/activation check: `GET /ready` (returns booleans only; never credential values)

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
- `BILLING_ENABLED`: Keep `false` until Stripe test-mode checkout and webhooks pass end to end.
- `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET`: Server-only Stripe credentials.
- `STRIPE_PRICE_PRO` / `STRIPE_PRICE_ELITE`: Server-owned recurring Price IDs; the client never supplies one.
- `BILLING_SUCCESS_URL`, `BILLING_CANCEL_URL`, `BILLING_PORTAL_RETURN_URL`: HTTPS app destinations.
- `ENABLE_HSTS`: Enable only after every public hostname is permanently HTTPS.

Recommended Render commands:

- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Health check path: `/health`

Production smoke test:

```bash
python scripts/launch_smoke.py \
  --base-url https://api.floatiqanalytics.com \
  --require-database
```

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
- `GET /api/billing/status` (effective tier and safe activation flags)
- `POST /api/billing/checkout-session` (authenticated; disabled until Stripe is configured)
- `POST /api/billing/customer-portal` (authenticated; hosted account management)
- `POST /api/billing/stripe/webhook` (Stripe only; signed raw body, no user authentication)
- `GET /api/notifications` (authenticated in-app inbox)
- `POST /api/notifications/{notification_id}/read` (ownership checked by verified token)
- `GET /api/setup-search`
- `POST /api/tools/large-trade-score` (Pro or Elite; provisional scoring pending live-feed calibration)
- `GET /api/tools/compounding-scenario` (educational paper-challenge math)
- `POST /api/research/analog-search` (hidden experimental historical-company comparison)
- `GET /api/scanners/premade-3pct-scalp`
# FloatIQ Analytics Backend

FastAPI service that supplies historical candlestick-pattern statistics, position sizing,
market movers, subscription limits, journals, and morning briefs to the FloatIQ FlutterFlow app.

Production API: `https://api.floatiqanalytics.com`

The legacy Render hostname remains enabled as a fallback, but new client configuration should use
the branded API hostname.

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

Readiness/activation check: `GET /ready` (returns booleans only; never credential values)

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
- `SESSION_SCANNER_FEED_ENABLED`: Keep `false` until feed calibration and circuit-breaker tests pass.
- `SESSION_SCANNER_CACHE_MAX_AGE_SECONDS`: Maximum fresh age for session observations; default `120`.
- `SCANNER_ALGORITHM_VERSION`: Immutable version label attached to every scored observation.
- `SUPERNOVA_ALGORITHM_VERSION`: Immutable version label attached to Supernova observations.
- `MINIMUM_WIN_RATE_SAMPLE_SIZE`: Evidence floor before a win rate is displayed; minimum/default `30`.
- `BETA_MODE_ENABLED`: Restricts the live session scanner to invited user IDs when `true`.
- `PUBLIC_LAUNCH_ENABLED`: Keep `false` throughout invite-only forward testing.
- `BETA_ALLOWLIST_USER_IDS`: Comma-separated Supabase user UUIDs; never returned by the API.
- `BILLING_ENABLED`: Keep `false` until Stripe test-mode checkout and webhooks pass end to end.
- `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET`: Server-only Stripe credentials.
- `STRIPE_PRICE_PRO` / `STRIPE_PRICE_ELITE`: Server-owned recurring Price IDs; the client never supplies one.
- `BILLING_SUCCESS_URL`, `BILLING_CANCEL_URL`, `BILLING_PORTAL_RETURN_URL`: HTTPS app destinations.
- `ENABLE_HSTS`: Enable only after every public hostname is permanently HTTPS.

Recommended Render commands:

- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Health check path: `/health`

Production smoke test:

```bash
python scripts/launch_smoke.py \
  --base-url https://api.floatiqanalytics.com \
  --require-database
```

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
- `GET /api/launch-status` (safe beta/public/feed/version flags; never returns the allowlist)
- `GET /api/accuracy/claim-readiness` (paid; enforces the minimum evidence floor)
- `POST /api/tools/market-regime` (paid; versioned deterministic regime classification)
- `GET /api/billing/status` (effective tier and safe activation flags)
- `POST /api/billing/checkout-session` (authenticated; disabled until Stripe is configured)
- `POST /api/billing/customer-portal` (authenticated; hosted account management)
- `POST /api/billing/stripe/webhook` (Stripe only; signed raw body, no user authentication)
- `GET /api/notifications` (authenticated in-app inbox)
- `POST /api/notifications/{notification_id}/read` (ownership checked by verified token)
- `GET /api/setup-search`
- `POST /api/tools/large-trade-score` (Pro or Elite; provisional scoring pending live-feed calibration)
- `GET /api/tools/compounding-scenario` (educational paper-challenge math)
- `POST /api/research/analog-search` (hidden experimental historical-company comparison)
- `GET /api/scanners/premade-3pct-scalp`
- `GET /api/scanners/supernova-radar` (default Pro/Elite preset; returns no simulated candidates)
- `POST /api/tools/supernova-score` (transparent Pro/Elite scoring for supplied metrics)
- `GET /api/scanners/session-opportunities` (session/freshness-aware paid scanner; live feed disabled by default)
- `POST /api/tools/session-opportunity-score` (transparent scoring for caller-supplied session metrics)
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

Run `migrations/008_reconcile_legacy_schema_and_lock_down_data_api.sql` and
`migrations/009_advisor_performance_cleanup.sql` after migration 007. They reconcile the legacy
project, remove direct Data API access to backend-owned tables, and clean up redundant indexes.

Run `migrations/010_billing_and_notification_delivery.sql` after migration 009. It adds a minimal
Stripe webhook audit ledger plus per-user notification events and retryable channel deliveries.
All three tables are service-only and have RLS enabled with no client policy.

Run `migrations/011_session_aware_scanner.sql` after migration 010. It adds service-only
instrument-session capabilities, fresh opportunity observations, and saved scanner presets. Keep
`SESSION_SCANNER_FEED_ENABLED=false` until a licensed feed supplies explicit timestamps and
session coverage. The API reports chartability, freshness, and broker-dependent tradability as
separate fields so a stale or merely chartable quote is never labeled tradable.

Run `migrations/012_accuracy_ledger_and_launch_controls.sql` after migration 011. It adds
append-only algorithm versions, market-regime snapshots, scanner-signal events, and outcome
observations. Application access is limited to service-role `SELECT` and `INSERT`, so old evidence
cannot be edited or deleted by the API. Each correction must be a new versioned observation.
Run `migrations/013_accuracy_ledger_index_cleanup.sql` afterward to cover correction-lineage
lookups and foreign-key maintenance efficiently.

Win rates remain hidden until `MINIMUM_WIN_RATE_SAMPLE_SIZE` qualifying examples exist. The API
still returns sample size and `statistical_claim_status=insufficient_sample`, allowing FlutterFlow
to explain why the percentage is unavailable without presenting an unstable statistic.

## Billing safety contract

- Checkout accepts only the canonical tier name; the server selects the Stripe Price ID.
- A plan changes only after a valid, timestamped Stripe webhook signature is verified.
- Webhook event IDs are stored uniquely, making normal Stripe retries idempotent.
- Because Stripe does not guarantee event order, each accepted event triggers a server-side read
  of the subscription's current state before any entitlement is changed.
- Unknown prices, missing user metadata, stale signatures, and unconfigured billing fail closed.
- Failed or canceled subscriptions fall back to Free through the existing entitlement check.
- Run Stripe CLI test-mode events before setting `BILLING_ENABLED=true`; never test with live cards.

## Notification delivery contract

The `notifications` row is the authenticated in-app inbox record. Each requested channel has one
`notification_deliveries` row with `pending`, `processing`, `delivered`, `failed`, or
`dead_letter` state. In-app delivery is immediately available; push, web push, and email remain
pending until a provider adapter and credential are explicitly enabled. Retry delays are bounded
and no provider failure can silently relabel a message delivered.

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

## Safe launch probe

With the server running, perform a small read-only concurrency check:

```bash
python scripts/launch_smoke.py --base-url http://127.0.0.1:8000 --requests 20
```

The probe checks health, readiness, OpenAPI, security headers, and a bounded burst. It never signs
in, mutates data, or calls a market-data endpoint.
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

Run `migrations/008_reconcile_legacy_schema_and_lock_down_data_api.sql` and
`migrations/009_advisor_performance_cleanup.sql` after migration 007. They reconcile the legacy
project, remove direct Data API access to backend-owned tables, and clean up redundant indexes.

Run `migrations/010_billing_and_notification_delivery.sql` after migration 009. It adds a minimal
Stripe webhook audit ledger plus per-user notification events and retryable channel deliveries.
All three tables are service-only and have RLS enabled with no client policy.

## Billing safety contract

- Checkout accepts only the canonical tier name; the server selects the Stripe Price ID.
- A plan changes only after a valid, timestamped Stripe webhook signature is verified.
- Webhook event IDs are stored uniquely, making normal Stripe retries idempotent.
- Because Stripe does not guarantee event order, each accepted event triggers a server-side read
  of the subscription's current state before any entitlement is changed.
- Unknown prices, missing user metadata, stale signatures, and unconfigured billing fail closed.
- Failed or canceled subscriptions fall back to Free through the existing entitlement check.
- Run Stripe CLI test-mode events before setting `BILLING_ENABLED=true`; never test with live cards.

## Notification delivery contract

The `notifications` row is the authenticated in-app inbox record. Each requested channel has one
`notification_deliveries` row with `pending`, `processing`, `delivered`, `failed`, or
`dead_letter` state. In-app delivery is immediately available; push, web push, and email remain
pending until a provider adapter and credential are explicitly enabled. Retry delays are bounded
and no provider failure can silently relabel a message delivered.

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

## Safe launch probe

With the server running, perform a small read-only concurrency check:

```bash
python scripts/launch_smoke.py --base-url http://127.0.0.1:8000 --requests 20
```

The probe checks health, readiness, OpenAPI, security headers, and a bounded burst. It never signs
in, mutates data, or calls a market-data endpoint.
