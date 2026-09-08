# FloatIQ Broker Integration Readiness

## Current safety boundary

FloatIQ can calculate and record a user-confirmed order intent, but it cannot
submit an order. Setting an environment variable cannot enable live execution:
an approved provider adapter must also be deliberately registered in code.

## Work completed before provider access

- Broker-neutral provider contract and explicit adapter registry
- Honest capability manifest for FlutterFlow
- Stable request fingerprints and required idempotency keys
- Database design for connections, order intents, and append-only order events
- Opaque secret references instead of broker access tokens in application tables
- Global kill-switch configuration defaulting to engaged
- Existing bracket validation, user confirmation, and Discipline gate retained

## Steps requiring the founder and provider

1. Form the operating entity and have securities counsel approve the workflow.
2. Choose the first provider and apply for commercial developer access.
3. Register production and staging redirect URLs with the provider.
4. Receive client credentials and place them only in the deployment secret store.
5. Confirm permitted scopes, data display/redistribution rights, rate limits,
   certification tests, branding rules, and incident contacts in writing.
6. Implement OAuth with PKCE/state validation and an encrypted token vault.
7. Build the provider adapter against a sandbox or paper environment.
8. Reconcile acknowledgements, fills, partial fills, cancellations, and unknown
   states; never infer a fill from a timeout.
9. Complete security, load, failure-recovery, and counsel reviews.
10. Enable a small allowlisted pilot before considering wider live access.

## Provider order

Treat Charles Schwab and thinkorswim as one integration target. Select the first
provider based on approved commercial API access, sandbox quality, documentation,
and the broker most requested by beta users—not solely on brand size.

## Non-negotiable production controls

- Short-lived access tokens and encrypted refresh-token storage
- Exact redirect-URI allowlist and OAuth state/PKCE checks
- Per-user and global kill switches
- Idempotency on every create/cancel/replace request
- User-visible order review and final confirmation
- Position/buying-power refresh immediately before submission
- Complete immutable event trail with provider timestamps and references
- Reconciliation worker for ambiguous, delayed, and partial executions
- Alerts when provider state and FloatIQ state disagree
- No autonomous orders until separately approved by provider and counsel
