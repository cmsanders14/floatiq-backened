import hashlib
import hmac
import importlib.util
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from floatiq_core.billing import (
    BillingConfig,
    BillingConfigurationError,
    WebhookSignatureError,
    event_is_newer,
    retry_delay_seconds,
    subscription_row_from_stripe,
    verify_stripe_signature,
)
from floatiq_core.notifications import delivery_rows, next_delivery_state, normalize_channels


APP_PATH = Path(__file__).parents[1] / "FloatIQ Analytics.py"
SPEC = importlib.util.spec_from_file_location("floatiq_launch_app", APP_PATH)
floatiq = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(floatiq)
client = TestClient(floatiq.app)


class FakeResponse:
    def __init__(self, data):
        self.data = data


class StatefulQuery:
    def __init__(self, database, table):
        self.database = database
        self.table = table
        self.filters = []
        self.maximum = None
        self.change = None
        self.inserted = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def is_(self, key, value):
        self.filters.append((key, None if value == "null" else value))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, maximum):
        self.maximum = maximum
        return self

    def insert(self, value):
        self.inserted = value
        return self

    def update(self, value):
        self.change = value
        return self

    def _matching(self):
        rows = self.database.rows.setdefault(self.table, [])
        matching = [row for row in rows if all(row.get(key) == value for key, value in self.filters)]
        return matching[: self.maximum] if self.maximum is not None else matching

    def execute(self):
        if self.inserted is not None:
            values = self.inserted if isinstance(self.inserted, list) else [self.inserted]
            saved = []
            for value in values:
                row = {"id": str(uuid.uuid4()), **value}
                self.database.rows.setdefault(self.table, []).append(row)
                saved.append(row)
            return FakeResponse(saved)
        matching = self._matching()
        if self.change is not None:
            for row in matching:
                row.update(self.change)
        return FakeResponse([dict(row) for row in matching])


class StatefulSupabase:
    def __init__(self, rows=None):
        self.rows = rows or {}

    def table(self, name):
        return StatefulQuery(self, name)


class StripeObject(dict):
    def to_dict_recursive(self):
        return dict(self)


class FakeStripe:
    subscription = None

    class Subscription:
        @classmethod
        def retrieve(cls, _subscription_id):
            return StripeObject(FakeStripe.subscription)


def billing_config(**overrides):
    values = {
        "enabled": True,
        "secret_key": "sk_test_placeholder",
        "webhook_secret": "whsec_test",
        "pro_price_id": "price_pro",
        "elite_price_id": "price_elite",
        "success_url": "https://example.test/success",
        "cancel_url": "https://example.test/cancel",
        "portal_return_url": "https://example.test/account",
    }
    values.update(overrides)
    return BillingConfig(**values)


def signed_event(event, secret="whsec_test", timestamp=2_000_000_000):
    payload = json.dumps(event, separators=(",", ":")).encode()
    signature = hmac.new(
        secret.encode(), str(timestamp).encode() + b"." + payload, hashlib.sha256
    ).hexdigest()
    return payload, f"t={timestamp},v1={signature}"


def test_stripe_signature_accepts_authentic_payload_and_rejects_tampering():
    event = {"id": "evt_123", "type": "customer.subscription.updated", "data": {}}
    payload, header = signed_event(event)
    assert verify_stripe_signature(payload, header, "whsec_test", now=2_000_000_000) == event
    with pytest.raises(WebhookSignatureError):
        verify_stripe_signature(payload + b" ", header, "whsec_test", now=2_000_000_000)


def test_stripe_signature_rejects_replayed_timestamp():
    payload, header = signed_event({"id": "evt_1", "type": "test"}, timestamp=100)
    with pytest.raises(WebhookSignatureError, match="tolerance"):
        verify_stripe_signature(payload, header, "whsec_test", now=1000)


def test_subscription_event_maps_only_server_owned_price_ids():
    row = subscription_row_from_stripe(
        {
            "id": "sub_123",
            "customer": "cus_123",
            "status": "active",
            "current_period_end": 2_000_000_000,
            "metadata": {"user_id": "64fa2bd4-479e-42ca-a084-c86ec9f855f2"},
            "items": {"data": [{"price": {"id": "price_pro"}}]},
        },
        billing_config(),
    )
    assert row["tier_level"] == "premium_scanner"
    assert row["max_charts"] == 4
    assert row["status"] == "active"
    with pytest.raises(ValueError, match="not mapped"):
        subscription_row_from_stripe(
            {
                "id": "sub_bad",
                "customer": "cus_bad",
                "metadata": {"user_id": row["user_id"]},
                "items": {"data": [{"price": {"id": "client_supplied_price"}}]},
            },
            billing_config(),
        )


