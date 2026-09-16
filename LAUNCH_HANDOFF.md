# FloatIQ at-home launch handoff

The code and database foundations are ready for an invite-only beta. Complete these owner-only
steps in order; do not enable public launch, paid live feeds, billing, or broker submission early.

## 1. Push the reviewed branch

The local branch is `codex-ui-polish` and contains the scanner-result polish plus deployment
preflight. Preserve the unrelated `database_sync.py` line-ending change when switching branches.

## 2. Replace the exposed Supabase credential

1. Create a new backend secret in Supabase.
2. Replace `SUPABASE_SERVICE_KEY` in Render.
3. Redeploy and confirm `/ready` reports the database reachable.
4. Revoke the old exposed key.

Never put the backend secret in FlutterFlow, GitHub, a screenshot, or a client API call.

## 3. Keep Render in safe beta mode

Use explicit HTTPS origins for `CORS_ORIGINS`, including the published FlutterFlow hostname and
the final FloatIQ web hostname. Keep these values until their corresponding integrations pass:

```text
BETA_MODE_ENABLED=true
PUBLIC_LAUNCH_ENABLED=false
SUPERNOVA_FEED_ENABLED=false
SESSION_SCANNER_FEED_ENABLED=false
BILLING_ENABLED=false
BROKER_LIVE_TRADING_ENABLED=false
BROKER_GLOBAL_KILL_SWITCH=true
RATE_LIMIT_ENABLED=true
```

Set `ENABLE_HSTS=true` only after every public hostname redirects to HTTPS. Before deploying,
run this against an uncommitted environment file:

```bash
python scripts/deployment_preflight.py --env-file .env --mode production
```

## 4. Verify the deployed backend

```bash
python scripts/launch_smoke.py \
  --base-url https://api.floatiqanalytics.com \
  --require-database
```

Then check `/api/launch-status`: it should say `invite_only_beta`, and both live-feed flags should
remain off until licensed data is connected and calibrated.

## 5. Finish FlutterFlow wiring and publish

1. Set the API base URL to `https://api.floatiqanalytics.com`.
2. Send `Authorization: Bearer <current Supabase access token>` on signed-in calls.
3. Bind tier/chart locks to `/api/subscription-entitlements`; never trust hidden widgets alone.
4. Bind the dashboard states and error handling in `FLUTTERFLOW_WIRING_GUIDE.md`.
5. Publish the web build, attach the final domain, and test desktop plus a real phone.

## 6. Run the beta objectively

Invite only specific Supabase user IDs. Keep algorithm versions fixed while collecting signal and
outcome rows. Do not display a win-rate claim below the configured sample floor, and compare
results by algorithm version, market regime, pattern, and horizon before changing thresholds.

## Still required before a paid public launch

- Licensed real-time market data and forward calibration across calm and volatile regimes.
- Stripe test-mode checkout, webhook, downgrade, cancellation, and retry tests.
- Push/web notification provider credentials and delivery monitoring.
- Attorney review of terms, privacy, disclosures, alerts, and any broker workflow.
- Uptime/error monitoring, backups, incident procedure, and a small invite-only load test.
- Separate broker approval, OAuth, paper testing, and legal review before any live order submission.
