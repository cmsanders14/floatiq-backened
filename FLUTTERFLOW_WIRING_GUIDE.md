# FloatIQ FlutterFlow wiring guide

## Global API setup

- Base URL: `https://api.floatiqanalytics.com`
- Content type for POST/PUT calls: `application/json`
- Signed-in calls: `Authorization: Bearer <current Supabase access token>`
- Never send or store `SUPABASE_SERVICE_KEY` in FlutterFlow.
- Do not use a `user_id` query parameter for authorization. The backend uses the verified token.
- Treat HTTP `401` as signed out, `403` as an upgrade/entitlement result, `429` as retry later,
  and `502/503` as a temporary service/data problem.

## Required migration order

1. `000_foundation_schema.sql`
2. `001_product_tiers_and_alerts.sql`
3. `002_market_analog_profiles.sql`
4. `003_discipline_coach.sql`
5. `004_atomic_feature_usage.sql`
6. `005_subscription_lifecycle_and_timestamps.sql`
7. `006_supernova_radar.sql`
8. `007_broker_integration_foundation.sql`
9. `008_reconcile_legacy_schema_and_lock_down_data_api.sql`
10. `009_advisor_performance_cleanup.sql`
11. `010_billing_and_notification_delivery.sql`

## Pricing and dashboard limits

Call `GET /api/subscription-entitlements` at sign-in and after a subscription changes.

Use:

- `$.current_tier`
- `$.current_entitlements.max_charts`
- `$.current_entitlements.pattern_result_limit`
- `$.current_entitlements.win_rate_access`
- `$.current_entitlements.large_trade_alerts`
- `$.current_entitlements.supernova_radar`
- `$.current_entitlements.custom_supernova_thresholds`
- `$.current_entitlements.custom_setup_alerts`
- `$.current_entitlements.bracket_order_access`
- `$.current_entitlements.discipline_level`

FlutterFlow may hide locked controls for convenience, but FastAPI remains the security boundary.

## Billing

Call `GET /api/billing/status` after login and after returning from hosted checkout. Bind
`$.tier`, `$.subscription_status`, `$.checkout_enabled`, and `$.customer_portal_enabled`.

To upgrade, send only `{"tier":"premium_scanner"}` or
`{"tier":"autonomous_bot"}` to `POST /api/billing/checkout-session`, then open
`$.checkout_url`. Never add a Price ID or claimed payment status to the FlutterFlow request.
Returning from Stripe is not proof of payment: refresh billing status until the signed webhook
updates the entitlement. Open `$.portal_url` returned by `POST /api/billing/customer-portal` for
cancellation and payment-method management.

If checkout returns `503`, show "Billing is not available yet" and leave the current tier intact.

## Notification inbox

Call `GET /api/notifications?unread_only=false&limit=50`. Bind the list to `$.notifications` and
show `event_type`, `title`, `message`, `payload`, `created_at`, and the `read_at` state. Mark a row
read with `POST /api/notifications/<id>/read`; do not send a user ID. A `404` means the record does
not exist for that signed-in account.

In-app notifications require no device-provider key. Push and web-push switches should remain
hidden until the backend reports a configured delivery provider in a future contract.

## Pattern search

`GET /api/pattern-probabilities`

Parameters: `ticker`, `timeframe`, `chart_interval`, `chart_slot`, and optional
`target_return_pct`. A signed-in Free user taps a
locked result to repeat the call with that item's `pattern_key` as `reveal_pattern_key`. The same
selection remains available for that chart for the UTC day; another selection is rejected.

Bind each result card to:

- `pattern_key`
- `pattern_headline`
- `overall_probability_win_rate`
- `thirty_day_recent_probability`
- `sample_size_count`
- `confidence_interval_95`
- `small_sample_warning`
- `historical_adverse_drawdown`
- `mathematical_expectancy_score`
- `win_rate_locked`

Never replace a `null` locked win rate with a made-up number.

## Setup search

`GET /api/setup-search?setup=<text>&chart_slot=<number>`

Free receives three candidates; Pro and Elite receive ten. For a Free reveal, repeat with
`reveal_ticker=<symbol>`. Bind to `$.candidates` and use `win_rate_locked` to show the upgrade or
daily-limit state.

## Market alerts

`GET /api/market-alerts`

Display `institutional_scale_trade` separately from `verified_insider_purchase`. Never display
`actor_name` for an institutional-scale trade. Show a person's name only when the event is
`verified_insider_purchase` with `verification_status=verified_public_filing` and a source.

## Supernova Radar

`GET /api/scanners/supernova-radar` is automatically available to Pro and Elite. Do not create a
per-user scanner during signup: the authenticated tier check exposes the canonical preset. Bind
to `scanner`, `data_status`, and `candidates`. When `data_status=awaiting_live_feed`, show a neutral
connection state and never substitute demo symbols.

Candidate stages are `WATCHING`, `TRIGGERED`, and `CONFIRMED_MOMENTUM`. Confirmation requires a
later follow-through observation; never relabel a trigger as confirmed immediately. Elite may show
custom-threshold controls when `custom_thresholds_available=true`; Pro uses the default preset.
Elite loads and saves those controls through `GET` and `PUT /api/scanners/supernova-settings`.
The saved filters cover minimum score, relative volume, price expansion, dollar liquidity, spread,
direction, notification channels, and whether the alert is enabled.
The radar still displays fresh `WATCHING` candidates; use `matches_active_alert_filters` to decide
whether a candidate also qualifies for the user's notification settings.

The radar describes detected conditions, not a predicted price move or recommendation.

## Discipline Coach

- `GET /api/discipline/settings`
- `PUT /api/discipline/settings`
- `POST /api/discipline/evaluate-order`
- `POST /api/discipline/review-trade`
- `GET /api/discipline/reviews`
- `GET /api/discipline/summary`

Settings controls belong to the user. Label the four modes Monitor, Coach, Strict, and Locked.
Only show Locked when `hard_lock_available=true`.

Before an order preview, send account value, quantity, entry, stop, target, recorded daily P&L,
projected sector exposure, open positions, relative volume, setup-confirmation state, and cooldown
data. Display `violations` as conflicts with the user's rules. Do not rewrite them as FloatIQ
recommendations.

Use `discipline_gate_passed` as the workflow gate. Strict mode may repeat the evaluation with
`acknowledge_override=true`; Locked mode cannot be overridden inside FloatIQ.

The summary screen binds to:

- `average_discipline_score`
- `current_disciplined_streak`
- `process_classifications`
- `recurring_rule_conflicts`

## Order preview

`POST /api/orders/bracket-preview` is calculation-only. Always show the returned
`broker_order_submitted=false`. `/api/execute-autonomous-token` also remains unable to submit; send
`verification_code` and `discipline_evaluation_id` in its JSON body. It fails closed on missing or
stale evaluation data and then reports that a broker connection is required.

Legal-acceptance calls must include the SHA-256 hash of the exact displayed document as
`document_sha256`. Reusing a version label for different content is rejected.

## Analog search

Keep the screen hidden while `ANALOG_SEARCH_ENABLED=false`. When enabled, post a query such as
`find stocks that mirror TSLA before its breakout` to `/api/research/analog-search`. Show the
reference window, score, shared-feature count, similarities, differences, and prediction disclaimer.

## Demo states

Use `demo/demo_fixtures.json` only in FlutterFlow's local/demo state. Every fixture is labeled demo
and must never be inserted into the production market-alert feed.
