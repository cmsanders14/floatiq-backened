"""Broker-neutral contracts for future FloatIQ integrations.

This module deliberately contains no live broker implementation.  Provider
credentials, OAuth callbacks, and order submission must be added behind these
contracts only after the provider and counsel approve the production workflow.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Protocol


class BrokerIntegrationError(ValueError):
    """Raised when a broker request cannot safely enter the order pipeline."""


class BrokerAdapter(Protocol):
    """Minimum surface every approved broker adapter must implement."""

    provider_key: str

    def authorization_url(self, state: str, redirect_uri: str) -> str: ...

    def list_accounts(self, connection_reference: str) -> list[dict[str, Any]]: ...

    def submit_order(
        self,
        connection_reference: str,
        account_reference: str,
        order: dict[str, Any],
        client_order_id: str,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class BrokerPlan:
    provider_key: str
    display_name: str
    integration_stage: str
    account_read: str = "planned"
    position_read: str = "planned"
    order_preview: str = "available_in_floatiq"
    live_order_submission: str = "not_implemented"


BROKER_PLANS = {
    "schwab": BrokerPlan(
        provider_key="schwab",
        display_name="Charles Schwab / thinkorswim",
        integration_stage="developer_approval_and_credentials_required",
    ),
    "webull": BrokerPlan(
        provider_key="webull",
        display_name="Webull",
        integration_stage="commercial_api_approval_and_credentials_required",
    ),
    "robinhood": BrokerPlan(
        provider_key="robinhood",
        display_name="Robinhood",
        integration_stage="provider_partnership_or_approved_access_required",
    ),
}

_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")


def normalize_broker_provider(provider: str) -> str:
    key = provider.strip().lower().replace("thinkorswim", "schwab")
    if key not in BROKER_PLANS:
        raise BrokerIntegrationError("Unsupported broker provider")
    return key


def validate_idempotency_key(value: str | None) -> str:
    if value is None or not _IDEMPOTENCY_KEY_RE.fullmatch(value.strip()):
        raise BrokerIntegrationError(
            "Idempotency-Key must contain 16-128 letters, numbers, dots, colons, underscores, or hyphens"
        )
    return value.strip()


def order_request_fingerprint(provider: str, payload: dict[str, Any]) -> str:
    """Create a stable digest used to prevent an idempotency key being reused."""
    canonical = json.dumps(
        {"provider": normalize_broker_provider(provider), "order": payload},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def broker_capability_manifest(
    *,
    connections_enabled: bool,
    paper_enabled: bool,
    live_enabled: bool,
    global_kill_switch: bool,
) -> dict[str, Any]:
    """Return honest runtime capability flags for the frontend and operators."""
    # No live adapter exists in this release.  An environment variable alone
    # must never turn order routing on.
    effective_live = False
    return {
        "connections_enabled": connections_enabled,
        "paper_trading_enabled": paper_enabled and connections_enabled,
        "live_order_submission_enabled": effective_live,
        "global_kill_switch_engaged": global_kill_switch,
        "providers": [asdict(plan) for plan in BROKER_PLANS.values()],
        "safety_notice": (
            "Live broker submission is not implemented. Provider approval, credentials, "
            "security review, counsel approval, and a registered adapter are all required."
        ),
        "requested_live_flag_ignored": bool(live_enabled),
    }


class BrokerAdapterRegistry:
    """Explicit registry prevents provider selection from dynamic imports."""

    def __init__(self) -> None:
        self._adapters: dict[str, BrokerAdapter] = {}

    def register(self, adapter: BrokerAdapter) -> None:
        provider = normalize_broker_provider(adapter.provider_key)
        if provider in self._adapters:
            raise BrokerIntegrationError(f"Adapter already registered for {provider}")
        self._adapters[provider] = adapter

    def get(self, provider: str) -> BrokerAdapter:
        key = normalize_broker_provider(provider)
        adapter = self._adapters.get(key)
        if adapter is None:
            raise BrokerIntegrationError(
                f"No approved live adapter is registered for {BROKER_PLANS[key].display_name}"
            )
        return adapter

    def registered_providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))


BROKER_ADAPTERS = BrokerAdapterRegistry()
