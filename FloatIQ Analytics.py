import logging
import json
import math
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated
import pandas as pd
import pandas_ta_classic as ta
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Query, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from supabase import create_client, Client

from floatiq_core.entitlements import TIER_CONFIG, TIER_WEIGHTS, tier_entitlements
from floatiq_core.billing import (
    BillingConfig,
    BillingConfigurationError,
    WebhookSignatureError,
    event_is_newer,
    subscription_row_from_stripe,
    verify_stripe_signature,
)
from floatiq_core.brokers import (
    BrokerIntegrationError,
    broker_capability_manifest,
    normalize_broker_provider,
    order_request_fingerprint,
    validate_idempotency_key,
)
from floatiq_core.market_data import _MARKET_DATA_CACHE, download_market_data
from floatiq_core.notifications import delivery_rows, notification_event_row, normalize_channels
from floatiq_core.rate_limit import SlidingWindowRateLimiter
logger = logging.getLogger("floatiq")
logger.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))
if not logger.handlers:
    _log_handler = logging.StreamHandler()
    _log_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_log_handler)
logger.propagate = False
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://wupivkrdqgrzogdaoueu.supabase.co")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

# Keep public market-data endpoints available even when database credentials are
# not configured. Database-backed features safely fall back to the free tier.
supabase: Client | None = (
    create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    if SUPABASE_SERVICE_KEY
    else None
)
ANALOG_SEARCH_ENABLED = os.getenv("ANALOG_SEARCH_ENABLED", "false").strip().lower() == "true"
SUPERNOVA_FEED_ENABLED = os.getenv("SUPERNOVA_FEED_ENABLED", "false").strip().lower() == "true"
SUPERNOVA_CACHE_MAX_AGE_SECONDS = max(
    30, int(os.getenv("SUPERNOVA_CACHE_MAX_AGE_SECONDS", "120"))
)
BROKER_CONNECTIONS_ENABLED = os.getenv("BROKER_CONNECTIONS_ENABLED", "false").strip().lower() == "true"
BROKER_PAPER_TRADING_ENABLED = os.getenv("BROKER_PAPER_TRADING_ENABLED", "false").strip().lower() == "true"
BROKER_LIVE_TRADING_ENABLED = os.getenv("BROKER_LIVE_TRADING_ENABLED", "false").strip().lower() == "true"
BROKER_GLOBAL_KILL_SWITCH = os.getenv("BROKER_GLOBAL_KILL_SWITCH", "true").strip().lower() == "true"
BILLING_CONFIG = BillingConfig.from_env()
ENABLE_HSTS = os.getenv("ENABLE_HSTS", "false").strip().lower() == "true"


app = FastAPI(title="FloatIQ Analytics Engine", version="3.0.0")

RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").strip().lower() == "true"
GENERAL_REQUESTS_PER_MINUTE = max(1, int(os.getenv("GENERAL_REQUESTS_PER_MINUTE", "120")))
MARKET_DATA_REQUESTS_PER_MINUTE = max(1, int(os.getenv("MARKET_DATA_REQUESTS_PER_MINUTE", "30")))
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "false").strip().lower() == "true"
_RATE_LIMITER = SlidingWindowRateLimiter()
_COSTLY_PATHS = {
    "/api/pattern-probabilities",
    "/api/scanners/premade-3pct-scalp",
    "/api/scanners/supernova-radar",
    "/api/market-movers",
    "/api/setup-search",
    "/api/research/analog-search",
}


@app.middleware("http")
async def add_request_context_and_security_headers(request: Request, call_next):
    """Attach a traceable request ID, safe browser headers, and one structured log."""
    request_id = request.headers.get("x-request-id", "").strip()[:100] or str(uuid.uuid4())
    started = time.perf_counter()
    response = None
    try:
        response = await call_next(request)
        return response
    finally:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        status_code = response.status_code if response is not None else 500
        logger.info(json.dumps({
            "event": "http_request",
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": status_code,
            "duration_ms": elapsed_ms,
        }, separators=(",", ":")))
        if response is not None:
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            if ENABLE_HSTS:
                response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"


@app.middleware("http")
async def enforce_rate_limit(request: Request, call_next):
    if not RATE_LIMIT_ENABLED or request.url.path in {"/", "/health", "/docs", "/openapi.json"}:
        return await call_next(request)
    direct_host = request.client.host if request.client else "unknown"
    forwarded_host = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    client_key = forwarded_host if TRUST_PROXY_HEADERS and forwarded_host else direct_host
    costly = request.url.path in _COSTLY_PATHS
    group = "market_data" if costly else "general"
    limit = MARKET_DATA_REQUESTS_PER_MINUTE if costly else GENERAL_REQUESTS_PER_MINUTE
    allowed, retry_after = _RATE_LIMITER.check(f"{group}:{client_key}", limit)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": "Request limit reached. Please retry shortly."},
            headers={"Retry-After": str(retry_after), "X-RateLimit-Limit": str(limit)},
        )
    response = await call_next(request)
    response.headers["X-RateLimit-Limit"] = str(limit)
    return response

CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "*").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=CORS_ORIGINS != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
@app.get("/health")
def health_check():
    """Lightweight endpoint for Render health checks and deployment verification."""
    return {
        "status": "ok",
        "service": "FloatIQ Analytics Engine",
        "version": app.version,
        "database_configured": supabase is not None,
    }


@app.head("/", include_in_schema=False)
@app.head("/health", include_in_schema=False)
def health_check_head():
    """Accept lightweight platform and uptime probes without a response body."""
    return Response(status_code=200)


def database_connection_ready() -> bool:
    """Run a bounded read so readiness reflects connectivity, not only configuration."""
    if supabase is None:
        return False
    try:
        supabase.table("pattern_metadata").select("id").limit(1).execute()
        return True
    except Exception:
        logger.warning(json.dumps({"event": "database_readiness_failed"}))
        return False


@app.get("/ready")
def readiness_check():
    """Report activation state without exposing credentials or provider identifiers."""
    database_configured = supabase is not None
    database_ready = database_connection_ready()
    return {
        "status": "ready" if database_ready else "degraded",
        "service": "FloatIQ Analytics Engine",
        "database": {
            "configured": database_configured,
            "reachable": database_ready,
        },
        "billing": {
            "enabled": BILLING_CONFIG.enabled,
            "checkout_configured": all((
                BILLING_CONFIG.enabled,
                BILLING_CONFIG.secret_key,
                BILLING_CONFIG.pro_price_id,
                BILLING_CONFIG.elite_price_id,
                BILLING_CONFIG.success_url,
                BILLING_CONFIG.cancel_url,
            )),
            "webhook_configured": bool(
                BILLING_CONFIG.enabled
                and BILLING_CONFIG.webhook_secret
                and BILLING_CONFIG.secret_key
            ),
        },
        "notifications": {
            "in_app_ready": database_ready,
            "external_delivery_enabled": False,
        },
        "broker_execution": {
            "live_enabled": BROKER_LIVE_TRADING_ENABLED and not BROKER_GLOBAL_KILL_SWITCH,
            "global_kill_switch": BROKER_GLOBAL_KILL_SWITCH,
        },
    }


def get_authenticated_user_id(authorization: str | None) -> str | None:
    """Validate a Supabase access token and return its user ID."""
    if not authorization:
        return None
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    if supabase is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    try:
        response = supabase.auth.get_user(token.strip())
        user = getattr(response, "user", None)
        if user is None or not getattr(user, "id", None):
            raise HTTPException(status_code=401, detail="Invalid or expired access token")
        return str(user.id)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired access token")


def check_user_tier_permissions(user_id: str, required_tier: str) -> dict:
    """Helper method to validate user subscription tier rules against endpoints."""
    try:
        if supabase is None or user_id == "guest_user":
            return {
                **tier_entitlements("free"),
                "subscription_status": "guest",
                "is_authorized": required_tier == "free",
            }
        res = (
            supabase.table("user_subscriptions")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not res.data:
            return {
                **tier_entitlements("free"),
                "subscription_status": "none",
                "is_authorized": required_tier == "free",
            }
        subscription = res.data[0]
        subscription_status = str(subscription.get("status", "active")).strip().lower()
        period_end = subscription.get("current_period_end")
        period_is_current = True
        if period_end:
            parsed_end = datetime.fromisoformat(str(period_end).replace("Z", "+00:00"))
            if parsed_end.tzinfo is None:
                parsed_end = parsed_end.replace(tzinfo=timezone.utc)
            period_is_current = parsed_end > datetime.now(timezone.utc)
        paid_access_active = subscription_status in {"active", "trialing"} and period_is_current
        user_tier = subscription.get("tier_level", "free") if paid_access_active else "free"
        entitlements = tier_entitlements(user_tier)
        is_authorized = TIER_WEIGHTS.get(entitlements["tier"], 0) >= TIER_WEIGHTS.get(required_tier, 0)
        return {
            **entitlements,
            "subscription_status": subscription_status if period_is_current else "expired",
            "is_authorized": is_authorized,
        }
    except Exception:
        logger.exception("Unable to read subscription tier for user %s", user_id)
        return {
            **tier_entitlements("free"),
            "subscription_status": "unavailable",
            "is_authorized": required_tier == "free",
        }


def claim_daily_win_rate_reveal(user_id: str, chart_slot: int, pattern_key: str) -> bool:
    """Reserve one stable Free-tier reveal for a chart slot for the UTC day."""
    if supabase is None or user_id == "guest_user":
        return False
    usage_day = date.today().isoformat()
    resource_key = f"chart:{chart_slot}"
    try:
        response = supabase.rpc("claim_daily_feature_selection", {
            "p_user_id": user_id,
            "p_usage_date": usage_day,
            "p_feature_name": "win_rate_reveal",
            "p_resource_key": resource_key,
            "p_selected_key": pattern_key,
        }).execute()
        result = response.data
        if isinstance(result, list):
            result = result[0] if result else False
        return result is True
    except Exception:
        logger.exception("Unable to reserve daily win-rate reveal")
        return False


def apply_pattern_entitlements(
    patterns: list[dict],
    permissions: dict,
    user_id: str,
    chart_slot: int,
    reveal_pattern_key: str | None,
) -> list[dict]:
    """Limit search results and redact premium statistics for Free users."""
    limited = patterns[: permissions["pattern_result_limit"]]
    if permissions["win_rate_access"] == "all":
        return [{**item, "win_rate_locked": False} for item in limited]

    reveal_allowed = False
    if reveal_pattern_key and any(item["pattern_key"] == reveal_pattern_key for item in limited):
        reveal_allowed = claim_daily_win_rate_reveal(user_id, chart_slot, reveal_pattern_key)

    protected = []
    for item in limited:
        visible = reveal_allowed and item["pattern_key"] == reveal_pattern_key
        result = {**item, "win_rate_locked": not visible}
        if not visible:
            result["overall_probability_win_rate"] = None
            result["thirty_day_recent_probability"] = None
            result["historical_win_rate_pct"] = None
            result["confidence_interval_95"] = None
        protected.append(result)
    return protected


def wilson_confidence_interval(successes: int, total: int, z: float = 1.96) -> dict | None:
    """Return a bounded Wilson interval for an observed binomial rate."""
    if total <= 0 or successes < 0 or successes > total:
        return None
    proportion = successes / total
    denominator = 1 + (z * z / total)
    center = (proportion + (z * z / (2 * total))) / denominator
    margin = (
        z
        * math.sqrt((proportion * (1 - proportion) / total) + (z * z / (4 * total * total)))
        / denominator
    )
    return {
        "low_pct": round(max(0.0, center - margin) * 100, 2),
        "high_pct": round(min(1.0, center + margin) * 100, 2),
    }


@app.get("/api/subscription-entitlements")
def get_subscription_entitlements(
    authorization: Annotated[str | None, Header()] = None,
):
    """Expose the canonical plans and the signed-in user's effective plan."""
    verified_user_id = get_authenticated_user_id(authorization)
    current = check_user_tier_permissions(verified_user_id or "guest_user", "free")
    return {
        "current_tier": current["tier"],
        "current_entitlements": {k: v for k, v in current.items() if k != "is_authorized"},
        "plans": [tier_entitlements(tier) for tier in TIER_CONFIG],
    }


def require_authenticated_tier(authorization: str | None, required_tier: str) -> tuple[str, dict]:
    user_id = get_authenticated_user_id(authorization)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    permissions = check_user_tier_permissions(user_id, required_tier)
    if not permissions["is_authorized"]:
        raise HTTPException(status_code=403, detail="This feature is not included in your subscription tier")
    return user_id, permissions


class BillingCheckoutInput(BaseModel):
    tier: str = Field(pattern=r"^(premium_scanner|autonomous_bot)$")


def _load_stripe_module():
    try:
        import stripe
    except ImportError as exc:
        raise BillingConfigurationError("Stripe support is not installed") from exc
    stripe.api_key = BILLING_CONFIG.secret_key
    return stripe


@app.get("/api/billing/status")
def get_billing_status(
    authorization: Annotated[str | None, Header()] = None,
):
    """Return effective access and safe billing activation flags for the signed-in user."""
    user_id = get_authenticated_user_id(authorization)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    permissions = check_user_tier_permissions(user_id, "free")
    return {
        "tier": permissions["tier"],
        "subscription_status": permissions["subscription_status"],
        "checkout_enabled": BILLING_CONFIG.enabled,
        "customer_portal_enabled": bool(
            BILLING_CONFIG.enabled
            and BILLING_CONFIG.secret_key
            and BILLING_CONFIG.portal_return_url
        ),
    }


@app.post("/api/billing/checkout-session")
def create_billing_checkout_session(
    checkout: BillingCheckoutInput,
    authorization: Annotated[str | None, Header()] = None,
):
    """Create a hosted Stripe subscription checkout using server-owned price IDs."""
    user_id = get_authenticated_user_id(authorization)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        BILLING_CONFIG.require_checkout()
        price_id = BILLING_CONFIG.price_for_tier(checkout.tier)
        stripe = _load_stripe_module()
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=BILLING_CONFIG.success_url,
            cancel_url=BILLING_CONFIG.cancel_url,
            client_reference_id=user_id,
            metadata={"user_id": user_id, "requested_tier": checkout.tier},
            subscription_data={"metadata": {"user_id": user_id}},
        )
        return {"checkout_url": session.url, "session_id": session.id}
    except BillingConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception:
        logger.exception("Stripe checkout creation failed for %s", user_id)
        raise HTTPException(status_code=502, detail="Billing checkout is temporarily unavailable")


