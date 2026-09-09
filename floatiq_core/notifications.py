"""Notification queue contracts that work without an external provider."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .billing import retry_delay_seconds


ALLOWED_CHANNELS = {"in_app", "push", "web", "email"}
DELIVERY_STATUSES = {"pending", "processing", "delivered", "failed", "dead_letter"}


def normalize_channels(channels: list[str]) -> list[str]:
    normalized = list(dict.fromkeys(str(channel).strip().lower() for channel in channels))
    if not normalized or any(channel not in ALLOWED_CHANNELS for channel in normalized):
        raise ValueError("Channels must be in_app, push, web, or email")
    return normalized


def next_delivery_state(
    attempt_count: int,
    max_attempts: int,
    *,
    now: datetime | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Return the deterministic failed/dead-letter transition for a delivery."""
    attempt = max(1, int(attempt_count))
    maximum = max(1, int(max_attempts))
    current = now or datetime.now(timezone.utc)
    clean_error = (error or "Delivery provider returned an error")[:500]
    if attempt >= maximum:
        return {
            "status": "dead_letter",
            "attempt_count": attempt,
            "available_at": current.isoformat(),
            "last_error": clean_error,
            "locked_at": None,
        }
    delay = retry_delay_seconds(attempt)
    return {
        "status": "failed",
        "attempt_count": attempt,
        "available_at": (current + timedelta(seconds=delay)).isoformat(),
        "last_error": clean_error,
        "locked_at": None,
    }


def notification_event_row(
    *,
    user_id: str,
    event_key: str,
    event_type: str,
    title: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not user_id or not event_key.strip() or not event_type.strip():
        raise ValueError("A notification requires user, event key, and event type")
    if not title.strip() or not message.strip():
        raise ValueError("A notification requires a title and message")
    return {
        "user_id": user_id,
        "event_key": event_key.strip()[:200],
        "event_type": event_type.strip()[:80],
        "title": title.strip()[:160],
        "message": message.strip()[:1000],
        "payload": payload or {},
    }


def delivery_rows(event_id: str, channels: list[str], *, max_attempts: int = 5) -> list[dict]:
    return [
        {
            "notification_id": event_id,
            "channel": channel,
            "status": "delivered" if channel == "in_app" else "pending",
            "attempt_count": 0,
            "max_attempts": max(1, int(max_attempts)),
            "delivered_at": datetime.now(timezone.utc).isoformat() if channel == "in_app" else None,
        }
        for channel in normalize_channels(channels)
    ]