def test_subscription_period_uses_current_item_shape_and_events_must_be_newer():
    row = subscription_row_from_stripe(
        {
            "id": "sub_123",
            "customer": "cus_123",
            "status": "active",
            "metadata": {"user_id": "64fa2bd4-479e-42ca-a084-c86ec9f855f2"},
            "items": {"data": [{
                "price": {"id": "price_elite"},
                "current_period_end": 2_000_000_000,
            }]},
        },
        billing_config(),
    )
    assert row["tier_level"] == "autonomous_bot"
    assert row["current_period_end"] is not None
    assert event_is_newer(101, 100)
    assert not event_is_newer(99, 100)
    assert not event_is_newer(None, 100)


def test_billing_configuration_fails_closed():
    disabled = billing_config(enabled=False)
    with pytest.raises(BillingConfigurationError):
        disabled.require_checkout()
    with pytest.raises(BillingConfigurationError):
        disabled.require_webhook()


def test_notification_channels_deduplicate_and_in_app_delivers_immediately():
    assert normalize_channels(["IN_APP", "in_app", "push"]) == ["in_app", "push"]
    rows = delivery_rows("notification-id", ["in_app", "push"])
    assert rows[0]["status"] == "delivered"
    assert rows[0]["delivered_at"] is not None
    assert rows[1]["status"] == "pending"


def test_delivery_retry_is_bounded_and_dead_letters():
    fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    retry = next_delivery_state(2, 5, now=fixed_now, error="provider unavailable")
    assert retry["status"] == "failed"
    assert retry["attempt_count"] == 2
    assert retry["available_at"] > fixed_now.isoformat()
    dead = next_delivery_state(5, 5, now=fixed_now)
    assert dead["status"] == "dead_letter"
    assert retry_delay_seconds(99) == 3600


def test_readiness_and_security_headers_do_not_expose_secrets():
    response = client.get("/ready", headers={"X-Request-ID": "launch-check"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "launch-check"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    body = response.text.lower()
    assert "sk_test" not in body
    assert "whsec" not in body


def test_checkout_endpoint_is_inactive_without_configuration():
    with patch.object(floatiq, "get_authenticated_user_id", return_value="user-1"):
        response = client.post(
            "/api/billing/checkout-session",
            json={"tier": "premium_scanner"},
        )
    assert response.status_code == 503


def test_webhook_updates_existing_free_row_and_duplicate_is_idempotent():
    user_id = "64fa2bd4-479e-42ca-a084-c86ec9f855f2"
    database = StatefulSupabase({
        "user_subscriptions": [{
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "tier_level": "free",
            "provider_subscription_id": None,
            "provider_event_created_at": None,
        }],
        "billing_webhook_events": [],
    })
    FakeStripe.subscription = {
        "id": "sub_123",
        "customer": "cus_123",
        "status": "active",
        "metadata": {"user_id": user_id},
        "items": {"data": [{
            "price": {"id": "price_pro"},
            "current_period_end": int(time.time()) + 30 * 86400,
        }]},
    }
    event = {
        "id": "evt_123",
        "type": "customer.subscription.created",
        "created": int(time.time()),
        "data": {"object": FakeStripe.subscription},
    }
    payload, signature = signed_event(event, timestamp=int(time.time()))
    with patch.object(floatiq, "BILLING_CONFIG", billing_config()), patch.object(
        floatiq, "supabase", database
    ), patch.object(floatiq, "_load_stripe_module", return_value=FakeStripe):
        first = client.post(
            "/api/billing/stripe/webhook",
            content=payload,
            headers={"Stripe-Signature": signature, "Content-Type": "application/json"},
        )
        second = client.post(
            "/api/billing/stripe/webhook",
            content=payload,
            headers={"Stripe-Signature": signature, "Content-Type": "application/json"},
        )
    assert first.status_code == 200
    assert database.rows["user_subscriptions"][0]["tier_level"] == "premium_scanner"
    assert database.rows["user_subscriptions"][0]["provider_subscription_id"] == "sub_123"
    assert len(database.rows["user_subscriptions"]) == 1
    assert second.json()["duplicate"] is True
    assert len(database.rows["billing_webhook_events"]) == 1


def test_notification_inbox_filters_by_verified_user_and_read_checks_owner():
    mine = str(uuid.uuid4())
    theirs = str(uuid.uuid4())
    database = StatefulSupabase({
        "notifications": [
            {"id": mine, "user_id": "user-1", "title": "Mine", "read_at": None},
            {"id": theirs, "user_id": "user-2", "title": "Theirs", "read_at": None},
        ]
    })
    with patch.object(floatiq, "supabase", database), patch.object(
        floatiq, "get_authenticated_user_id", return_value="user-1"
    ):
        inbox = client.get("/api/notifications")
        forbidden = client.post(f"/api/notifications/{theirs}/read")
        read = client.post(f"/api/notifications/{mine}/read")
    assert [item["title"] for item in inbox.json()["notifications"]] == ["Mine"]
    assert forbidden.status_code == 404
    assert read.status_code == 200
    assert database.rows["notifications"][0]["read_at"] is not None
