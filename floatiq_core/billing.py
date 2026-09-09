"""Fail-closed Stripe billing helpers.

The API layer owns authentication and database writes.  This module keeps price
mapping, signature verification, and subscription normalization deterministic so
they can be tested without Stripe credentials or network access.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


PAID_TIERS = {"premium_scanner", "autonomous_bot"}
ACTIVE_SUBSCRIPTION_STATUSES = {"active", "trialing"}
KNOWN_SUBSCRIPTION_STATUSES = {
    "active",
    "trialing",
    "past_due",
    "canceled",
    "incomplete",
    "unpaid",
}


class BillingConfigurationError(RuntimeError):
    """Raised when a billing operation is attempted before safe configuration."""


class WebhookSignatureError(ValueError):
    """Raised when a Stripe webhook signature cannot be verified."""


@dataclass(frozen=True)
class BillingConfig:
    enabled: bool
    secret_key: str | None
    webhook_secret: str | None
    pro_price_id: str | None
    elite_price_id: str | None
    success_url: str | None
    cancel_url: str | None
    portal_return_url: str | None

    @classmethod
    def from_env(cls) -> "BillingConfig":
        return cls(
            enabled=os.getenv("BILLING_ENABLED", "false").strip().lower() == "true",
            secret_key=os.getenv("STRIPE_SECRET_KEY"),
            webhook_secret=os.getenv("STRIPE_WEBHOOK_SECRET"),
            pro_price_id=os.getenv("STRIPE_PRICE_PRO"),
            elite_price_id=os.getenv("STRIPE_PRICE_ELITE"),
            success_url=os.getenv("BILLING_SUCCESS_URL"),
            cancel_url=os.getenv("BILLING_CANCEL_URL"),
            portal_return_url=os.getenv("BILLING_PORTAL_RETURN_URL"),
        )

    @property
    def price_to_tier(self) -> dict[str, str]:
        return {
            price: tier
            for price, tier in (
                (self.pro_price_id, "premium_scanner"),
                (self.elite_price_id, "autonomous_bot"),
            )
            if price
        }

    def price_for_tier(self, tier: str) -> str:
        if tier not in PAID_TIERS:
            raise BillingConfigurationError("Checkout is available only for paid plans")
        price = self.pro_price_id if tier == "premium_scanner" else self.elite_price_id
        if not price:
            raise BillingConfigurationError("The selected plan is not configured")
        return price

    def require_checkout(self) -> None:
        required = (
            self.enabled,
            self.secret_key,
            self.success_url,
            self.cancel_url,
            self.pro_price_id,
            self.elite_price_id,
        )
        if not all(required):
            raise BillingConfigurationError("Billing checkout is not configured")

    def require_webhook(self) -> None:
        if not self.enabled or not self.webhook_secret or not self.secret_key:
            raise BillingConfigurationError("Billing webhooks are not configured")


def verify_stripe_signature(
    payload: bytes,
    signature_header: str | None,
    secret: str,
    *,
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> dict[str, Any]:
    """Verify Stripe's timestamped v1 HMAC and return the decoded event."""
    if not signature_header:
        raise WebhookSignatureError("Missing Stripe-Signature header")
    parts: dict[str, list[str]] = {}
    for component in signature_header.split(","):
        key, separator, value = component.strip().partition("=")
        if separator:
            parts.setdefault(key, []).append(value)
    try:
        timestamp = int(parts["t"][0])
        signatures = parts["v1"]
    except (KeyError, IndexError, ValueError) as exc:
        raise WebhookSignatureError("Malformed Stripe-Signature header") from exc

    current_time = int(time.time()) if now is None else int(now)
    if abs(current_time - timestamp) > tolerance_seconds:
        raise WebhookSignatureError("Webhook timestamp is outside the allowed tolerance")
    signed_payload = str(timestamp).encode("ascii") + b"." + payload
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise WebhookSignatureError("Webhook signature verification failed")
    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookSignatureError("Webhook body is not valid JSON") from exc
    if not isinstance(event, dict) or not event.get("id") or not event.get("type"):
        raise WebhookSignatureError("Webhook event is missing required fields")
    return event


def _timestamp_to_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def subscription_row_from_stripe(
    subscription: dict[str, Any],
    config: BillingConfig,
    *,
    fallback_user_id: str | None = None,
) -> dict[str, Any]:
    """Build the only database fields Stripe is allowed to author."""
    subscription_id = str(subscription.get("id") or "").strip()
    customer_id = str(subscription.get("customer") or "").strip()
    metadata = subscription.get("metadata") or {}
    user_id = str(metadata.get("user_id") or fallback_user_id or "").strip()
    items = ((subscription.get("items") or {}).get("data") or [])
    price_id = None
    if items:
        price = items[0].get("price") or {}
        price_id = str(price.get("id") or "").strip() or None
    tier = config.price_to_tier.get(price_id or "")
    if not subscription_id or not customer_id or not user_id:
        raise ValueError("Subscription is missing its id, customer, or user metadata")
    if tier not in PAID_TIERS:
        raise ValueError("Subscription price is not mapped to a FloatIQ tier")
    raw_status = str(subscription.get("status") or "incomplete").strip().lower()
    status = raw_status if raw_status in KNOWN_SUBSCRIPTION_STATUSES else "incomplete"
    item_period_end = items[0].get("current_period_end") if items else None
    return {
        "user_id": user_id,
        "tier_level": tier,
        "max_charts": 4 if tier == "premium_scanner" else 6,
        "status": status,
        "current_period_end": _timestamp_to_iso(
            subscription.get("current_period_end") or item_period_end
        ),
        "provider_customer_id": customer_id,
        "provider_subscription_id": subscription_id,
    }


def event_is_newer(incoming_created_at: Any, saved_created_at: Any) -> bool:
    """Prevent out-of-order Stripe deliveries from restoring stale access."""
    try:
        incoming = int(incoming_created_at)
    except (TypeError, ValueError):
        return False
    if incoming <= 0:
        return False
    if saved_created_at in (None, ""):
        return True
    try:
        return incoming > int(saved_created_at)
    except (TypeError, ValueError):
        return True


def retry_delay_seconds(attempt_number: int, *, cap_seconds: int = 3600) -> int:
    """Bounded exponential retry delay shared by billing and delivery workers."""
    attempt = max(1, int(attempt_number))
    return min(cap_seconds, 30 * (2 ** (attempt - 1)))