@app.post("/api/billing/customer-portal")
def create_billing_portal_session(
    authorization: Annotated[str | None, Header()] = None,
):
    """Create a hosted Stripe customer-portal session without exposing customer IDs."""
    user_id = get_authenticated_user_id(authorization)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not (
        BILLING_CONFIG.enabled
        and BILLING_CONFIG.secret_key
        and BILLING_CONFIG.portal_return_url
    ):
        raise HTTPException(status_code=503, detail="The billing portal is not configured")
    try:
        subscription = (
            supabase.table("user_subscriptions")
            .select("provider_customer_id")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        customer_id = (
            subscription.data[0].get("provider_customer_id")
            if subscription.data else None
        )
        if not customer_id:
            raise HTTPException(status_code=404, detail="No billing customer is associated with this account")
        stripe = _load_stripe_module()
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=BILLING_CONFIG.portal_return_url,
        )
        return {"portal_url": session.url}
    except HTTPException:
        raise
    except BillingConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception:
        logger.exception("Stripe portal creation failed for %s", user_id)
        raise HTTPException(status_code=502, detail="The billing portal is temporarily unavailable")


@app.post("/api/billing/stripe/webhook")
async def receive_stripe_webhook(
    request: Request,
    stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
):
    """Verify, deduplicate, and apply subscription lifecycle events from Stripe."""
    try:
        BILLING_CONFIG.require_webhook()
        event = verify_stripe_signature(
            await request.body(),
            stripe_signature,
            BILLING_CONFIG.webhook_secret or "",
        )
    except BillingConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except WebhookSignatureError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if supabase is None:
        raise HTTPException(status_code=503, detail="Database is not configured")

    event_id = str(event["id"])
    event_type = str(event["type"])
    supported = {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }
    try:
        existing = (
            supabase.table("billing_webhook_events")
            .select("processing_status")
            .eq("provider", "stripe")
            .eq("provider_event_id", event_id)
            .limit(1)
            .execute()
        )
        if existing.data and existing.data[0].get("processing_status") in {"processed", "ignored"}:
            return {"received": True, "duplicate": True}

        if not existing.data:
            supabase.table("billing_webhook_events").insert({
                "provider": "stripe",
                "provider_event_id": event_id,
                "event_type": event_type,
                "processing_status": "processing",
            }).execute()

        if event_type not in supported:
            (
                supabase.table("billing_webhook_events")
                .update({
                    "processing_status": "ignored",
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                })
                .eq("provider", "stripe")
                .eq("provider_event_id", event_id)
                .execute()
            )
            return {"received": True, "ignored": True}

        subscription = ((event.get("data") or {}).get("object") or {})
        subscription_id = str(subscription.get("id") or "")
        if not subscription_id:
            raise ValueError("Subscription event is missing its subscription ID")
        # Stripe doesn't guarantee event delivery order. Fetching the current
        # subscription prevents a delayed older event from restoring stale access.
        stripe = _load_stripe_module()
        current_subscription = stripe.Subscription.retrieve(subscription_id)
        if hasattr(current_subscription, "to_dict_recursive"):
            subscription = current_subscription.to_dict_recursive()
        else:
            subscription = dict(current_subscription)
        event_created_at = event.get("created")
        if not event_is_newer(event_created_at, None):
            raise ValueError("Subscription event has an invalid creation timestamp")
        prior = None
        prior_response = (
            supabase.table("user_subscriptions")
            .select("user_id,provider_subscription_id,provider_event_created_at")
            .eq("provider_subscription_id", subscription_id)
            .limit(1)
            .execute()
        )
        prior = prior_response.data[0] if prior_response.data else None
        metadata_user_id = str((subscription.get("metadata") or {}).get("user_id") or "").strip()
        if prior and metadata_user_id and str(prior.get("user_id")) != metadata_user_id:
            raise ValueError("Subscription metadata conflicts with the linked account")
        if prior and not event_is_newer(event_created_at, prior.get("provider_event_created_at")):
            (
                supabase.table("billing_webhook_events")
                .update({
                    "processing_status": "ignored",
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                    "error_message": "Out-of-order subscription event ignored",
                })
                .eq("provider", "stripe")
                .eq("provider_event_id", event_id)
                .execute()
            )
            return {"received": True, "stale": True}
        fallback_user_id = prior.get("user_id") if prior else None
        row = subscription_row_from_stripe(
            subscription,
            BILLING_CONFIG,
            fallback_user_id=fallback_user_id,
        )
        if event_type == "customer.subscription.deleted":
            row["status"] = "canceled"
        row["provider_event_created_at"] = int(event_created_at)
        account_subscription = (
            supabase.table("user_subscriptions")
            .select("provider_subscription_id,provider_event_created_at")
            .eq("user_id", row["user_id"])
            .limit(1)
            .execute()
        )
        if account_subscription.data:
            linked_id = account_subscription.data[0].get("provider_subscription_id")
            if linked_id and linked_id != subscription_id:
                raise ValueError("Account is already linked to another subscription")
            if not event_is_newer(
                event_created_at,
                account_subscription.data[0].get("provider_event_created_at"),
            ):
                (
                    supabase.table("billing_webhook_events")
                    .update({
                        "processing_status": "ignored",
                        "processed_at": datetime.now(timezone.utc).isoformat(),
                        "error_message": "Out-of-order account event ignored",
                    })
                    .eq("provider", "stripe")
                    .eq("provider_event_id", event_id)
                    .execute()
                )
                return {"received": True, "stale": True}
            (
                supabase.table("user_subscriptions")
                .update(row)
                .eq("user_id", row["user_id"])
                .execute()
            )
        else:
            supabase.table("user_subscriptions").insert(row).execute()
        (
            supabase.table("billing_webhook_events")
            .update({
                "processing_status": "processed",
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "error_message": None,
            })
            .eq("provider", "stripe")
            .eq("provider_event_id", event_id)
            .execute()
        )
        return {"received": True, "duplicate": False}
    except ValueError as exc:
        logger.warning("Rejected Stripe event %s: %s", event_id, exc)
        try:
            (
                supabase.table("billing_webhook_events")
                .update({"processing_status": "failed", "error_message": str(exc)[:500]})
                .eq("provider", "stripe")
                .eq("provider_event_id", event_id)
                .execute()
            )
        except Exception:
            logger.exception("Unable to record Stripe event failure %s", event_id)
        raise HTTPException(status_code=422, detail="The subscription event could not be mapped")
    except Exception:
        logger.exception("Stripe webhook processing failed for %s", event_id)
        raise HTTPException(status_code=500, detail="Webhook processing failed; Stripe may retry")


class AlertRuleInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    ticker: str | None = Field(default=None, max_length=15)
    pattern_name: str | None = Field(default=None, max_length=100)
    minimum_win_rate: float = Field(default=90.0, ge=0, le=100)
    minimum_sample_size: int = Field(default=30, ge=2, le=100000)
    minimum_relative_volume: float | None = Field(default=None, ge=0)
    channels: list[str] = Field(default_factory=lambda: ["in_app"])
    enabled: bool = True


class BracketOrderPreviewInput(BaseModel):
    ticker: str = Field(min_length=1, max_length=15)
    side: str = "buy"
    quantity: int = Field(gt=0, le=1000000)
    entry_price: float = Field(gt=0)
    stop_loss_price: float = Field(gt=0)
    take_profit_price: float = Field(gt=0)
    order_type: str = "limit"


class BrokerOrderIntentInput(BaseModel):
    provider: str = Field(min_length=2, max_length=40)
    broker_connection_id: str | None = Field(
        default=None,
        pattern=r"^[a-fA-F0-9-]{36}$",
    )
    discipline_evaluation_id: str | None = Field(
        default=None,
        pattern=r"^[a-fA-F0-9-]{36}$",
    )
    order: BracketOrderPreviewInput
    user_confirmed: bool = False


class LargeTradeContext(BaseModel):
    ticker: str = Field(min_length=1, max_length=15)
    asset_type: str = "stock"
    quantity: float = Field(gt=0)
    price: float = Field(gt=0)
    average_trade_quantity: float = Field(gt=0)
    average_daily_volume: float = Field(gt=0)
    relative_volume: float = Field(gt=0)
    float_or_circulating_units: float | None = Field(default=None, gt=0)
    side: str = "unknown"


class SupernovaCandidateInput(BaseModel):
    ticker: str = Field(min_length=1, max_length=15)
    price: float = Field(gt=0, allow_inf_nan=False)
    relative_volume_at_time: float = Field(ge=0, allow_inf_nan=False)
    volume_acceleration: float = Field(ge=0, allow_inf_nan=False)
    price_change_pct: float = Field(allow_inf_nan=False)
    range_breakout_pct: float = Field(allow_inf_nan=False)
    vwap_distance_pct: float = Field(allow_inf_nan=False)
    average_dollar_volume: float = Field(ge=0, allow_inf_nan=False)
    spread_bps: float = Field(ge=0, allow_inf_nan=False)
    float_turnover_pct: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    follow_through_confirmed: bool = False
    market_session: str = "regular"


class SupernovaAlertSettingsInput(BaseModel):
    enabled: bool = True
    minimum_score: float = Field(default=65, ge=0, le=100, allow_inf_nan=False)
    minimum_relative_volume: float = Field(default=3, ge=0, allow_inf_nan=False)
    minimum_price_expansion_pct: float = Field(default=2, ge=0, le=100, allow_inf_nan=False)
    minimum_average_dollar_volume: float = Field(default=1_000_000, ge=0, allow_inf_nan=False)
    maximum_spread_bps: float = Field(default=150, ge=0, allow_inf_nan=False)
    directions: list[str] = Field(default_factory=lambda: ["up", "down"])
    channels: list[str] = Field(default_factory=lambda: ["in_app"])


class AnalogSearchInput(BaseModel):
    query: str = Field(min_length=3, max_length=300)
    top_n: int = Field(default=5, ge=1, le=10)
    minimum_similarity: float = Field(default=50, ge=0, le=100)


class DisciplineSettingsInput(BaseModel):
    enabled: bool = True
    mode: str = "coach"
    max_risk_per_trade_pct: float = Field(default=1.0, gt=0, le=100)
    max_position_value_pct: float = Field(default=20.0, gt=0, le=100)
    max_daily_loss_pct: float = Field(default=3.0, gt=0, le=100)
    minimum_reward_to_risk: float = Field(default=2.0, gt=0, le=100)
    required_relative_volume: float | None = Field(default=None, gt=0)
    require_setup_confirmation: bool = True
    max_open_positions: int = Field(default=5, ge=1, le=1000)
    max_sector_exposure_pct: float = Field(default=40.0, gt=0, le=100)
    cooldown_after_loss_minutes: int = Field(default=15, ge=0, le=10080)


class DisciplineOrderInput(BaseModel):
    ticker: str = Field(min_length=1, max_length=15)
    side: str = "buy"
    account_value: float = Field(gt=0)
    quantity: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    stop_loss_price: float = Field(gt=0)
    take_profit_price: float = Field(gt=0)
    daily_realized_pnl: float = 0
    projected_sector_exposure_pct: float = Field(default=0, ge=0, le=1000)
    current_open_positions: int = Field(default=0, ge=0, le=10000)
    minutes_since_last_loss: int | None = Field(default=None, ge=0)
    relative_volume: float | None = Field(default=None, gt=0)
    setup_confirmed: bool | None = None
    acknowledge_override: bool = False


