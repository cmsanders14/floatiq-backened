"""Deterministic deployment checks that never print credential values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse


TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class PreflightFinding:
    severity: str
    code: str
    message: str


def _enabled(config: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = config.get(name)
    return default if raw is None else raw.strip().lower() in TRUE_VALUES


def _present(config: Mapping[str, str], name: str) -> bool:
    value = config.get(name, "").strip()
    return bool(value) and value.lower() not in {
        "changeme",
        "placeholder",
        "replace-me",
        "replace_me",
    }


def _https_url(config: Mapping[str, str], name: str) -> bool:
    value = config.get(name, "").strip()
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def assess_deployment_config(
    config: Mapping[str, str], *, mode: str = "production"
) -> list[PreflightFinding]:
    """Return safe findings for a deployment environment without echoing values."""
    if mode not in {"production", "template"}:
        raise ValueError("mode must be production or template")

    findings: list[PreflightFinding] = []

    def add(severity: str, code: str, message: str) -> None:
        findings.append(PreflightFinding(severity, code, message))

    if mode == "production":
        if not _https_url(config, "SUPABASE_URL"):
            add("error", "supabase_url", "SUPABASE_URL must be a valid HTTPS URL.")
        if not _present(config, "SUPABASE_SERVICE_KEY"):
            add("error", "supabase_service_key", "SUPABASE_SERVICE_KEY is not configured.")

        origins = [item.strip() for item in config.get("CORS_ORIGINS", "").split(",") if item.strip()]
        if not origins or "*" in origins:
            add("error", "cors_wildcard", "CORS_ORIGINS must list explicit production HTTPS origins.")
        elif any(urlparse(origin).scheme != "https" or not urlparse(origin).netloc for origin in origins):
            add("error", "cors_https", "Every production CORS origin must be a valid HTTPS origin.")

        if _enabled(config, "PUBLIC_LAUNCH_ENABLED") and not _enabled(config, "ENABLE_HSTS"):
            add("error", "hsts_disabled", "ENABLE_HSTS must be true before public launch.")
        if _enabled(config, "PUBLIC_LAUNCH_ENABLED") and _enabled(config, "BETA_MODE_ENABLED", True):
            add("warning", "beta_public_overlap", "Disable beta mode when switching to public launch.")

        live_feed = _enabled(config, "SUPERNOVA_FEED_ENABLED") or _enabled(
            config, "SESSION_SCANNER_FEED_ENABLED"
        )
        if live_feed and config.get("MARKET_DATA_PROVIDER", "").strip().lower() in {"", "yahoo", "yfinance"}:
            add("error", "unlicensed_live_feed", "Live feed flags require the licensed market-data adapter.")

        if not _enabled(config, "RATE_LIMIT_ENABLED", True):
            add("error", "rate_limit_disabled", "RATE_LIMIT_ENABLED must remain true in production.")

    if _enabled(config, "BILLING_ENABLED"):
        for name in (
            "STRIPE_SECRET_KEY",
            "STRIPE_WEBHOOK_SECRET",
            "STRIPE_PRICE_PRO",
            "STRIPE_PRICE_ELITE",
        ):
            if not _present(config, name):
                add("error", f"billing_{name.lower()}", f"{name} is required when billing is enabled.")
        for name in ("BILLING_SUCCESS_URL", "BILLING_CANCEL_URL", "BILLING_PORTAL_RETURN_URL"):
            if not _https_url(config, name):
                add("error", f"billing_{name.lower()}", f"{name} must be a valid HTTPS URL.")

    if _enabled(config, "BROKER_LIVE_TRADING_ENABLED"):
        add("error", "live_broker_unsupported", "Live broker submission is not implemented and must remain disabled.")
    if not _enabled(config, "BROKER_GLOBAL_KILL_SWITCH", True):
        add("error", "broker_kill_switch", "The broker global kill switch must remain engaged.")

    return findings


def preflight_summary(findings: list[PreflightFinding]) -> dict:
    errors = sum(item.severity == "error" for item in findings)
    warnings = sum(item.severity == "warning" for item in findings)
    return {
        "passed": errors == 0,
        "error_count": errors,
        "warning_count": warnings,
        "findings": [item.__dict__ for item in findings],
    }
