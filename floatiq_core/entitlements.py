"""Canonical FloatIQ subscription contract used by every API endpoint."""

TIER_CONFIG = {
    "free": {
        "display_name": "Free",
        "price_monthly": 0.0,
        "max_charts": 2,
        "pattern_result_limit": 3,
        "win_rate_access": "one_per_chart_daily",
        "large_trade_alerts": False,
        "supernova_radar": False,
        "custom_supernova_thresholds": False,
        "custom_setup_alerts": False,
        "bracket_order_access": False,
        "discipline_level": "basic_calculator",
    },
    "premium_scanner": {
        "display_name": "FloatIQ Pro",
        "price_monthly": 19.99,
        "max_charts": 4,
        "pattern_result_limit": 10,
        "win_rate_access": "all",
        "large_trade_alerts": True,
        "supernova_radar": True,
        "custom_supernova_thresholds": False,
        "custom_setup_alerts": False,
        "bracket_order_access": False,
        "discipline_level": "coach",
    },
    "autonomous_bot": {
        "display_name": "FloatIQ Elite",
        "price_monthly": 39.99,
        "max_charts": 6,
        "pattern_result_limit": 10,
        "win_rate_access": "all",
        "large_trade_alerts": True,
        "supernova_radar": True,
        "custom_supernova_thresholds": True,
        "custom_setup_alerts": True,
        "bracket_order_access": True,
        "discipline_level": "broker_gate_ready",
    },
}
TIER_WEIGHTS = {"free": 0, "premium_scanner": 1, "autonomous_bot": 2}


def tier_entitlements(tier: str) -> dict:
    canonical_tier = tier if tier in TIER_CONFIG else "free"
    return {"tier": canonical_tier, **TIER_CONFIG[canonical_tier]}