class TradeProcessReviewInput(BaseModel):
    ticker: str = Field(min_length=1, max_length=15)
    pnl_percentage: float | None = None
    followed_entry_rule: bool
    used_planned_position_size: bool
    stop_present_before_entry: bool
    followed_stop_rule: bool
    followed_exit_rule: bool
    setup_confirmed_at_entry: bool
    notes: str | None = Field(default=None, max_length=1000)


class LegalAcceptanceInput(BaseModel):
    document_type: str
    document_version: str = Field(pattern=r"^[A-Za-z0-9._-]{1,40}$")
    document_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    accepted: bool


class OrderGateInput(BaseModel):
    verification_code: str = Field(min_length=4, max_length=128)
    discipline_evaluation_id: str = Field(min_length=1, max_length=128)


ANALOG_FEATURES = {
    "total_return_pct": (1.3, 50.0, "price return"),
    "annualized_volatility_pct": (1.0, 40.0, "volatility"),
    "max_drawdown_pct": (1.1, 30.0, "drawdown behavior"),
    "volume_growth_pct": (1.1, 100.0, "volume growth"),
    "trend_strength": (1.2, 1.0, "trend strength"),
    "relative_strength_pct": (1.0, 40.0, "relative strength"),
    "revenue_growth_pct": (1.3, 50.0, "revenue growth"),
    "gross_margin_pct": (0.7, 30.0, "gross margin"),
    "short_float_pct": (0.7, 20.0, "short interest"),
    "log_market_cap": (1.2, 1.5, "market-cap stage"),
}

SUPERNOVA_PRESET = {
    "preset_key": "floatiq_supernova_v1",
    "display_name": "FloatIQ Supernova Radar",
    "minimum_price": 0.25,
    "minimum_average_dollar_volume": 1_000_000,
    "maximum_spread_bps": 150,
    "watch_score": 40,
    "trigger_score": 65,
    "confirmation_score": 80,
    "minimum_trigger_relative_volume": 3.0,
    "minimum_trigger_price_expansion_pct": 2.0,
    "minimum_confirmation_relative_volume": 5.0,
    "minimum_confirmation_volume_acceleration": 3.0,
}


def extract_reference_ticker(query: str) -> str:
    """Extract an explicitly capitalized ticker from a natural-language request."""
    matches = re.findall(r"\b[A-Z]{1,5}(?:-[A-Z]{3})?\b", query)
    ignored = {"FIND", "LIKE", "STOCK", "STOCKS", "ETF", "CEO", "IPO"}
    for match in matches:
        if match not in ignored:
            return normalize_ticker(match)
    raise HTTPException(
        status_code=422,
        detail="Include an uppercase reference ticker, for example TSLA",
    )


def _feature_number(features: dict, key: str) -> float | None:
    value = features.get(key)
    if value is None and key == "log_market_cap" and features.get("market_cap"):
        value = math.log10(float(features["market_cap"]))
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def compare_analog_profiles(reference: dict, candidate: dict) -> dict | None:
    """Return an explainable 0-100 similarity score from normalized feature distances."""
    reference_features = reference.get("features") or {}
    candidate_features = candidate.get("features") or {}
    components = []
    similarities = []
    differences = []

    for key, (weight, scale, label) in ANALOG_FEATURES.items():
        reference_value = _feature_number(reference_features, key)
        candidate_value = _feature_number(candidate_features, key)
        if reference_value is None or candidate_value is None:
            continue
        distance = abs(candidate_value - reference_value)
        component_score = max(0.0, 1.0 - (distance / scale))
        components.append((component_score, weight))
        detail = {
            "feature": key,
            "label": label,
            "reference_value": round(reference_value, 3),
            "candidate_value": round(candidate_value, 3),
            "component_similarity": round(component_score * 100, 1),
        }
        if component_score >= 0.75:
            similarities.append(detail)
        elif component_score < 0.45:
            differences.append(detail)

    if len(components) < 4:
        return None
    weighted_score = sum(score * weight for score, weight in components) / sum(
        weight for _score, weight in components
    )
    return {
        "ticker": candidate.get("ticker"),
        "company_name": candidate.get("company_name"),
        "sector": candidate.get("sector"),
        "similarity_score": round(weighted_score * 100, 1),
        "shared_features_compared": len(components),
        "similarities": sorted(
            similarities, key=lambda item: item["component_similarity"], reverse=True
        )[:5],
        "differences": sorted(
            differences, key=lambda item: item["component_similarity"]
        )[:5],
        "candidate_window": {
            "start": candidate.get("window_start"),
            "end": candidate.get("window_end"),
        },
    }


def score_supernova_candidate(candidate: SupernovaCandidateInput) -> dict:
    """Score developing momentum conditions without predicting a future price move."""
    ticker = normalize_ticker(candidate.ticker)
    session = candidate.market_session.strip().lower()
    if session not in {"pre_market", "regular", "after_hours"}:
        raise HTTPException(
            status_code=422,
            detail="Market session must be pre_market, regular, or after_hours",
        )

    quality_failures = []
    if candidate.price < SUPERNOVA_PRESET["minimum_price"]:
        quality_failures.append("price_below_quality_floor")
    if candidate.average_dollar_volume < SUPERNOVA_PRESET["minimum_average_dollar_volume"]:
        quality_failures.append("insufficient_average_dollar_volume")
    if candidate.spread_bps > SUPERNOVA_PRESET["maximum_spread_bps"]:
        quality_failures.append("spread_too_wide")

    direction = "up" if candidate.price_change_pct >= 0 else "down"
    vwap_aligned = (
        candidate.vwap_distance_pct >= 0
        if direction == "up"
        else candidate.vwap_distance_pct <= 0
    )
    breakout_in_direction = (
        candidate.range_breakout_pct >= 0
        if direction == "up"
        else candidate.range_breakout_pct <= 0
    )
    directional_breakout = abs(candidate.range_breakout_pct) if breakout_in_direction else 0.0

    components = {
        "time_adjusted_relative_volume": min(candidate.relative_volume_at_time / 8.0, 1.0) * 30,
        "volume_acceleration": min(candidate.volume_acceleration / 5.0, 1.0) * 20,
        "price_expansion": min(abs(candidate.price_change_pct) / 10.0, 1.0) * 20,
        "range_breakout": min(directional_breakout / 3.0, 1.0) * 15,
        "vwap_alignment": 10.0 if vwap_aligned else 0.0,
        "float_turnover": (
            min(candidate.float_turnover_pct / 5.0, 1.0) * 5
            if candidate.float_turnover_pct is not None
            else 0.0
        ),
    }
    score = round(sum(components.values()), 1)
    trigger_conditions = (
        score >= SUPERNOVA_PRESET["trigger_score"]
        and candidate.relative_volume_at_time >= SUPERNOVA_PRESET["minimum_trigger_relative_volume"]
        and abs(candidate.price_change_pct) >= SUPERNOVA_PRESET["minimum_trigger_price_expansion_pct"]
        and breakout_in_direction
        and vwap_aligned
    )
    confirmation_conditions = (
        trigger_conditions
        and score >= SUPERNOVA_PRESET["confirmation_score"]
        and candidate.relative_volume_at_time >= SUPERNOVA_PRESET["minimum_confirmation_relative_volume"]
        and candidate.volume_acceleration >= SUPERNOVA_PRESET["minimum_confirmation_volume_acceleration"]
        and candidate.follow_through_confirmed
    )

    if quality_failures:
        stage = "SUPPRESSED"
    elif confirmation_conditions:
        stage = "CONFIRMED_MOMENTUM"
    elif trigger_conditions:
        stage = "TRIGGERED"
    elif score >= SUPERNOVA_PRESET["watch_score"]:
        stage = "WATCHING"
    else:
        stage = "NORMAL"

    return {
        "ticker": ticker,
        "preset_key": SUPERNOVA_PRESET["preset_key"],
        "stage": stage,
        "score": score,
        "score_maximum": 100,
        "direction": direction,
        "alert_eligible": stage in {"TRIGGERED", "CONFIRMED_MOMENTUM"},
        "quality_failures": quality_failures,
        "components": {key: round(value, 1) for key, value in components.items()},
        "metrics": candidate.model_dump(exclude={"ticker"}),
        "time_adjusted_volume_required": True,
        "follow_through_required_for_confirmation": True,
        "prediction_claimed": False,
        "calibration_status": "provisional_until_paid_feed_backtest",
        "message": f"{ticker} has {stage.lower().replace('_', ' ')} Supernova conditions; this is not a price prediction.",
    }


def normalize_supernova_alert_settings(settings: dict) -> dict:
    normalized = SupernovaAlertSettingsInput(**settings).model_dump()
    allowed_directions = {"up", "down"}
    allowed_channels = {"in_app", "push", "web"}
    normalized["directions"] = list(dict.fromkeys(
        str(value).strip().lower() for value in normalized["directions"]
    ))
    normalized["channels"] = list(dict.fromkeys(
        str(value).strip().lower() for value in normalized["channels"]
    ))
    if not normalized["directions"] or any(
        value not in allowed_directions for value in normalized["directions"]
    ):
        raise HTTPException(status_code=422, detail="Directions must contain up, down, or both")
    if not normalized["channels"] or any(
        value not in allowed_channels for value in normalized["channels"]
    ):
        raise HTTPException(status_code=422, detail="Channels must be in_app, push, or web")
    return normalized


def supernova_candidate_matches_alert_settings(candidate: dict, settings: dict) -> bool:
    """Apply saved Elite notification filters to a default-radar candidate."""
    if not settings["enabled"]:
        return False
    return all((
        float(candidate.get("score", 0)) >= settings["minimum_score"],
        float(candidate.get("relative_volume_at_time", 0)) >= settings["minimum_relative_volume"],
        abs(float(candidate.get("price_change_pct", 0))) >= settings["minimum_price_expansion_pct"],
        float(candidate.get("average_dollar_volume", 0)) >= settings["minimum_average_dollar_volume"],
        float(candidate.get("spread_bps", float("inf"))) <= settings["maximum_spread_bps"],
        candidate.get("direction") in settings["directions"],
    ))


def score_large_trade(context: LargeTradeContext) -> dict:
    """Score a trade relative to its own asset instead of using a fixed share count."""
    asset_type = context.asset_type.strip().lower()
    if asset_type not in {"stock", "crypto"}:
        raise HTTPException(status_code=422, detail="Asset type must be stock or crypto")
    side = context.side.strip().lower()
    if side not in {"buy", "sell", "unknown"}:
        raise HTTPException(status_code=422, detail="Side must be buy, sell, or unknown")

    notional_value = context.quantity * context.price
    trade_vs_typical = context.quantity / context.average_trade_quantity
    percent_of_daily_volume = (context.quantity / context.average_daily_volume) * 100
    percent_of_float = (
        (context.quantity / context.float_or_circulating_units) * 100
        if context.float_or_circulating_units
        else None
    )

    score = 0
    notional_levels = (1_000_000, 10_000_000) if asset_type == "crypto" else (100_000, 1_000_000)
    score += 2 if notional_value >= notional_levels[1] else 1 if notional_value >= notional_levels[0] else 0
    score += 2 if trade_vs_typical >= 20 else 1 if trade_vs_typical >= 5 else 0
    score += 2 if percent_of_daily_volume >= 1 else 1 if percent_of_daily_volume >= 0.25 else 0
    score += 2 if context.relative_volume >= 5 else 1 if context.relative_volume >= 2 else 0
    if percent_of_float is not None:
        score += 2 if percent_of_float >= 0.25 else 1 if percent_of_float >= 0.05 else 0

    if score >= 9:
        classification = "exceptional_whale_activity"
    elif score >= 7:
        classification = "institutional_scale_characteristics"
    elif score >= 5:
        classification = "large_block"
    elif score >= 3:
        classification = "unusual_volume"
    else:
        classification = "normal_activity"

    ticker = normalize_ticker(context.ticker)
    label = classification.replace("_", " ").title()
    return {
        "ticker": ticker,
        "asset_type": asset_type,
        "side": side,
        "classification": classification,
        "score": score,
        "score_maximum": 10,
        "alert_eligible": score >= 3,
        "notional_value": round(notional_value, 2),
        "trade_vs_typical": round(trade_vs_typical, 2),
        "percent_of_daily_volume": round(percent_of_daily_volume, 4),
        "relative_volume": round(context.relative_volume, 2),
        "percent_of_float_or_supply": round(percent_of_float, 6) if percent_of_float is not None else None,
        "message": (
            f"{label} in {ticker}: {context.quantity:,.8g} units at ${context.price:,.2f} "
            f"(${notional_value:,.0f}), {trade_vs_typical:.1f}x its typical trade size and "
            f"{percent_of_daily_volume:.2f}% of average daily volume."
        ),
        "identity_claimed": False,
        "calibration_status": "provisional_until_live_feed_baseline",
    }


