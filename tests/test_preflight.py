from floatiq_core.preflight import assess_deployment_config, preflight_summary


def production_config(**overrides):
    config = {
        "SUPABASE_URL": "https://project.supabase.co",
        "SUPABASE_SERVICE_KEY": "configured-server-secret",
        "CORS_ORIGINS": "https://floatiqanalytics.com,https://www.floatiqanalytics.com",
        "PUBLIC_LAUNCH_ENABLED": "false",
        "BETA_MODE_ENABLED": "true",
        "ENABLE_HSTS": "false",
        "MARKET_DATA_PROVIDER": "yahoo",
        "SUPERNOVA_FEED_ENABLED": "false",
        "SESSION_SCANNER_FEED_ENABLED": "false",
        "RATE_LIMIT_ENABLED": "true",
        "BILLING_ENABLED": "false",
        "BROKER_LIVE_TRADING_ENABLED": "false",
        "BROKER_GLOBAL_KILL_SWITCH": "true",
    }
    config.update(overrides)
    return config


def finding_codes(config, *, mode="production"):
    return {item.code for item in assess_deployment_config(config, mode=mode)}


def test_invite_only_production_configuration_passes():
    result = preflight_summary(assess_deployment_config(production_config()))
    assert result["passed"] is True
    assert result["findings"] == []


def test_production_rejects_wildcard_cors_and_unsafe_public_launch():
    codes = finding_codes(production_config(
        CORS_ORIGINS="*",
        PUBLIC_LAUNCH_ENABLED="true",
        ENABLE_HSTS="false",
    ))
    assert {"cors_wildcard", "hsts_disabled", "beta_public_overlap"} <= codes


def test_live_flags_reject_test_market_data_provider():
    assert "unlicensed_live_feed" in finding_codes(
        production_config(SESSION_SCANNER_FEED_ENABLED="true")
    )


def test_enabled_billing_requires_complete_https_configuration():
    codes = finding_codes(production_config(BILLING_ENABLED="true"))
    assert "billing_stripe_secret_key" in codes
    assert "billing_billing_success_url" in codes


def test_broker_live_submission_always_fails_closed():
    codes = finding_codes(production_config(
        BROKER_LIVE_TRADING_ENABLED="true",
        BROKER_GLOBAL_KILL_SWITCH="false",
    ))
    assert {"live_broker_unsupported", "broker_kill_switch"} <= codes


def test_template_mode_accepts_intentionally_blank_secrets_and_wildcard_cors():
    config = production_config(
        SUPABASE_SERVICE_KEY="",
        CORS_ORIGINS="*",
        SUPABASE_URL="https://project.supabase.co",
    )
    assert assess_deployment_config(config, mode="template") == []