@app.post("/api/tools/large-trade-score")
def calculate_large_trade_score(
    context: LargeTradeContext,
    authorization: Annotated[str | None, Header()] = None,
):
    require_authenticated_tier(authorization, "premium_scanner")
    return score_large_trade(context)


@app.post("/api/tools/supernova-score")
def calculate_supernova_score(
    candidate: SupernovaCandidateInput,
    authorization: Annotated[str | None, Header()] = None,
):
    """Evaluate supplied metrics with the same transparent rules used by the radar."""
    _user_id, permissions = require_authenticated_tier(authorization, "premium_scanner")
    return {
        **score_supernova_candidate(candidate),
        "tier": permissions["tier"],
        "input_source": "caller_supplied_metrics",
    }


def load_supernova_alert_settings(user_id: str) -> dict:
    if supabase is None:
        raise HTTPException(status_code=503, detail="Supernova settings database is not configured")
    try:
        response = (
            supabase.table("user_supernova_settings")
            .select("*")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if not response.data:
            return normalize_supernova_alert_settings({})
        allowed = set(SupernovaAlertSettingsInput.model_fields)
        return normalize_supernova_alert_settings({
            key: value for key, value in response.data[0].items() if key in allowed
        })
    except HTTPException:
        raise
    except Exception:
        logger.exception("Supernova settings lookup failed for %s", user_id)
        raise HTTPException(status_code=502, detail="Supernova settings are temporarily unavailable")


@app.get("/api/scanners/supernova-settings")
def get_supernova_alert_settings(
    authorization: Annotated[str | None, Header()] = None,
):
    user_id, _permissions = require_authenticated_tier(authorization, "autonomous_bot")
    return {
        "settings": load_supernova_alert_settings(user_id),
        "user_authored": True,
    }


@app.put("/api/scanners/supernova-settings")
def save_supernova_alert_settings(
    settings: SupernovaAlertSettingsInput,
    authorization: Annotated[str | None, Header()] = None,
):
    user_id, _permissions = require_authenticated_tier(authorization, "autonomous_bot")
    normalized = normalize_supernova_alert_settings(settings.model_dump())
    try:
        response = supabase.table("user_supernova_settings").upsert(
            {"user_id": user_id, **normalized}, on_conflict="user_id"
        ).execute()
        return {
            "settings": response.data[0] if response.data else normalized,
            "user_authored": True,
        }
    except Exception:
        logger.exception("Supernova settings save failed for %s", user_id)
        raise HTTPException(status_code=502, detail="Supernova settings could not be saved")


@app.get("/api/scanners/supernova-radar")
def get_supernova_radar(
    limit: int = Query(25, ge=1, le=100),
    authorization: Annotated[str | None, Header()] = None,
):
    """Return the default Pro/Elite Supernova preset and fresh live candidates."""
    user_id, permissions = require_authenticated_tier(authorization, "premium_scanner")
    base = {
        "scanner": SUPERNOVA_PRESET,
        "tier": permissions["tier"],
        "included_with_subscription": True,
        "custom_thresholds_available": permissions["custom_supernova_thresholds"],
        "prediction_claimed": False,
    }
    if not SUPERNOVA_FEED_ENABLED:
        return {
            **base,
            "data_status": "awaiting_live_feed",
            "candidates": [],
        }
    if supabase is None:
        raise HTTPException(status_code=503, detail="Supernova cache is not configured")
    try:
        response = (
            supabase.table("supernova_scan_cache")
            .select("*")
            .order("score", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception:
        logger.exception("Supernova Radar lookup failed")
        raise HTTPException(status_code=502, detail="Supernova Radar is temporarily unavailable")

    now = datetime.now(timezone.utc)
    alert_settings = (
        load_supernova_alert_settings(user_id)
        if permissions["custom_supernova_thresholds"]
        else normalize_supernova_alert_settings({})
    )
    candidates = []
    for row in response.data or []:
        try:
            updated_at = datetime.fromisoformat(str(row.get("updated_at", "")).replace("Z", "+00:00"))
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        age_seconds = (now - updated_at).total_seconds()
        if 0 <= age_seconds <= SUPERNOVA_CACHE_MAX_AGE_SECONDS:
            candidates.append({
                **row,
                "data_age_seconds": round(age_seconds, 1),
                "matches_active_alert_filters": supernova_candidate_matches_alert_settings(
                    row, alert_settings
                ),
            })
    return {
        **base,
        "data_status": "live" if candidates else "live_no_fresh_candidates",
        "active_alert_filters": alert_settings,
        "candidates": candidates,
    }


@app.get("/api/tools/compounding-scenario")
def calculate_compounding_scenario(
    starting_balance: float = Query(1000, gt=0, le=100000000),
    winning_days: int = Query(252, ge=0, le=10000),
    average_win_pct: float = Query(2.0, ge=0, le=100),
    losing_days: int = Query(0, ge=0, le=10000),
    average_loss_pct: float = Query(1.0, ge=0, lt=100),
    trading_cost_pct_per_day: float = Query(0.0, ge=0, lt=100),
):
    """Educational compounding math for a paper challenge, never a return projection."""
    net_win = (average_win_pct - trading_cost_pct_per_day) / 100
    net_loss = (average_loss_pct + trading_cost_pct_per_day) / 100
    if net_win <= -1 or net_loss >= 1:
        raise HTTPException(status_code=422, detail="Costs and losses would reduce the account to zero")
    final_balance = starting_balance * ((1 + net_win) ** winning_days) * ((1 - net_loss) ** losing_days)
    total_days = winning_days + losing_days
    return {
        "starting_balance": round(starting_balance, 2),
        "final_balance": round(final_balance, 2),
        "profit": round(final_balance - starting_balance, 2),
        "trading_days": total_days,
        "winning_days": winning_days,
        "losing_days": losing_days,
        "win_rate": round((winning_days / total_days) * 100, 2) if total_days else 0,
        "average_win_pct": average_win_pct,
        "average_loss_pct": average_loss_pct,
        "trading_cost_pct_per_day": trading_cost_pct_per_day,
        "paper_challenge_only": True,
        "assumption_warning": (
            "This is compounding arithmetic, not a forecast. Real trading includes losing days, "
            "slippage, fees, taxes, liquidity limits, position-size limits, and execution risk."
        ),
    }


@app.post("/api/research/analog-search")
def search_market_analogs(
    request: AnalogSearchInput,
    authorization: Annotated[str | None, Header()] = None,
):
    """Rank current stocks against a curated historical reference era."""
    if not ANALOG_SEARCH_ENABLED:
        raise HTTPException(status_code=404, detail="Experimental analog search is not enabled")
    _user_id, permissions = require_authenticated_tier(authorization, "autonomous_bot")
    reference_ticker = extract_reference_ticker(request.query)
    if supabase is None:
        raise HTTPException(status_code=503, detail="Analog profile database is not configured")

    try:
        reference_response = (
            supabase.table("market_analog_profiles")
            .select("ticker,company_name,sector,window_start,window_end,era_label,features")
            .eq("ticker", reference_ticker)
            .eq("profile_type", "reference_era")
            .order("window_end", desc=True)
            .limit(1)
            .execute()
        )
        if not reference_response.data:
            raise HTTPException(
                status_code=404,
                detail=f"No curated pre-breakout reference era is available for {reference_ticker}",
            )
        reference = reference_response.data[0]
        candidate_response = (
            supabase.table("market_analog_profiles")
            .select("ticker,company_name,sector,window_start,window_end,features")
            .eq("profile_type", "current")
            .neq("ticker", reference_ticker)
            .limit(7000)
            .execute()
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Analog profile lookup failed for %s", reference_ticker)
        raise HTTPException(status_code=502, detail="Analog search is temporarily unavailable")

    matches = []
    for candidate in candidate_response.data or []:
        comparison = compare_analog_profiles(reference, candidate)
        if comparison and comparison["similarity_score"] >= request.minimum_similarity:
            matches.append(comparison)
    matches.sort(key=lambda item: item["similarity_score"], reverse=True)

    return {
        "query": request.query,
        "reference": {
            "ticker": reference.get("ticker"),
            "company_name": reference.get("company_name"),
            "era_label": reference.get("era_label"),
            "window_start": reference.get("window_start"),
            "window_end": reference.get("window_end"),
        },
        "tier": permissions["tier"],
        "experimental": True,
        "feature_scope": "price, volume, market structure, and available fundamentals",
        "prediction_disclaimer": (
            "Similarity to a historical company stage does not predict the same future outcome."
        ),
        "matches": matches[: request.top_n],
    }


def normalize_ticker(ticker: str) -> str:
    normalized = ticker.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.^-]{1,15}", normalized):
        raise HTTPException(status_code=422, detail="Invalid ticker format")
    return normalized


def normalize_discipline_settings(settings: dict) -> dict:
    normalized = DisciplineSettingsInput(**settings).model_dump()
    normalized["mode"] = normalized["mode"].strip().lower()
    if normalized["mode"] not in {"monitor", "coach", "strict", "locked"}:
        raise HTTPException(
            status_code=422,
            detail="Discipline mode must be monitor, coach, strict, or locked",
        )
    return normalized


def evaluate_discipline_order(order: DisciplineOrderInput, settings: dict) -> dict:
    """Compare an order with rules authored by the user; do not create recommendations."""
    rules = normalize_discipline_settings(settings)
    ticker = normalize_ticker(order.ticker)
    side = order.side.strip().lower()
    if side not in {"buy", "sell"}:
        raise HTTPException(status_code=422, detail="Side must be buy or sell")
    if side == "buy" and not (order.stop_loss_price < order.entry_price < order.take_profit_price):
        raise HTTPException(status_code=422, detail="A buy plan requires stop < entry < take profit")
    if side == "sell" and not (order.take_profit_price < order.entry_price < order.stop_loss_price):
        raise HTTPException(status_code=422, detail="A sell plan requires take profit < entry < stop")

    risk_per_unit = abs(order.entry_price - order.stop_loss_price)
    reward_per_unit = abs(order.take_profit_price - order.entry_price)
    planned_risk = order.quantity * risk_per_unit
    planned_position_value = order.quantity * order.entry_price
    risk_pct = (planned_risk / order.account_value) * 100
    position_value_pct = (planned_position_value / order.account_value) * 100
    reward_to_risk = reward_per_unit / risk_per_unit
    daily_loss_pct = (abs(min(order.daily_realized_pnl, 0)) / order.account_value) * 100

    violations = []
    unable_to_evaluate = []

    def add_violation(rule_key: str, observed, limit, message: str):
        violations.append({
            "rule_key": rule_key,
            "source": "user_defined_rule",
            "observed": round(observed, 4) if isinstance(observed, float) else observed,
            "user_limit": limit,
            "message": message,
        })

    if risk_pct > rules["max_risk_per_trade_pct"]:
        add_violation(
            "max_risk_per_trade_pct",
            risk_pct,
            rules["max_risk_per_trade_pct"],
            "This plan exceeds your saved maximum risk per trade.",
        )
    if position_value_pct > rules["max_position_value_pct"]:
        add_violation(
            "max_position_value_pct",
            position_value_pct,
            rules["max_position_value_pct"],
            "This plan exceeds your saved maximum position value.",
        )
    if daily_loss_pct >= rules["max_daily_loss_pct"]:
        add_violation(
            "max_daily_loss_pct",
            daily_loss_pct,
            rules["max_daily_loss_pct"],
            "Your recorded daily loss has reached your saved limit.",
        )
    if reward_to_risk < rules["minimum_reward_to_risk"]:
        add_violation(
            "minimum_reward_to_risk",
            reward_to_risk,
            rules["minimum_reward_to_risk"],
            "This plan is below your saved minimum reward-to-risk ratio.",
        )
    if order.projected_sector_exposure_pct > rules["max_sector_exposure_pct"]:
        add_violation(
            "max_sector_exposure_pct",
            order.projected_sector_exposure_pct,
            rules["max_sector_exposure_pct"],
            "This plan exceeds your saved maximum sector exposure.",
        )
    if order.current_open_positions >= rules["max_open_positions"]:
        add_violation(
            "max_open_positions",
            order.current_open_positions,
            rules["max_open_positions"],
            "Your recorded open-position count has reached your saved limit.",
        )
    if rules["require_setup_confirmation"] and order.setup_confirmed is not True:
        add_violation(
            "require_setup_confirmation",
            order.setup_confirmed,
            True,
            "This plan does not show the setup confirmation required by your saved rule.",
        )
    if rules["required_relative_volume"] is not None:
        if order.relative_volume is None:
            unable_to_evaluate.append({
                "rule_key": "required_relative_volume",
                "message": "Relative-volume data was not supplied, so this rule could not be evaluated.",
            })
        elif order.relative_volume < rules["required_relative_volume"]:
            add_violation(
                "required_relative_volume",
                order.relative_volume,
                rules["required_relative_volume"],
                "Recorded relative volume is below your saved requirement.",
            )
    if rules["cooldown_after_loss_minutes"] > 0:
        if order.minutes_since_last_loss is None:
            unable_to_evaluate.append({
                "rule_key": "cooldown_after_loss_minutes",
                "message": "No prior-loss timestamp was supplied, so the cooldown rule could not be evaluated.",
            })
        elif order.minutes_since_last_loss < rules["cooldown_after_loss_minutes"]:
            add_violation(
                "cooldown_after_loss_minutes",
                order.minutes_since_last_loss,
                rules["cooldown_after_loss_minutes"],
                "This plan falls inside the cooldown period you selected after a loss.",
            )

    mode = rules["mode"]
    has_gate_issues = bool(violations or unable_to_evaluate)
    if not rules["enabled"] or not has_gate_issues:
        discipline_gate_passed = True
    elif mode in {"monitor", "coach"}:
        discipline_gate_passed = True
    elif mode == "strict":
        discipline_gate_passed = order.acknowledge_override
    else:
        discipline_gate_passed = False

    return {
        "ticker": ticker,
        "mode": mode,
        "discipline_gate_passed": discipline_gate_passed,
        "requires_acknowledgement": has_gate_issues and mode == "strict" and not order.acknowledge_override,
        "override_available": mode != "locked",
        "broker_order_submitted": False,
        "product_role": "evaluation_of_user_defined_rules",
        "investment_recommendation": False,
        "metrics": {
            "planned_position_value": round(planned_position_value, 2),
            "planned_position_value_pct": round(position_value_pct, 4),
            "planned_risk": round(planned_risk, 2),
            "planned_risk_pct": round(risk_pct, 4),
            "reward_to_risk": round(reward_to_risk, 4),
            "recorded_daily_loss_pct": round(daily_loss_pct, 4),
        },
        "violations": violations,
        "unable_to_evaluate": unable_to_evaluate,
        "language_notice": (
            "FloatIQ compared values you supplied with rules you created. It did not decide "
            "whether this security is suitable or recommend that you buy or sell it."
        ),
    }


def review_trade_process(trade: TradeProcessReviewInput) -> dict:
    """Score adherence to a recorded plan independently from the trade's profit or loss."""
    checks = [
        ("entry_rule", trade.followed_entry_rule, "The recorded entry did not follow the saved entry rule."),
        ("position_size", trade.used_planned_position_size, "The recorded size differed from the planned size."),
        ("stop_at_entry", trade.stop_present_before_entry, "No stop was recorded before the entry."),
        ("stop_rule", trade.followed_stop_rule, "The recorded trade did not follow the saved stop rule."),
        ("exit_rule", trade.followed_exit_rule, "The recorded exit did not follow the saved exit rule."),
        ("setup_confirmation", trade.setup_confirmed_at_entry, "The recorded entry occurred before setup confirmation."),
    ]
    findings = [
        {"rule_key": key, "source": "recorded_plan", "message": message}
        for key, passed, message in checks
        if not passed
    ]
    discipline_score = round((sum(1 for _key, passed, _message in checks if passed) / len(checks)) * 100, 1)
    profitable = trade.pnl_percentage is not None and trade.pnl_percentage > 0
    if discipline_score >= 80:
        process = "disciplined_win" if profitable else "disciplined_loss"
    else:
        process = "undisciplined_win" if profitable else "undisciplined_loss"
    return {
        "ticker": normalize_ticker(trade.ticker),
        "discipline_score": discipline_score,
        "process_classification": process,
        "findings": findings,
        "outcome_evaluated_separately": True,
        "message": (
            "This review measures adherence to the recorded plan. A profitable trade can still "
            "be undisciplined, and a losing trade can still follow the plan."
        ),
    }


def summarize_trade_reviews(reviews: list[dict]) -> dict:
    """Summarize recorded process behavior without treating profit as discipline."""
    if not reviews:
        return {
            "trades_reviewed": 0,
            "average_discipline_score": None,
            "current_disciplined_streak": 0,
            "process_classifications": {},
            "recurring_rule_conflicts": [],
        }
    scores = [float(row.get("discipline_score", 0)) for row in reviews]
    classifications = {}
    conflicts = {}
    for row in reviews:
        classification = row.get("process_classification", "unclassified")
        classifications[classification] = classifications.get(classification, 0) + 1
        for finding in row.get("findings") or []:
            key = finding.get("rule_key", "unknown")
            conflicts[key] = conflicts.get(key, 0) + 1
    streak = 0
    for row in reviews:  # Endpoint supplies newest first.
        if float(row.get("discipline_score", 0)) < 80:
            break
        streak += 1
    recurring = [
        {"rule_key": key, "recorded_conflicts": count}
        for key, count in sorted(conflicts.items(), key=lambda item: item[1], reverse=True)
    ]
    return {
        "trades_reviewed": len(reviews),
        "average_discipline_score": round(sum(scores) / len(scores), 1),
        "current_disciplined_streak": streak,
        "process_classifications": classifications,
        "recurring_rule_conflicts": recurring,
        "score_basis": "adherence_to_recorded_user_plan",
        "profit_is_not_the_discipline_score": True,
    }


@app.get("/api/setup-search")
def search_live_setups(
    setup: str = Query(min_length=1, max_length=100),
    chart_slot: Annotated[int, Query(ge=1, le=6)] = 1,
    reveal_ticker: str | None = None,
    authorization: Annotated[str | None, Header()] = None,
):
    """Search the scanner cache and apply the canonical 3/10 tier result contract."""
    verified_user_id = get_authenticated_user_id(authorization)
    permissions = check_user_tier_permissions(verified_user_id or "guest_user", "free")
    if chart_slot > permissions["max_charts"]:
        raise HTTPException(status_code=403, detail="Chart slot is not included in this subscription tier")
    if supabase is None:
        raise HTTPException(status_code=503, detail="Live setup search is not configured")

    try:
        response = (
            supabase.table("live_scan_cache")
            .select("ticker,pattern_detected,current_price,current_rvol,gap_fill_probability,market_session,updated_at")
            .ilike("pattern_detected", f"%{setup.strip()}%")
            .order("gap_fill_probability", desc=True)
            .limit(permissions["pattern_result_limit"])
            .execute()
        )
    except Exception:
        logger.exception("Live setup search failed")
        raise HTTPException(status_code=502, detail="Live setup search is temporarily unavailable")

    normalized_reveal = normalize_ticker(reveal_ticker) if reveal_ticker else None
    returned_tickers = {
        normalize_ticker(str(row.get("ticker", ""))) for row in (response.data or [])
    }
    reveal_key = (
        f"setup:{setup.strip().lower()}:{normalized_reveal}"
        if normalized_reveal in returned_tickers
        else None
    )
    reveal_allowed = False
    if permissions["tier"] == "free" and reveal_key:
        reveal_allowed = claim_daily_win_rate_reveal(
            verified_user_id or "guest_user", chart_slot, reveal_key
        )

    candidates = []
    for row in response.data or []:
        ticker = normalize_ticker(str(row.get("ticker", "")))
        win_rate_visible = permissions["win_rate_access"] == "all" or (
            reveal_allowed and ticker == normalized_reveal
        )
        public_row = {key: value for key, value in row.items() if key != "gap_fill_probability"}
        candidates.append({
            **public_row,
            "ticker": ticker,
            "win_rate": row.get("gap_fill_probability") if win_rate_visible else None,
            "win_rate_locked": not win_rate_visible,
        })

    return {
        "setup": setup.strip(),
        "tier": permissions["tier"],
        "result_limit": permissions["pattern_result_limit"],
        "max_allowed_charts": permissions["max_charts"],
        "daily_reveal_requires_sign_in": permissions["tier"] == "free" and verified_user_id is None,
        "candidates": candidates,
    }


@app.get("/api/pattern-probabilities")
def get_probabilities(
    ticker: str = "PLTR",
    timeframe: str = Query("1y", description="Options: 1d, 5d, 1mo, 6mo, 1y, max"),
    chart_interval: str = Query("1d", description="Options: 1m, 5m, 15m, 1h, 1d"),
    user_id: str = "guest_user",
    chart_slot: Annotated[int, Query(ge=1, le=6)] = 1,
    reveal_pattern_key: str | None = None,
    authorization: Annotated[str | None, Header()] = None,
    target_return_pct: Annotated[float, Query(gt=0, le=25)] = 0.5,
):
    """Calculate observed pattern outcomes from the available historical price window."""
    ticker = ticker.strip().upper()
    valid_timeframes = {"1d", "5d", "1mo", "6mo", "1y", "max"}
    valid_intervals = {"1m", "5m", "15m", "1h", "1d"}
    if not re.fullmatch(r"[A-Z0-9.^-]{1,15}", ticker):
        raise HTTPException(status_code=422, detail="Invalid ticker format")
    if timeframe not in valid_timeframes:
        raise HTTPException(status_code=422, detail="Unsupported timeframe")
    if chart_interval not in valid_intervals:
        raise HTTPException(status_code=422, detail="Unsupported chart interval")
    if chart_interval == "1m":
        timeframe = "7d"
    elif chart_interval in ["5m", "15m", "1h"] and timeframe not in ["1d", "5d", "1mo"]:
        timeframe = "1mo"
    try:
        verified_user_id = get_authenticated_user_id(authorization)
        permissions = check_user_tier_permissions(verified_user_id or "guest_user", "free")
        if chart_slot > permissions["max_charts"]:
            raise HTTPException(status_code=403, detail="Chart slot is not included in this subscription tier")
        spy_df = download_market_data("SPY", period="1y", interval="1d")
        if not spy_df.empty and len(spy_df) >= 200:
            if isinstance(spy_df.columns, pd.MultiIndex):
                spy_df.columns = spy_df.columns.get_level_values(0)
            spy_df['MA200'] = spy_df['Close'].rolling(window=200).mean()
            latest_ma = float(spy_df['MA200'].iloc[-1])
            if math.isfinite(latest_ma):
                is_bull_market = float(spy_df['Close'].iloc[-1]) >= latest_ma
                market_status = "BULLISH" if is_bull_market else "BEARISH"
            else:
                is_bull_market = None
                market_status = "UNKNOWN"
        else:
            market_status = "UNKNOWN"
            is_bull_market = None

        df = download_market_data(ticker, period=timeframe, interval=chart_interval)
        if df.empty or len(df) < 25:
            return {
                "ticker": ticker.upper(),
                "timeframe": timeframe,
                "interval": chart_interval,
                "current_market_environment": market_status,
                "asset_categories": ["common_stock"],
                "market_cap_formatted": None,
                "max_allowed_charts": permissions["max_charts"],
                "pattern_result_limit": permissions["pattern_result_limit"],
                "win_rate_access": permissions["win_rate_access"],
                "target_return_pct": target_return_pct,
                "patterns": [],
                "data_status": "unavailable",
            }
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        is_intraday = chart_interval.endswith(("m", "h"))
        typical_price_volume = df['Volume'] * (df['High'] + df['Low'] + df['Close']) / 3
        if is_intraday and isinstance(df.index, pd.DatetimeIndex):
            sessions = df.index.normalize()
            cumulative_value = typical_price_volume.groupby(sessions).cumsum()
            cumulative_volume = df['Volume'].groupby(sessions).cumsum()
            df['vwap'] = cumulative_value / cumulative_volume.replace(0, float("nan"))
        else:
            df['vwap'] = typical_price_volume.cumsum() / df['Volume'].cumsum().replace(0, float("nan"))
        df['volume_ma20'] = df['Volume'].rolling(window=20).mean()
        df['rvol'] = df['Volume'] / df['volume_ma20']
        df.ta.cdl_pattern(name="all", append=True)
        pattern_cols = [col for col in df.columns if col.startswith('CDL_')]
        logger.debug("%s rows=%s pattern_columns=%s", ticker, len(df), len(pattern_cols))

        results = []
        lookahead_candles = 15 if chart_interval == "1m" else (6 if chart_interval in ["5m", "15m"] else 5)

        try:
            asset_ticker_obj = yf.Ticker(ticker.upper())
            info_cache = asset_ticker_obj.info if hasattr(asset_ticker_obj, "info") else {}
        except Exception:
            info_cache = {}

        sector = str(info_cache.get("sector", "")).lower()
        industry = str(info_cache.get("industry", "")).lower()
        market_cap = info_cache.get("marketCap")
        current_price = float(df['Close'].iloc[-1])

        asset_class_tags = []
        if "technology" in sector or "software" in industry or "semiconductors" in industry:
            asset_class_tags.append("tech")
        if current_price < 5.00:
            asset_class_tags.append("penny_stock")
        if "-USD" in ticker.upper() or str(info_cache.get("quoteType", "")).upper() == "CRYPTOCURRENCY":
            asset_class_tags.append("crypto")
        meme_watchlist = ["GME", "AMC", "DJT", "WIF-USD", "DOGE-USD", "BABA"]
        if ticker.upper() in meme_watchlist:
            asset_class_tags.append("meme")
        if not asset_class_tags:
            asset_class_tags.append("common_stock")

        for i in range(20, len(df) - lookahead_candles):
            prior_close = float(df['Close'].iloc[i - 1])
            current_open = float(df['Open'].iloc[i])
            current_close = float(df['Close'].iloc[i])
            current_time = df.index[i]

            if is_intraday:
                is_gap_up = None
                is_valid_setup_candle = True
            else:
                gap_size = current_open - prior_close
                gap_pct = abs(gap_size) / prior_close if prior_close else 0.0
                is_gap_up = gap_size > 0
                is_valid_setup_candle = gap_pct >= 0.005

            if not is_valid_setup_candle:
                continue

            initial_entry_price = current_close if is_intraday else current_open

            for col in pattern_cols:
                pattern_value = int(df[col].iloc[i])
                if pattern_value == 0:
                    continue

                bullish_outcome = pattern_value > 0 if is_intraday else not bool(is_gap_up)
                outcome_reached = False
                pct_gained_on_outcome = 0.0
                max_drawdown_pct = 0.0
                for step in range(1, lookahead_candles + 1):
                    f_idx = i + step
                    if (
                        is_intraday
                        and isinstance(df.index, pd.DatetimeIndex)
                        and df.index[f_idx].date() != current_time.date()
                    ):
                        break
                    f_low = float(df['Low'].iloc[f_idx])
                    f_high = float(df['High'].iloc[f_idx])
                    if bullish_outcome:
                        adverse = max(0.0, initial_entry_price - f_low)
                        reached = f_high >= initial_entry_price * (1 + target_return_pct / 100)
                    else:
                        adverse = max(0.0, f_high - initial_entry_price)
                        reached = f_low <= initial_entry_price * (1 - target_return_pct / 100)

                    if not is_intraday:
                        reached = f_high >= prior_close if bullish_outcome else f_low <= prior_close
                    max_drawdown_pct = max(
                        max_drawdown_pct, (adverse / initial_entry_price) * 100
                    )
                    if reached:
                        outcome_reached = True
                        pct_gained_on_outcome = (
                            target_return_pct
                            if is_intraday
                            else (abs(current_open - prior_close) / current_open) * 100
                        )
                        break

                clean_name = col.replace("CDL_", "").lower().replace("_", " ")
                current_volume = float(df['Volume'].iloc[i])
                avg_recent_volume = float(df['Volume'].iloc[i - 5:i].mean())
                volume_velocity = round(current_volume / avg_recent_volume, 2) if avg_recent_volume > 0 else 1.0
                is_10x_spike = volume_velocity >= 10.0

                vwap_value = float(df['vwap'].iloc[i])
                vwap_void_dist = (
                    abs(current_close - vwap_value) / vwap_value
                    if math.isfinite(vwap_value) and vwap_value
                    else 0.0
                )
                results.append({
                    "pattern": clean_name,
                    "is_bullish_pattern": pattern_value > 0,
                    "gap_type": "intraday directional move" if is_intraday else ("gap up fill" if is_gap_up else "gap down fill"),
                    "outcome": 1 if outcome_reached else 0,
                    "trade_date": current_time,
                    "actual_gap_size_dollar": abs(round(current_open - prior_close, 2)),
                    "dollar_size": abs(round(current_open - prior_close, 2)),
                    "target_payout_dollar": (
                        initial_entry_price * (target_return_pct / 100)
                        if is_intraday
                        else abs(current_open - prior_close)
                    ),
                    "pct_gain": pct_gained_on_outcome if outcome_reached else 0.0,
                    "drawdown": max_drawdown_pct,
                    "is_bull_env": is_bull_market,
                    # Relative volume alone is unusual activity, not proof of a whale.
                    "is_unusual_relative_volume": volume_velocity >= 2.0,
                    "is_10x_volume_spike": is_10x_spike,
                    "momentum_velocity_factor": f"{volume_velocity}x Base",
                    "vwap_void_dist": vwap_void_dist,
                })
        logger.debug("%s raw pattern results=%s", ticker, len(results))
        if not results:
            return {
                "ticker": ticker.upper(),
                "timeframe": timeframe,
                "interval": chart_interval,
                "current_market_environment": market_status,
                "asset_categories": asset_class_tags,
                "market_cap_formatted": None if not market_cap else f"${round(float(market_cap) / 1000000, 2)}M",
                "max_allowed_charts": permissions["max_charts"],
                "pattern_result_limit": permissions["pattern_result_limit"],
                "win_rate_access": permissions["win_rate_access"],
                "target_return_pct": target_return_pct,
                "patterns": [],
                "data_status": "available_no_qualifying_patterns",
            }

        summary_df = pd.DataFrame(results)
        grouped = summary_df.groupby(["pattern", "gap_type"])
        formatted_patterns = []
        thirty_days_ago = df.index[-1] - timedelta(days=30)

        for (p_name, g_type), group in grouped:
            total = len(group)
            if total < 2:
                continue

            successes = int(group["outcome"].sum())
            win_rate = round((successes / total) * 100, 2)
            recent_group = group[group["trade_date"] >= thirty_days_ago]
            recent_win_rate = None
            if len(recent_group) >= 2:
                recent_win_rate = round((recent_group["outcome"].sum() / len(recent_group)) * 100, 2)

            confluence_score = 1
            if group["is_unusual_relative_volume"].any():
                confluence_score += 1
            if is_bull_market is True and group["is_bullish_pattern"].any():
                confluence_score += 1
            if group["vwap_void_dist"].mean() > 0.02:
                confluence_score += 1

            successful_group = group[group["outcome"] == 1]
            failed_group = group[group["outcome"] == 0]
            avg_dd = round(float(group["drawdown"].mean()), 2)
            avg_loss = round(float(failed_group["drawdown"].mean()), 2) if len(failed_group) else 0.0
            avg_dollar = (
                round(float(successful_group["target_payout_dollar"].mean()), 2)
                if len(successful_group)
                else 0.0
            )
            avg_gain = (
                round(float(successful_group["pct_gain"].mean()), 2)
                if len(successful_group)
                else 0.0
            )
            expectancy = round(
                ((win_rate / 100.0) * avg_gain)
                - (((100 - win_rate) / 100.0) * avg_loss),
                2,
            )
            formatted_patterns.append({
                "pattern_key": re.sub(r"[^a-z0-9]+", "-", f"{p_name}-{g_type}".lower()).strip("-"),
                "pattern_headline": f"{p_name} ({g_type})",
                "overall_probability_win_rate": f"{win_rate}%",
                "historical_win_rate_pct": win_rate,
                "confidence_interval_95": wilson_confidence_interval(successes, total),
                "thirty_day_recent_probability": f"{recent_win_rate}%" if recent_win_rate is not None else None,
                "recent_sample_size_count": len(recent_group),
                "sample_quality": "low" if total < 20 else "moderate" if total < 50 else "higher",
                "small_sample_warning": total < 20,
                "confluence_factor_rating": f"{confluence_score}/4",
                "sample_size_count": total,
                "average_target_payout": f"${avg_dollar} ({avg_gain}%)",
                "historical_adverse_drawdown": f"{avg_dd}%",
                "mathematical_expectancy_score": expectancy,
                "strategy_hot_badge": (
                    "Recent rate above full sample"
                    if recent_win_rate is not None and recent_win_rate > win_rate
                    else "Observed history"
                ),
            })

        formatted_patterns.sort(
            key=lambda item: item["mathematical_expectancy_score"],
            reverse=True,
        )
        formatted_patterns = apply_pattern_entitlements(
            patterns=formatted_patterns,
            permissions=permissions,
            user_id=verified_user_id or "guest_user",
            chart_slot=chart_slot,
            reveal_pattern_key=reveal_pattern_key,
        )

        return {
            "ticker": ticker.upper(),
            "timeframe": timeframe,
            "interval": chart_interval,
            "current_market_environment": market_status,
            "asset_categories": asset_class_tags,
            "market_cap_formatted": None if not market_cap else f"${round(float(market_cap) / 1000000, 2)}M",
            "max_allowed_charts": permissions["max_charts"],
            "pattern_result_limit": permissions["pattern_result_limit"],
            "win_rate_access": permissions["win_rate_access"],
            "daily_reveal_requires_sign_in": permissions["tier"] == "free" and verified_user_id is None,
            "target_return_pct": target_return_pct,
            "outcome_definition": (
                f"pattern-direction move of {target_return_pct}% within {lookahead_candles} same-session candles"
                if is_intraday
                else f"gap fill to prior close within {lookahead_candles} daily candles"
            ),
            "metric_type": "historical_observed_rate",
            "data_status": "available",
            "patterns": formatted_patterns,
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Pattern pipeline failed for %s", ticker)
        raise HTTPException(status_code=502, detail="Pattern data is temporarily unavailable")

### 🧭 SECTION 3: Scanners, Risk Calculators, Live Movers & User Journals
@app.get("/api/scanners/premade-3pct-scalp")
def get_premade_3pct_scalp_candidates(
    ticker: str = "PLTR",
    user_id: str = "guest_user",
    chart_slot: Annotated[int, Query(ge=1, le=6)] = 1,
    authorization: Annotated[str | None, Header()] = None,
):
    """Isolates high-expectancy setups capable of immediate 3% price extensions."""
    raw_data = get_probabilities(
        ticker=ticker,
        timeframe="1mo",
        chart_interval="5m",
        user_id=user_id,
        chart_slot=chart_slot,
        authorization=authorization,
        target_return_pct=3.0,
    )
    if not isinstance(raw_data, dict) or "error" in raw_data or "patterns" not in raw_data:
        return {"scanner_name": "3% Quick Scalp Radar", "candidates": []}
    scalp_candidates = [
        p for p in raw_data["patterns"]
        if p.get("overall_probability_win_rate") is not None
        and float(p["historical_win_rate_pct"]) >= 70.0
        and p["mathematical_expectancy_score"] >= 0.2
    ]
    return {
        "scanner_name": "3% Quick Scalp Radar",
        "market_environment_guard": raw_data["current_market_environment"],
        "total_setups_found": len(scalp_candidates),
        "candidates": scalp_candidates
    }

@app.get("/api/calculate-position")
def calculate_position(
    ticker: str, account_size: float, risk_percentage: float,
    entry_price: float, historical_drawdown_pct: float, trade_direction: str = "long"
):
    try:
        direction = trade_direction.lower().strip()
        if account_size <= 0 or entry_price <= 0:
            raise ValueError("Account size and entry price must be positive.")
        if not 0 < risk_percentage <= 100:
            raise ValueError("Risk percentage must be greater than 0 and no more than 100.")
        if historical_drawdown_pct < 0:
            raise ValueError("Historical drawdown cannot be negative.")
        if direction not in {"long", "short"}:
            raise ValueError("Trade direction must be long or short.")
        allowed_dollar_risk = account_size * (risk_percentage / 100.0)
        stop_pct = max(1.5, historical_drawdown_pct)
        if direction == "short":
            stop_loss_price = round(entry_price * (1.0 + (stop_pct / 100.0)), 2)
            risk_per_share = stop_loss_price - entry_price
        else:
            stop_loss_price = round(entry_price * (1.0 - (stop_pct / 100.0)), 2)
            risk_per_share = entry_price - stop_loss_price
        if risk_per_share <= 0:
            return {"error": "Invalid metrics passed to math blocks."}
        risk_based_shares = int(allowed_dollar_risk / risk_per_share)
        capital_limited_shares = int(account_size / entry_price)
        calculated_shares = min(risk_based_shares, capital_limited_shares)
        total_cost = calculated_shares * entry_price

        try:
            short_fraction = yf.Ticker(ticker.upper()).info.get("shortPercentOfFloat")
            short_float_pct = round(float(short_fraction) * 100, 2) if short_fraction is not None else 0.0
        except Exception:
            short_float_pct = 0.0

        advisory_text = "Calculated from the account, risk, entry, and drawdown values you supplied."
        if risk_based_shares > capital_limited_shares:
            advisory_text += " Position size was capped at available account capital."
        if direction == "short" and short_float_pct > 15.0:
            advisory_text += f" ⚠️ WARNING: Short Float is extreme at {short_float_pct}%. High risk of a SHORT SQUEEZE!"
        elif entry_price < 5.00:
            advisory_text += " ℹ️ NOTE: Asset is a penny stock (<$5). Check account cash boundaries."
        return {
            "shares_to_allocate": calculated_shares,
            "total_capital_required": f"${round(total_cost, 2)}",
            "suggested_stop_loss": f"${stop_loss_price} ({stop_pct}%)",
            "calculated_stop_loss": f"${stop_loss_price} ({stop_pct}%)",
            "max_risk_exposure": f"${round(allowed_dollar_risk, 2)}",
            "short_float_percentage": f"{short_float_pct}%",
            "advisory_warning_badge": advisory_text,
            "calculation_basis": "user_supplied_values",
            "investment_recommendation": False,
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/market-movers")
def get_market_movers():
    """Return movers only when live data is available; never serve placeholder quotes."""
    try:
        gainers_response = yf.screen("day_gainers", count=10)
        losers_response = yf.screen("day_losers", count=10)

        def format_quotes(response, direction):
            formatted = []
            for quote in response.get("quotes", []):
                ticker = quote.get("symbol")
                price = quote.get("regularMarketPrice")
                change_pct = quote.get("regularMarketChangePercent")
                if ticker is None or price is None or change_pct is None:
                    continue
                action = "up" if direction == "up" else "down"
                formatted.append({
                    "ticker": ticker,
                    "price": round(float(price), 2),
                    "change_pct": round(float(change_pct), 2),
                    "display_text": f"{ticker} {action} {abs(float(change_pct)):.2f}% at ${float(price):.2f}",
                })
            return formatted

        return {
            "gainers": format_quotes(gainers_response, "up"),
            "losers": format_quotes(losers_response, "down"),
        }
    except Exception:
        logger.exception("Live movers lookup failed")
        return {"gainers": [], "losers": [], "error": "Live movers are temporarily unavailable"}

@app.get("/api/user-journal-summary")
def get_user_journal_summary(
    user_id: str | None = None,
    authorization: Annotated[str | None, Header()] = None,
):
    verified_user_id = get_authenticated_user_id(authorization)
    if verified_user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        res = supabase.table("user_trade_journal").select("*").eq("user_id", verified_user_id).execute()
        if not res.data: return {"total_lifetime_trades_logged": 0, "performance_by_setup": []}
        df = pd.DataFrame(res.data)
        df['is_win'] = df['pnl_percentage'] > 0
        grouped = df.groupby(["pattern_traded", "chart_interval", "market_trend"]).agg(
            total_trades=("is_win", "count"), wins=("is_win", "sum"), net_pnl=("pnl_percentage", "sum")
        ).reset_index()
        summary_cards = []
        for _, r in grouped.iterrows():
            win_rate = round((r['wins'] / r['total_trades']) * 100, 2)
            summary_cards.append({
                "setup_headline": f"{r['chart_interval']} {r['pattern_traded'].title()}",
                "environment_context": f"Traded during a {r['market_trend']} Market",
                "stats_breakdown": f"Win Rate: {win_rate}% ({int(r['wins'])} / {int(r['total_trades'])} Wins)",
                "total_return_score": f"{round(r['net_pnl'], 2)}%",
                "status_badge": "Profit Engine ✅" if r['net_pnl'] > 0 else "Capital Leak ⚠️"
            })
        return {"total_lifetime_trades_logged": len(df), "overall_account_pnl_pct": f"{round(df['pnl_percentage'].sum(), 2)}%", "performance_by_setup": summary_cards}
    except Exception:
        logger.exception("Journal summary failed for user %s", verified_user_id)
        raise HTTPException(status_code=502, detail="Journal summary is temporarily unavailable")

@app.get("/api/user-morning-brief")
def get_user_morning_brief(
    user_id: str | None = None,
    authorization: Annotated[str | None, Header()] = None,
):
    verified_user_id = get_authenticated_user_id(authorization)
    if verified_user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        history_res = supabase.table("user_ticker_history").select("ticker").eq("user_id", verified_user_id).order("last_viewed_at", desc=True).limit(3).execute()
        recent_tickers = [row['ticker'].upper() for row in history_res.data] if history_res.data else ["PLTR", "AAPL", "NVDA"]
        spy_df = download_market_data("SPY", period="5d", interval="1d")
        market_env_str = "BULLISH" if float(spy_df['Close'].iloc[-1]) >= float(spy_df['Close'].rolling(window=2).mean().iloc[-1]) else "BEARISH"
        stock_briefs = []
        for ticker in recent_tickers:
            df = download_market_data(ticker, period="5d", interval="1d")
            if df.empty: continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            first_close = float(df['Close'].iloc[0])
            last_close = float(df['Close'].iloc[-1])
            if first_close == 0:
                continue
            five_day_change = round(((last_close - first_close) / first_close) * 100, 2)
            trend = "gained 📈" if five_day_change >= 0 else "dropped 📉"
            stock_briefs.append(f"📌 {ticker} has {trend} {abs(five_day_change)}% over your last tracking sessions.")
        return {"ai_generated_brief_text": f"Welcome back! The macro market environment is trended {market_env_str}.\n\n" + "\n\n".join(stock_briefs)}
    except Exception:
        logger.exception("Morning brief failed for user %s", verified_user_id)
        raise HTTPException(status_code=502, detail="Morning brief is temporarily unavailable")


@app.get("/api/market-alerts")
def get_market_alerts(
    limit: int = Query(25, ge=1, le=100),
    authorization: Annotated[str | None, Header()] = None,
):
    """Return Pro/Elite large-trade and separately verified insider events."""
    _, _permissions = require_authenticated_tier(authorization, "premium_scanner")
    try:
        response = (
            supabase.table("market_alert_events")
            .select("*")
            .order("detected_at", desc=True)
            .limit(limit)
            .execute()
        )
        alerts = []
        for raw_alert in response.data or []:
            alert = dict(raw_alert)
            is_verified_insider = (
                alert.get("event_type") == "verified_insider_purchase"
                and alert.get("verification_status") == "verified_public_filing"
                and bool(alert.get("source_name"))
            )
            if not is_verified_insider:
                alert["actor_name"] = None
                alert["actor_role"] = None
            alerts.append(alert)
        return {"alerts": alerts}
    except Exception:
        logger.exception("Market alert lookup failed")
        raise HTTPException(status_code=502, detail="Market alerts are temporarily unavailable")


def enqueue_user_notification(
    *,
    user_id: str,
    event_key: str,
    event_type: str,
    title: str,
    message: str,
    channels: list[str],
    payload: dict | None = None,
) -> dict:
    """Create one notification event and one delivery per channel, idempotently."""
    if supabase is None:
        raise RuntimeError("Database is not configured")
    event_row = notification_event_row(
        user_id=user_id,
        event_key=event_key,
        event_type=event_type,
        title=title,
        message=message,
        payload=payload,
    )
    normalized_channels = normalize_channels(channels)
    existing = (
        supabase.table("notifications")
        .select("*")
        .eq("user_id", user_id)
        .eq("event_key", event_row["event_key"])
        .limit(1)
        .execute()
    )
    if existing.data:
        return {"notification": existing.data[0], "already_recorded": True}
    saved = supabase.table("notifications").insert(event_row).execute()
    notification = saved.data[0] if saved.data else event_row
    notification_id = notification.get("id")
    if not notification_id:
        raise RuntimeError("Database did not return a notification ID")
    supabase.table("notification_deliveries").insert(
        delivery_rows(notification_id, normalized_channels)
    ).execute()
    return {"notification": notification, "already_recorded": False}


@app.get("/api/notifications")
def get_notifications(
    unread_only: bool = False,
    limit: int = Query(50, ge=1, le=100),
    authorization: Annotated[str | None, Header()] = None,
):
    """Return only the signed-in user's in-app notification inbox."""
    user_id = get_authenticated_user_id(authorization)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        query = (
            supabase.table("notifications")
            .select("id,event_type,title,message,payload,read_at,created_at")
            .eq("user_id", user_id)
        )
        if unread_only:
            query = query.is_("read_at", "null")
        response = query.order("created_at", desc=True).limit(limit).execute()
        notifications = response.data or []
        return {
            "notifications": notifications,
            "unread_count": sum(1 for item in notifications if not item.get("read_at")),
        }
    except Exception:
        logger.exception("Notification inbox lookup failed for %s", user_id)
        raise HTTPException(status_code=502, detail="Notifications are temporarily unavailable")


@app.post("/api/notifications/{notification_id}/read")
def mark_notification_read(
    notification_id: str,
    authorization: Annotated[str | None, Header()] = None,
):
    """Mark a notification read only when it belongs to the authenticated user."""
    user_id = get_authenticated_user_id(authorization)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not re.fullmatch(r"[a-fA-F0-9-]{36}", notification_id):
        raise HTTPException(status_code=422, detail="Invalid notification ID")
    try:
        existing = (
            supabase.table("notifications")
            .select("id,read_at")
            .eq("id", notification_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if not existing.data:
            raise HTTPException(status_code=404, detail="Notification not found")
        if existing.data[0].get("read_at"):
            return {"notification": existing.data[0], "already_read": True}
        updated = (
            supabase.table("notifications")
            .update({"read_at": datetime.now(timezone.utc).isoformat()})
            .eq("id", notification_id)
            .eq("user_id", user_id)
            .execute()
        )
        return {
            "notification": updated.data[0] if updated.data else {
                "id": notification_id,
                "read_at": datetime.now(timezone.utc).isoformat(),
            },
            "already_read": False,
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Notification update failed for %s", user_id)
        raise HTTPException(status_code=502, detail="Notification could not be updated")


@app.get("/api/alert-rules")
def get_alert_rules(
    authorization: Annotated[str | None, Header()] = None,
):
    user_id, _permissions = require_authenticated_tier(authorization, "autonomous_bot")
    try:
        response = (
            supabase.table("user_alert_rules")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return {"rules": response.data or []}
    except Exception:
        logger.exception("Alert-rule lookup failed for %s", user_id)
        raise HTTPException(status_code=502, detail="Alert rules are temporarily unavailable")


@app.post("/api/alert-rules", status_code=201)
def create_alert_rule(
    rule: AlertRuleInput,
    authorization: Annotated[str | None, Header()] = None,
):
    user_id, _permissions = require_authenticated_tier(authorization, "autonomous_bot")
    allowed_channels = {"in_app", "push", "web"}
    channels = list(dict.fromkeys(channel.strip().lower() for channel in rule.channels))
    if not channels or any(channel not in allowed_channels for channel in channels):
        raise HTTPException(status_code=422, detail="Channels must be in_app, push, or web")
    payload = rule.model_dump()
    payload["user_id"] = user_id
    payload["channels"] = channels
    if payload["ticker"]:
        payload["ticker"] = normalize_ticker(payload["ticker"])
    if not payload["ticker"] and not payload["pattern_name"]:
        raise HTTPException(status_code=422, detail="Choose a ticker, pattern, or both")
    try:
        response = supabase.table("user_alert_rules").insert(payload).execute()
        return {"rule": response.data[0] if response.data else payload}
    except Exception:
        logger.exception("Alert-rule creation failed for %s", user_id)
        raise HTTPException(status_code=502, detail="Alert rule could not be saved")


def load_user_discipline_settings(user_id: str) -> dict:
    try:
        response = (
            supabase.table("user_discipline_settings")
            .select("*")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if response.data:
            allowed = set(DisciplineSettingsInput.model_fields)
            return normalize_discipline_settings({
                key: value for key, value in response.data[0].items() if key in allowed
            })
        return normalize_discipline_settings({})
    except Exception:
        logger.exception("Discipline settings lookup failed for %s", user_id)
        raise HTTPException(status_code=502, detail="Discipline settings are temporarily unavailable")


@app.get("/api/discipline/settings")
def get_discipline_settings(
    authorization: Annotated[str | None, Header()] = None,
):
    user_id, permissions = require_authenticated_tier(authorization, "premium_scanner")
    return {
        "settings": load_user_discipline_settings(user_id),
        "hard_lock_available": permissions["tier"] == "autonomous_bot",
        "rules_are_user_authored": True,
    }


@app.put("/api/discipline/settings")
def save_discipline_settings(
    settings: DisciplineSettingsInput,
    authorization: Annotated[str | None, Header()] = None,
):
    user_id, permissions = require_authenticated_tier(authorization, "premium_scanner")
    payload = normalize_discipline_settings(settings.model_dump())
    if payload["mode"] == "locked" and permissions["tier"] != "autonomous_bot":
        raise HTTPException(status_code=403, detail="Locked mode requires the Elite tier")
    try:
        response = supabase.table("user_discipline_settings").upsert(
            {"user_id": user_id, **payload}, on_conflict="user_id"
        ).execute()
        return {
            "settings": response.data[0] if response.data else payload,
            "rules_are_user_authored": True,
            "live_broker_submission_enabled": False,
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Discipline settings save failed for %s", user_id)
        raise HTTPException(status_code=502, detail="Discipline settings could not be saved")


@app.post("/api/discipline/evaluate-order")
def evaluate_order_against_user_rules(
    order: DisciplineOrderInput,
    authorization: Annotated[str | None, Header()] = None,
):
    user_id, _permissions = require_authenticated_tier(authorization, "premium_scanner")
    settings = load_user_discipline_settings(user_id)
    result = evaluate_discipline_order(order, settings)
    try:
        saved = supabase.table("discipline_evaluations").insert({
            "user_id": user_id,
            "ticker": result["ticker"],
            "settings_snapshot": settings,
            "order_snapshot": order.model_dump(),
            "violations": result["violations"],
            "unable_to_evaluate": result["unable_to_evaluate"],
            "discipline_gate_passed": result["discipline_gate_passed"],
            "acknowledgement_used": order.acknowledge_override,
            "broker_order_submitted": False,
        }).execute()
        result["evaluation_id"] = saved.data[0].get("id") if saved.data else None
        return result
    except Exception:
        logger.exception("Discipline evaluation save failed for %s", user_id)
        raise HTTPException(status_code=502, detail="Discipline evaluation could not be recorded")


@app.post("/api/discipline/review-trade", status_code=201)
def create_trade_process_review(
    trade: TradeProcessReviewInput,
    authorization: Annotated[str | None, Header()] = None,
):
    user_id, _permissions = require_authenticated_tier(authorization, "premium_scanner")
    result = review_trade_process(trade)
    try:
        saved = supabase.table("trade_process_reviews").insert({
            "user_id": user_id,
            "ticker": result["ticker"],
            "trade_snapshot": trade.model_dump(),
            "discipline_score": result["discipline_score"],
            "process_classification": result["process_classification"],
            "findings": result["findings"],
        }).execute()
        result["review_id"] = saved.data[0].get("id") if saved.data else None
        return result
    except Exception:
        logger.exception("Trade process review save failed for %s", user_id)
        raise HTTPException(status_code=502, detail="Trade review could not be recorded")


@app.get("/api/discipline/reviews")
def get_trade_process_reviews(
    limit: int = Query(25, ge=1, le=100),
    authorization: Annotated[str | None, Header()] = None,
):
    user_id, _permissions = require_authenticated_tier(authorization, "premium_scanner")
    try:
        response = (
            supabase.table("trade_process_reviews")
            .select("*")
            .eq("user_id", user_id)
            .order("reviewed_at", desc=True)
            .limit(limit)
            .execute()
        )
        return {"reviews": response.data or []}
    except Exception:
        logger.exception("Trade process review lookup failed for %s", user_id)
        raise HTTPException(status_code=502, detail="Trade reviews are temporarily unavailable")


@app.get("/api/discipline/summary")
def get_discipline_summary(
    authorization: Annotated[str | None, Header()] = None,
):
    user_id, _permissions = require_authenticated_tier(authorization, "premium_scanner")
    try:
        response = (
            supabase.table("trade_process_reviews")
            .select("discipline_score,process_classification,findings,reviewed_at")
            .eq("user_id", user_id)
            .order("reviewed_at", desc=True)
            .limit(500)
            .execute()
        )
        return summarize_trade_reviews(response.data or [])
    except Exception:
        logger.exception("Discipline summary lookup failed for %s", user_id)
        raise HTTPException(status_code=502, detail="Discipline summary is temporarily unavailable")


@app.post("/api/legal/acceptances", status_code=201)
def record_legal_acceptance(
    acceptance: LegalAcceptanceInput,
    authorization: Annotated[str | None, Header()] = None,
    user_agent: Annotated[str | None, Header()] = None,
):
    user_id = get_authenticated_user_id(authorization)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    document_type = acceptance.document_type.strip().lower()
    allowed_documents = {"terms", "privacy", "risk_disclosure", "broker_terms"}
    if document_type not in allowed_documents:
        raise HTTPException(status_code=422, detail="Unsupported document type")
    if not acceptance.accepted:
        raise HTTPException(status_code=422, detail="Acceptance must be affirmative")
    try:
        existing = (
            supabase.table("user_legal_acceptances")
            .select("id,document_type,document_version,document_sha256,accepted_at")
            .eq("user_id", user_id)
            .eq("document_type", document_type)
            .eq("document_version", acceptance.document_version)
            .limit(1)
            .execute()
        )
        if existing.data:
            if existing.data[0].get("document_sha256", "").lower() != acceptance.document_sha256.lower():
                raise HTTPException(
                    status_code=409,
                    detail="This document version is already associated with different content",
                )
            return {
                "acceptance": existing.data[0],
                "already_recorded": True,
                "legal_effect_notice": (
                    "This record documents consent. It does not waive securities laws or enable live orders."
                ),
            }
        response = supabase.table("user_legal_acceptances").insert({
            "user_id": user_id,
            "document_type": document_type,
            "document_version": acceptance.document_version,
            "document_sha256": acceptance.document_sha256.lower(),
            "acceptance_source": "flutterflow",
            "user_agent": user_agent,
        }).execute()
        return {
            "acceptance": response.data[0] if response.data else {
                "document_type": document_type,
                "document_version": acceptance.document_version,
                "document_sha256": acceptance.document_sha256.lower(),
            },
            "already_recorded": False,
            "legal_effect_notice": (
                "This record documents consent. It does not waive securities laws or enable live orders."
            ),
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Legal acceptance save failed for %s", user_id)
        raise HTTPException(status_code=502, detail="Acceptance could not be recorded")


@app.get("/api/legal/acceptances")
def get_legal_acceptances(
    authorization: Annotated[str | None, Header()] = None,
):
    user_id = get_authenticated_user_id(authorization)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        response = (
            supabase.table("user_legal_acceptances")
            .select("document_type,document_version,document_sha256,accepted_at")
            .eq("user_id", user_id)
            .order("accepted_at", desc=True)
            .execute()
        )
        return {"acceptances": response.data or []}
    except Exception:
        logger.exception("Legal acceptance lookup failed for %s", user_id)
        raise HTTPException(status_code=502, detail="Acceptances are temporarily unavailable")


@app.get("/api/brokers/capabilities")
def get_broker_capabilities():
    """Expose planned providers and the honest runtime execution state."""
    return broker_capability_manifest(
        connections_enabled=BROKER_CONNECTIONS_ENABLED,
        paper_enabled=BROKER_PAPER_TRADING_ENABLED,
        live_enabled=BROKER_LIVE_TRADING_ENABLED,
        global_kill_switch=BROKER_GLOBAL_KILL_SWITCH,
    )


@app.get("/api/brokers/connections")
def get_broker_connections(
    authorization: Annotated[str | None, Header()] = None,
):
    """Return non-secret connection metadata for the signed-in Elite user."""
    user_id, _permissions = require_authenticated_tier(authorization, "autonomous_bot")
    if not BROKER_CONNECTIONS_ENABLED:
        return {
            "connections_enabled": False,
            "connections": [],
            "message": "Broker connections are being prepared and are not active.",
        }
    if supabase is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    try:
        response = (
            supabase.table("broker_connections")
            .select(
                "id,provider,connection_status,scopes,connected_at,last_verified_at,revoked_at"
            )
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return {"connections_enabled": True, "connections": response.data or []}
    except Exception:
        logger.exception("Broker connection lookup failed for %s", user_id)
        raise HTTPException(status_code=502, detail="Broker connections are temporarily unavailable")


def build_bracket_order_preview(order: BracketOrderPreviewInput) -> dict:
    """Validate user-supplied bracket values without authentication or submission."""
    ticker = normalize_ticker(order.ticker)
    side = order.side.strip().lower()
    order_type = order.order_type.strip().lower()
    if side not in {"buy", "sell"}:
        raise HTTPException(status_code=422, detail="Side must be buy or sell")
    if order_type not in {"market", "limit"}:
        raise HTTPException(status_code=422, detail="Order type must be market or limit")
    if side == "buy" and not (order.stop_loss_price < order.entry_price < order.take_profit_price):
        raise HTTPException(status_code=422, detail="A buy bracket requires stop < entry < take profit")
    if side == "sell" and not (order.take_profit_price < order.entry_price < order.stop_loss_price):
        raise HTTPException(status_code=422, detail="A sell bracket requires take profit < entry < stop")

    risk_per_share = abs(order.entry_price - order.stop_loss_price)
    reward_per_share = abs(order.take_profit_price - order.entry_price)
    return {
        **order.model_dump(),
        "ticker": ticker,
        "side": side,
        "order_type": order_type,
        "estimated_notional": round(order.quantity * order.entry_price, 2),
        "maximum_planned_loss": round(order.quantity * risk_per_share, 2),
        "planned_profit_target": round(order.quantity * reward_per_share, 2),
        "reward_to_risk": round(reward_per_share / risk_per_share, 2),
    }


@app.post("/api/orders/bracket-preview")
def preview_bracket_order(
    order: BracketOrderPreviewInput,
    authorization: Annotated[str | None, Header()] = None,
):
    """Validate and preview an Elite bracket order; this endpoint never submits it."""
    _user_id, _permissions = require_authenticated_tier(authorization, "autonomous_bot")
    preview = build_bracket_order_preview(order)
    return {
        "status": "PREVIEW_ONLY",
        "requires_verification_code": True,
        "broker_order_submitted": False,
        "created_from_user_inputs": True,
        "investment_recommendation": False,
        "order": preview,
        "next_step": "Connect and approve a supported broker before live submission is enabled",
    }


@app.post("/api/brokers/order-intents", status_code=201)
def record_broker_order_intent(
    intent: BrokerOrderIntentInput,
    authorization: Annotated[str | None, Header()] = None,
    idempotency_key: Annotated[str | None, Header()] = None,
):
    """Record a reviewed intent exactly once; never route it to a broker."""
    user_id, _permissions = require_authenticated_tier(authorization, "autonomous_bot")
    if not intent.user_confirmed:
        raise HTTPException(status_code=422, detail="The user must affirmatively confirm the order intent")
    try:
        provider = normalize_broker_provider(intent.provider)
        stable_key = validate_idempotency_key(idempotency_key)
    except BrokerIntegrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    preview = build_bracket_order_preview(intent.order)
    fingerprint = order_request_fingerprint(provider, preview)
    if supabase is None:
        raise HTTPException(status_code=503, detail="Database is not configured")

    try:
        existing = (
            supabase.table("broker_order_intents")
            .select("id,provider,request_fingerprint,status,order_payload,created_at")
            .eq("user_id", user_id)
            .eq("idempotency_key", stable_key)
            .limit(1)
            .execute()
        )
        if existing.data:
            saved = existing.data[0]
            if saved.get("request_fingerprint") != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="That Idempotency-Key was already used for a different order intent",
                )
            return {
                "order_intent": saved,
                "already_recorded": True,
                "broker_order_submitted": False,
            }

        row = {
            "user_id": user_id,
            "broker_connection_id": intent.broker_connection_id,
            "discipline_evaluation_id": intent.discipline_evaluation_id,
            "provider": provider,
            "idempotency_key": stable_key,
            "request_fingerprint": fingerprint,
            "order_payload": preview,
            "user_confirmed": True,
            "status": "recorded_not_submitted",
        }
        saved = supabase.table("broker_order_intents").insert(row).execute()
        return {
            "order_intent": saved.data[0] if saved.data else row,
            "already_recorded": False,
            "broker_order_submitted": False,
            "live_submission_enabled": False,
            "message": "Intent recorded for audit and future paper workflow; no broker order was sent.",
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Broker order-intent save failed for %s", user_id)
        raise HTTPException(status_code=502, detail="Order intent could not be recorded")

@app.post("/api/execute-autonomous-token")
def execute_autonomous_token(
    gate: OrderGateInput,
    authorization: Annotated[str | None, Header()] = None,
):
    verified_user_id = get_authenticated_user_id(authorization)
    if verified_user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    permissions = check_user_tier_permissions(verified_user_id, "autonomous_bot")
    if not permissions["is_authorized"]:
        raise HTTPException(status_code=403, detail="Unauthorized subscription tier access limits.")
    try:
        evaluation = (
            supabase.table("discipline_evaluations")
            .select("id,discipline_gate_passed,violations,unable_to_evaluate,evaluated_at")
            .eq("id", gate.discipline_evaluation_id)
            .eq("user_id", verified_user_id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception("Discipline-gate verification failed for %s", verified_user_id)
        raise HTTPException(status_code=502, detail="Discipline evaluation could not be verified")
    if not evaluation.data:
        raise HTTPException(status_code=404, detail="Discipline evaluation not found")
    saved_evaluation = evaluation.data[0]
    if (
        not saved_evaluation.get("discipline_gate_passed")
        or bool(saved_evaluation.get("unable_to_evaluate"))
    ):
        return {
            "status": "BLOCKED_BY_USER_RULES",
            "broker_order_submitted": False,
            "violations": saved_evaluation.get("violations") or [],
            "unable_to_evaluate": saved_evaluation.get("unable_to_evaluate") or [],
            "message": "FloatIQ stopped its workflow because the plan could not pass every rule you locked.",
        }
    try:
        evaluated_at = datetime.fromisoformat(str(saved_evaluation.get("evaluated_at", "")).replace("Z", "+00:00"))
        if evaluated_at.tzinfo is None:
            evaluated_at = evaluated_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        evaluated_at = None
    if evaluated_at is None or datetime.now(timezone.utc) - evaluated_at > timedelta(minutes=5):
        return {
            "status": "DISCIPLINE_EVALUATION_EXPIRED",
            "broker_order_submitted": False,
            "message": "Re-evaluate this order plan before continuing.",
        }
    return {
        "status": "BROKER_CONNECTION_REQUIRED",
        "broker_order_submitted": False,
        "message": "A verification code was received, but it was not validated and no order was submitted. Connect a broker and verification provider before enabling live orders.",
    }
