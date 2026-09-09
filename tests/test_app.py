import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
from fastapi import HTTPException
from fastapi.testclient import TestClient


APP_PATH = Path(__file__).parents[1] / "FloatIQ Analytics.py"
SPEC = importlib.util.spec_from_file_location("floatiq_app", APP_PATH)
floatiq = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(floatiq)
client = TestClient(floatiq.app)


class FakeTicker:
    info = {
        "sector": "Technology",
        "industry": "Software",
        "marketCap": 1_000_000_000,
        "shortPercentOfFloat": 0.04,
    }


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, data):
        self.data = data

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def execute(self):
        return FakeResponse(self.data)


class FakeSupabase:
    def __init__(self, data):
        self.data = data

    def table(self, _name):
        return FakeQuery(self.data)


class FakeRpcSupabase:
    def __init__(self, data=True):
        self.data = data
        self.rpc_name = None
        self.rpc_params = None

    def rpc(self, name, params):
        self.rpc_name = name
        self.rpc_params = params
        return FakeQuery(self.data)


def synthetic_prices():
    rng = np.random.default_rng(42)
    index = pd.date_range("2025-01-01", periods=260, freq="B")
    close = 25 + np.cumsum(rng.normal(0.03, 0.5, len(index)))
    open_price = close + rng.normal(0, 0.35, len(index))
    high = np.maximum(open_price, close) + rng.uniform(0.1, 0.8, len(index))
    low = np.minimum(open_price, close) - rng.uniform(0.1, 0.8, len(index))
    volume = rng.integers(1_000_000, 6_000_000, len(index))
    return pd.DataFrame(
        {
            "Open": open_price,
            "High": high,
            "Low": low,
            "Close": close,
            "Adj Close": close,
            "Volume": volume,
        },
        index=index,
    )


class FloatIQTests(unittest.TestCase):
    def test_http_health_contract(self):
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_http_health_accepts_head_probes(self):
        response = client.head("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")

    def test_http_private_route_is_protected(self):
        response = client.get("/api/user-journal-summary")
        self.assertEqual(response.status_code, 401)

    def test_health_check_without_database(self):
        result = floatiq.health_check()
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["database_configured"])

    def test_guest_receives_free_tier(self):
        result = floatiq.check_user_tier_permissions("guest_user", "free")
        self.assertEqual(result["tier"], "free")
        self.assertEqual(result["max_charts"], 2)
        self.assertTrue(result["is_authorized"])

    def test_inactive_or_expired_subscription_falls_back_to_free(self):
        inactive = [{
            "tier_level": "autonomous_bot",
            "status": "canceled",
            "current_period_end": "2099-01-01T00:00:00Z",
        }]
        expired = [{
            "tier_level": "autonomous_bot",
            "status": "active",
            "current_period_end": "2020-01-01T00:00:00Z",
        }]
        with patch.object(floatiq, "supabase", FakeSupabase(inactive)):
            inactive_result = floatiq.check_user_tier_permissions("user-1", "free")
        with patch.object(floatiq, "supabase", FakeSupabase(expired)):
            expired_result = floatiq.check_user_tier_permissions("user-1", "free")
        self.assertEqual(inactive_result["tier"], "free")
        self.assertEqual(expired_result["tier"], "free")
        self.assertEqual(expired_result["subscription_status"], "expired")

    def test_pattern_pipeline_processes_full_history(self):
        prices = synthetic_prices()
        floatiq._MARKET_DATA_CACHE.clear()
        with patch.object(floatiq.yf, "download", return_value=prices.copy()), patch.object(
            floatiq.yf, "Ticker", return_value=FakeTicker()
        ):
            result = floatiq.get_probabilities("PLTR", "1y", "1d", "guest_user")

        self.assertNotIn("error", result)
        self.assertEqual(result["max_allowed_charts"], 2)
        self.assertGreater(len(result["patterns"]), 0)
        self.assertLessEqual(len(result["patterns"]), 3)
        self.assertTrue(all(item["win_rate_locked"] for item in result["patterns"]))
        self.assertTrue(all(item["overall_probability_win_rate"] is None for item in result["patterns"]))

    def test_tier_contract_matches_product_pricing(self):
        free = floatiq.tier_entitlements("free")
        pro = floatiq.tier_entitlements("premium_scanner")
        elite = floatiq.tier_entitlements("autonomous_bot")

        self.assertEqual((free["max_charts"], free["pattern_result_limit"]), (2, 3))
        self.assertEqual((pro["max_charts"], pro["pattern_result_limit"]), (4, 10))
        self.assertEqual(
            (elite["max_charts"], elite["pattern_result_limit"], elite["price_monthly"]),
            (6, 10, 39.99),
        )
        self.assertTrue(pro["large_trade_alerts"])
        self.assertTrue(pro["supernova_radar"])
        self.assertFalse(pro["custom_supernova_thresholds"])
        self.assertTrue(elite["supernova_radar"])
        self.assertTrue(elite["custom_supernova_thresholds"])
        self.assertTrue(elite["bracket_order_access"])

    def test_free_entitlements_redact_paid_statistics(self):
        patterns = [
            {
                "pattern_key": f"pattern-{index}",
                "overall_probability_win_rate": "90%",
                "historical_win_rate_pct": 90.0,
                "confidence_interval_95": {"low_pct": 70, "high_pct": 98},
                "thirty_day_recent_probability": "92%",
            }
            for index in range(5)
        ]
        result = floatiq.apply_pattern_entitlements(
            patterns,
            floatiq.tier_entitlements("free"),
            "guest_user",
            1,
            None,
        )
        self.assertEqual(len(result), 3)
        self.assertTrue(all(item["win_rate_locked"] for item in result))
        self.assertTrue(all(item["historical_win_rate_pct"] is None for item in result))
        self.assertTrue(all(item["confidence_interval_95"] is None for item in result))

    def test_wilson_interval_is_bounded_and_reports_uncertainty(self):
        interval = floatiq.wilson_confidence_interval(1, 2)
        self.assertGreaterEqual(interval["low_pct"], 0)
        self.assertLessEqual(interval["high_pct"], 100)
        self.assertGreater(interval["high_pct"] - interval["low_pct"], 50)

    def test_daily_reveal_uses_atomic_database_function(self):
        database = FakeRpcSupabase(True)
        with patch.object(floatiq, "supabase", database):
            claimed = floatiq.claim_daily_win_rate_reveal(
                "5f71582c-1b33-4f98-b128-8d2f14970db5", 2, "bull-flag"
            )
        self.assertTrue(claimed)
        self.assertEqual(database.rpc_name, "claim_daily_feature_selection")
        self.assertEqual(database.rpc_params["p_resource_key"], "chart:2")

    def test_bracket_preview_math(self):
        order = floatiq.BracketOrderPreviewInput(
            ticker="pltr",
            quantity=20,
            entry_price=120,
            stop_loss_price=114,
            take_profit_price=132,
        )
        with patch.object(
            floatiq,
            "require_authenticated_tier",
            return_value=("user-1", {"tier": "autonomous_bot"}),
        ):
            result = floatiq.preview_bracket_order(order)
        self.assertEqual(result["status"], "PREVIEW_ONLY")
        self.assertFalse(result["broker_order_submitted"])
        self.assertEqual(result["order"]["maximum_planned_loss"], 120)
        self.assertEqual(result["order"]["reward_to_risk"], 2.0)

    def test_broker_capability_endpoint_is_live_disabled(self):
        result = floatiq.get_broker_capabilities()
        self.assertFalse(result["live_order_submission_enabled"])
        self.assertTrue(result["global_kill_switch_engaged"])
        self.assertEqual(
            {provider["provider_key"] for provider in result["providers"]},
            {"schwab", "webull", "robinhood"},
        )

    def test_large_trade_score_is_relative_to_the_asset(self):
        thin_asset = floatiq.LargeTradeContext(
            ticker="SNLD",
            quantity=20_000,
            price=5,
            average_trade_quantity=500,
            average_daily_volume=400_000,
            relative_volume=6,
            float_or_circulating_units=8_000_000,
        )
        liquid_asset = floatiq.LargeTradeContext(
            ticker="BTC-USD",
            asset_type="crypto",
            quantity=20_000,
            price=5,
            average_trade_quantity=50_000,
            average_daily_volume=100_000_000,
            relative_volume=1,
            float_or_circulating_units=20_000_000,
        )
        thin_result = floatiq.score_large_trade(thin_asset)
        liquid_result = floatiq.score_large_trade(liquid_asset)

        self.assertGreater(thin_result["score"], liquid_result["score"])
        self.assertTrue(thin_result["alert_eligible"])
        self.assertFalse(liquid_result["alert_eligible"])
        self.assertFalse(thin_result["identity_claimed"])
        self.assertNotEqual(thin_result["classification"], "probable_institutional_activity")

    def test_market_alert_api_redacts_unverified_actor_identity(self):
        alerts = [{
            "event_type": "institutional_scale_trade",
            "verification_status": "confirmed_market_data",
            "source_name": "feed",
            "actor_name": "Unverified Person",
            "actor_role": "CEO",
        }]
        with patch.object(floatiq, "supabase", FakeSupabase(alerts)), patch.object(
            floatiq, "require_authenticated_tier", return_value=("user-1", {})
        ):
            result = floatiq.get_market_alerts()
        self.assertIsNone(result["alerts"][0]["actor_name"])
        self.assertIsNone(result["alerts"][0]["actor_role"])

    def test_three_percent_scanner_uses_three_percent_outcome(self):
        raw = {
            "patterns": [{
                "historical_win_rate_pct": 75.0,
                "overall_probability_win_rate": "75.0%",
                "mathematical_expectancy_score": 1.0,
            }],
            "current_market_environment": "UNKNOWN",
        }
        with patch.object(floatiq, "get_probabilities", return_value=raw) as probabilities:
            result = floatiq.get_premade_3pct_scalp_candidates()
        self.assertEqual(probabilities.call_args.kwargs["target_return_pct"], 3.0)
        self.assertEqual(result["total_setups_found"], 1)

    def test_supernova_requires_follow_through_before_confirmation(self):
        base = {
            "ticker": "PLTR",
            "price": 120,
            "relative_volume_at_time": 8,
            "volume_acceleration": 5,
            "price_change_pct": 8,
            "range_breakout_pct": 3,
            "vwap_distance_pct": 4,
            "average_dollar_volume": 50_000_000,
            "spread_bps": 10,
            "float_turnover_pct": 5,
        }
        triggered = floatiq.score_supernova_candidate(
            floatiq.SupernovaCandidateInput(**base)
        )
        confirmed = floatiq.score_supernova_candidate(
            floatiq.SupernovaCandidateInput(**base, follow_through_confirmed=True)
        )
        self.assertEqual(triggered["stage"], "TRIGGERED")
        self.assertEqual(confirmed["stage"], "CONFIRMED_MOMENTUM")
        self.assertFalse(triggered["prediction_claimed"])

    def test_supernova_quality_filter_suppresses_illiquid_candidate(self):
        candidate = floatiq.SupernovaCandidateInput(
            ticker="TEST",
            price=1,
            relative_volume_at_time=10,
            volume_acceleration=10,
            price_change_pct=15,
            range_breakout_pct=5,
            vwap_distance_pct=4,
            average_dollar_volume=10_000,
            spread_bps=300,
            float_turnover_pct=8,
            follow_through_confirmed=True,
        )
        result = floatiq.score_supernova_candidate(candidate)
        self.assertEqual(result["stage"], "SUPPRESSED")
        self.assertFalse(result["alert_eligible"])
        self.assertIn("spread_too_wide", result["quality_failures"])

    def test_supernova_radar_is_prebuilt_for_pro_and_waits_for_live_feed(self):
        with patch.object(
            floatiq,
            "require_authenticated_tier",
            return_value=("user-1", floatiq.tier_entitlements("premium_scanner")),
        ), patch.object(floatiq, "SUPERNOVA_FEED_ENABLED", False):
            result = floatiq.get_supernova_radar()
        self.assertTrue(result["included_with_subscription"])
        self.assertEqual(result["data_status"], "awaiting_live_feed")
        self.assertEqual(result["candidates"], [])
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
from fastapi import HTTPException
from fastapi.testclient import TestClient


APP_PATH = Path(__file__).parents[1] / "FloatIQ Analytics.py"
SPEC = importlib.util.spec_from_file_location("floatiq_app", APP_PATH)
floatiq = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(floatiq)
client = TestClient(floatiq.app)


class FakeTicker:
    info = {
        "sector": "Technology",
        "industry": "Software",
        "marketCap": 1_000_000_000,
        "shortPercentOfFloat": 0.04,
    }


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, data):
        self.data = data

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def execute(self):
        return FakeResponse(self.data)


class FakeSupabase:
    def __init__(self, data):
        self.data = data

    def table(self, _name):
        return FakeQuery(self.data)


class FakeRpcSupabase:
    def __init__(self, data=True):
        self.data = data
        self.rpc_name = None
        self.rpc_params = None

    def rpc(self, name, params):
        self.rpc_name = name
        self.rpc_params = params
        return FakeQuery(self.data)


def synthetic_prices():
    rng = np.random.default_rng(42)
    index = pd.date_range("2025-01-01", periods=260, freq="B")
    close = 25 + np.cumsum(rng.normal(0.03, 0.5, len(index)))
    open_price = close + rng.normal(0, 0.35, len(index))
    high = np.maximum(open_price, close) + rng.uniform(0.1, 0.8, len(index))
    low = np.minimum(open_price, close) - rng.uniform(0.1, 0.8, len(index))
    volume = rng.integers(1_000_000, 6_000_000, len(index))
    return pd.DataFrame(
        {
            "Open": open_price,
            "High": high,
            "Low": low,
            "Close": close,
            "Adj Close": close,
            "Volume": volume,
        },
        index=index,
    )


class FloatIQTests(unittest.TestCase):
    def test_http_health_contract(self):
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_http_health_accepts_head_probes(self):
        response = client.head("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")

    def test_launch_status_exposes_flags_without_allowlist(self):
        response = client.get("/api/launch-status")
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["release_channel"], "invite_only_beta")
        self.assertNotIn("allowlist", result)
        self.assertEqual(result["algorithm_versions"]["session_opportunity"], "session_opportunity_v1")
        self.assertEqual(result["algorithm_versions"]["supernova"], "supernova_v1")

    def test_http_private_route_is_protected(self):
        response = client.get("/api/user-journal-summary")
        self.assertEqual(response.status_code, 401)

    def test_health_check_without_database(self):
        result = floatiq.health_check()
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["database_configured"])

    def test_guest_receives_free_tier(self):
        result = floatiq.check_user_tier_permissions("guest_user", "free")
        self.assertEqual(result["tier"], "free")
        self.assertEqual(result["max_charts"], 2)
        self.assertTrue(result["is_authorized"])

    def test_inactive_or_expired_subscription_falls_back_to_free(self):
        inactive = [{
            "tier_level": "autonomous_bot",
            "status": "canceled",
            "current_period_end": "2099-01-01T00:00:00Z",
        }]
        expired = [{
            "tier_level": "autonomous_bot",
            "status": "active",
            "current_period_end": "2020-01-01T00:00:00Z",
        }]
        with patch.object(floatiq, "supabase", FakeSupabase(inactive)):
            inactive_result = floatiq.check_user_tier_permissions("user-1", "free")
        with patch.object(floatiq, "supabase", FakeSupabase(expired)):
            expired_result = floatiq.check_user_tier_permissions("user-1", "free")
        self.assertEqual(inactive_result["tier"], "free")
        self.assertEqual(expired_result["tier"], "free")
        self.assertEqual(expired_result["subscription_status"], "expired")

    def test_pattern_pipeline_processes_full_history(self):
        prices = synthetic_prices()
        floatiq._MARKET_DATA_CACHE.clear()
        with patch.object(floatiq.yf, "download", return_value=prices.copy()), patch.object(
            floatiq.yf, "Ticker", return_value=FakeTicker()
        ):
            result = floatiq.get_probabilities("PLTR", "1y", "1d", "guest_user")

        self.assertNotIn("error", result)
        self.assertEqual(result["max_allowed_charts"], 2)
        self.assertGreater(len(result["patterns"]), 0)
        self.assertLessEqual(len(result["patterns"]), 3)
        self.assertTrue(all(item["win_rate_locked"] for item in result["patterns"]))
        self.assertTrue(all(item["overall_probability_win_rate"] is None for item in result["patterns"]))

    def test_tier_contract_matches_product_pricing(self):
        free = floatiq.tier_entitlements("free")
        pro = floatiq.tier_entitlements("premium_scanner")
        elite = floatiq.tier_entitlements("autonomous_bot")

        self.assertEqual((free["max_charts"], free["pattern_result_limit"]), (2, 3))
        self.assertEqual((pro["max_charts"], pro["pattern_result_limit"]), (4, 10))
        self.assertEqual(
            (elite["max_charts"], elite["pattern_result_limit"], elite["price_monthly"]),
            (6, 10, 39.99),
        )
        self.assertTrue(pro["large_trade_alerts"])
        self.assertTrue(pro["supernova_radar"])
        self.assertFalse(pro["custom_supernova_thresholds"])
        self.assertTrue(elite["supernova_radar"])
        self.assertTrue(elite["custom_supernova_thresholds"])
        self.assertTrue(elite["bracket_order_access"])

    def test_free_entitlements_redact_paid_statistics(self):
        patterns = [
            {
                "pattern_key": f"pattern-{index}",
                "overall_probability_win_rate": "90%",
                "historical_win_rate_pct": 90.0,
                "confidence_interval_95": {"low_pct": 70, "high_pct": 98},
                "thirty_day_recent_probability": "92%",
            }
            for index in range(5)
        ]
        result = floatiq.apply_pattern_entitlements(
            patterns,
            floatiq.tier_entitlements("free"),
            "guest_user",
            1,
            None,
        )
        self.assertEqual(len(result), 3)
        self.assertTrue(all(item["win_rate_locked"] for item in result))
        self.assertTrue(all(item["historical_win_rate_pct"] is None for item in result))
        self.assertTrue(all(item["confidence_interval_95"] is None for item in result))

    def test_wilson_interval_is_bounded_and_reports_uncertainty(self):
        interval = floatiq.wilson_confidence_interval(1, 2)
        self.assertGreaterEqual(interval["low_pct"], 0)
        self.assertLessEqual(interval["high_pct"], 100)
        self.assertGreater(interval["high_pct"] - interval["low_pct"], 50)

    def test_claim_readiness_hides_small_sample_win_rate(self):
        with patch.object(
            floatiq,
            "require_authenticated_tier",
            return_value=("user-1", {"tier": "premium_scanner"}),
        ):
            small = floatiq.get_claim_readiness(successes=9, total=10)
            ready = floatiq.get_claim_readiness(successes=27, total=30)
        self.assertFalse(small["win_rate_display_allowed"])
        self.assertIsNone(small["confidence_interval_95"])
        self.assertTrue(ready["win_rate_display_allowed"])
        self.assertIsNotNone(ready["confidence_interval_95"])

    def test_daily_reveal_uses_atomic_database_function(self):
        database = FakeRpcSupabase(True)
        with patch.object(floatiq, "supabase", database):
            claimed = floatiq.claim_daily_win_rate_reveal(
                "5f71582c-1b33-4f98-b128-8d2f14970db5", 2, "bull-flag"
            )
        self.assertTrue(claimed)
        self.assertEqual(database.rpc_name, "claim_daily_feature_selection")
        self.assertEqual(database.rpc_params["p_resource_key"], "chart:2")

    def test_bracket_preview_math(self):
        order = floatiq.BracketOrderPreviewInput(
            ticker="pltr",
            quantity=20,
            entry_price=120,
            stop_loss_price=114,
            take_profit_price=132,
        )
        with patch.object(
            floatiq,
            "require_authenticated_tier",
            return_value=("user-1", {"tier": "autonomous_bot"}),
        ):
            result = floatiq.preview_bracket_order(order)
        self.assertEqual(result["status"], "PREVIEW_ONLY")
        self.assertFalse(result["broker_order_submitted"])
        self.assertEqual(result["order"]["maximum_planned_loss"], 120)
        self.assertEqual(result["order"]["reward_to_risk"], 2.0)

    def test_broker_capability_endpoint_is_live_disabled(self):
        result = floatiq.get_broker_capabilities()
        self.assertFalse(result["live_order_submission_enabled"])
        self.assertTrue(result["global_kill_switch_engaged"])
        self.assertEqual(
            {provider["provider_key"] for provider in result["providers"]},
            {"schwab", "webull", "robinhood"},
        )

    def test_large_trade_score_is_relative_to_the_asset(self):
        thin_asset = floatiq.LargeTradeContext(
            ticker="SNLD",
            quantity=20_000,
            price=5,
            average_trade_quantity=500,
            average_daily_volume=400_000,
            relative_volume=6,
            float_or_circulating_units=8_000_000,
        )
        liquid_asset = floatiq.LargeTradeContext(
            ticker="BTC-USD",
            asset_type="crypto",
            quantity=20_000,
            price=5,
            average_trade_quantity=50_000,
            average_daily_volume=100_000_000,
            relative_volume=1,
            float_or_circulating_units=20_000_000,
        )
        thin_result = floatiq.score_large_trade(thin_asset)
        liquid_result = floatiq.score_large_trade(liquid_asset)

        self.assertGreater(thin_result["score"], liquid_result["score"])
        self.assertTrue(thin_result["alert_eligible"])
        self.assertFalse(liquid_result["alert_eligible"])
        self.assertFalse(thin_result["identity_claimed"])
        self.assertNotEqual(thin_result["classification"], "probable_institutional_activity")

    def test_market_alert_api_redacts_unverified_actor_identity(self):
        alerts = [{
            "event_type": "institutional_scale_trade",
            "verification_status": "confirmed_market_data",
            "source_name": "feed",
            "actor_name": "Unverified Person",
            "actor_role": "CEO",
        }]
        with patch.object(floatiq, "supabase", FakeSupabase(alerts)), patch.object(
            floatiq, "require_authenticated_tier", return_value=("user-1", {})
        ):
            result = floatiq.get_market_alerts()
        self.assertIsNone(result["alerts"][0]["actor_name"])
        self.assertIsNone(result["alerts"][0]["actor_role"])

    def test_three_percent_scanner_uses_three_percent_outcome(self):
        raw = {
            "patterns": [{
                "historical_win_rate_pct": 75.0,
                "overall_probability_win_rate": "75.0%",
                "mathematical_expectancy_score": 1.0,
            }],
            "current_market_environment": "UNKNOWN",
        }
        with patch.object(floatiq, "get_probabilities", return_value=raw) as probabilities:
            result = floatiq.get_premade_3pct_scalp_candidates()
        self.assertEqual(probabilities.call_args.kwargs["target_return_pct"], 3.0)
        self.assertEqual(result["total_setups_found"], 1)

    def test_supernova_requires_follow_through_before_confirmation(self):
        base = {
            "ticker": "PLTR",
            "price": 120,
            "relative_volume_at_time": 8,
            "volume_acceleration": 5,
            "price_change_pct": 8,
            "range_breakout_pct": 3,
            "vwap_distance_pct": 4,
            "average_dollar_volume": 50_000_000,
            "spread_bps": 10,
            "float_turnover_pct": 5,
        }
        triggered = floatiq.score_supernova_candidate(
            floatiq.SupernovaCandidateInput(**base)
        )
        confirmed = floatiq.score_supernova_candidate(
            floatiq.SupernovaCandidateInput(**base, follow_through_confirmed=True)
        )
        self.assertEqual(triggered["stage"], "TRIGGERED")
        self.assertEqual(confirmed["stage"], "CONFIRMED_MOMENTUM")
        self.assertFalse(triggered["prediction_claimed"])

    def test_supernova_quality_filter_suppresses_illiquid_candidate(self):
        candidate = floatiq.SupernovaCandidateInput(
            ticker="TEST",
            price=1,
            relative_volume_at_time=10,
            volume_acceleration=10,
            price_change_pct=15,
            range_breakout_pct=5,
            vwap_distance_pct=4,
            average_dollar_volume=10_000,
            spread_bps=300,
            float_turnover_pct=8,
            follow_through_confirmed=True,
        )
        result = floatiq.score_supernova_candidate(candidate)
        self.assertEqual(result["stage"], "SUPPRESSED")
        self.assertFalse(result["alert_eligible"])
        self.assertIn("spread_too_wide", result["quality_failures"])

    def test_supernova_radar_is_prebuilt_for_pro_and_waits_for_live_feed(self):
        with patch.object(
            floatiq,
            "require_authenticated_tier",
            return_value=("user-1", floatiq.tier_entitlements("premium_scanner")),
        ), patch.object(floatiq, "SUPERNOVA_FEED_ENABLED", False):
            result = floatiq.get_supernova_radar()
        self.assertTrue(result["included_with_subscription"])
        self.assertEqual(result["data_status"], "awaiting_live_feed")
        self.assertEqual(result["candidates"], [])
        self.assertFalse(result["custom_thresholds_available"])

    def test_session_scanner_is_prebuilt_and_waits_for_licensed_feed(self):
        with patch.object(
            floatiq,
            "require_authenticated_tier",
            return_value=("user-1", floatiq.tier_entitlements("premium_scanner")),
        ), patch.object(floatiq, "SESSION_SCANNER_FEED_ENABLED", False):
            result = floatiq.get_session_opportunities(
                asset_type=None,
                market_session=None,
                weekend_only=True,
                minimum_score=40,
                include_unconfirmed=False,
                limit=10,
            )
        self.assertTrue(result["scanner"]["weekend_capable"])
        self.assertEqual(result["data_status"], "awaiting_live_feed")
        self.assertEqual(result["candidates"], [])
        self.assertIn("chart availability alone", result["tradability_disclaimer"])

    def test_session_opportunity_keeps_broker_support_explicit(self):
        now = datetime(2026, 9, 12, 14, tzinfo=timezone.utc)
        candidate = floatiq.SessionOpportunityInput(
            ticker="BTC-USD",
            asset_type="crypto",
            price=60_000,
            relative_volume=6,
            average_dollar_volume=500_000_000,
            spread_bps=5,
            price_change_pct=4,
            range_breakout_pct=3,
            vwap_distance_pct=2,
            observed_at=now - timedelta(seconds=15),
            market_session="continuous",
            provider_supports_session=True,
        )
        result = floatiq.score_session_opportunity(candidate, now=now)
        self.assertTrue(result["chartable"])
        self.assertTrue(result["scanner_eligible"])
        self.assertFalse(result["potentially_tradable"])
        self.assertEqual(result["tradability_status"], "broker_support_unknown")
        self.assertFalse(result["trade_execution_available"])

    def test_session_opportunity_carries_versioned_market_regime(self):
        now = datetime(2026, 9, 14, 14, tzinfo=timezone.utc)
        candidate = floatiq.SessionOpportunityInput(
            ticker="PLTR",
            price=120,
            relative_volume=3,
            average_dollar_volume=50_000_000,
            spread_bps=8,
            price_change_pct=2,
            range_breakout_pct=1,
            vwap_distance_pct=1,
            observed_at=now - timedelta(seconds=10),
            market_session="regular",
            provider_supports_session=True,
            benchmark_change_pct=-1.2,
            benchmark_above_20d_ma=False,
            market_breadth_pct=25,
            volatility_percentile=70,
            liquidity_status="normal",
        )
        result = floatiq.score_session_opportunity(candidate, now=now)
        self.assertEqual(result["market_regime"], "bearish_trend")
        self.assertEqual(result["algorithm_version"], "session_opportunity_v1")

    def test_session_feed_circuit_breaker_halts_duplicate_batch(self):
        now = datetime.now(timezone.utc)
        row = {
            "source_name": "licensed_feed",
            "source_event_id": "duplicate-1",
            "ticker": "BTC-USD",
            "asset_type": "crypto",
            "price": 60_000,
            "relative_volume": 3,
            "average_dollar_volume": 100_000_000,
            "spread_bps": 5,
            "price_change_pct": 2,
            "range_breakout_pct": 1,
            "vwap_distance_pct": 1,
            "observed_at": (now - timedelta(seconds=5)).isoformat(),
            "received_at": now.isoformat(),
            "market_session": "continuous",
            "provider_supports_session": True,
        }
        with patch.object(
            floatiq,
            "require_authenticated_tier",
            return_value=("user-1", floatiq.tier_entitlements("premium_scanner")),
        ), patch.object(floatiq, "SESSION_SCANNER_FEED_ENABLED", True), patch.object(
            floatiq, "BETA_ALLOWLIST_USER_IDS", {"user-1"}
        ), patch.object(floatiq, "supabase", FakeSupabase([row, row])):
            result = floatiq.get_session_opportunities(
                asset_type=None,
                market_session=None,
                weekend_only=False,
                minimum_score=40,
                include_unconfirmed=False,
                limit=10,
            )
        self.assertEqual(result["data_status"], "feed_halted")
        self.assertEqual(result["candidates"], [])
        self.assertIn("duplicate_source_events", result["data_quality"]["reason_codes"])

    def test_session_opportunity_rejects_naive_observation_time(self):
        candidate = floatiq.SessionOpportunityInput(
            ticker="PLTR",
            asset_type="equity",
            price=120,
            relative_volume=2,
            average_dollar_volume=20_000_000,
            spread_bps=10,
            price_change_pct=1,
            range_breakout_pct=1,
            vwap_distance_pct=1,
            observed_at=datetime(2026, 9, 12, 14),
        )
        with self.assertRaises(HTTPException) as exc:
            floatiq.score_session_opportunity(candidate)
        self.assertEqual(exc.exception.status_code, 422)

    def test_session_opportunity_rejects_naive_received_time(self):
        observed = datetime(2026, 9, 12, 14, tzinfo=timezone.utc)
        candidate = floatiq.SessionOpportunityInput(
            ticker="PLTR",
            price=120,
            relative_volume=2,
            average_dollar_volume=20_000_000,
            spread_bps=10,
            price_change_pct=1,
            range_breakout_pct=1,
            vwap_distance_pct=1,
            observed_at=observed,
            received_at=datetime(2026, 9, 12, 14),
        )
        with self.assertRaises(HTTPException) as exc:
            floatiq.score_session_opportunity(candidate, now=observed)
        self.assertEqual(exc.exception.status_code, 422)

    def test_supernova_radar_drops_stale_cache_rows(self):
        now = datetime.now(timezone.utc)
        metrics = {
            "price": 10,
            "relative_volume_at_time": 5,
            "price_change_pct": 4,
            "average_dollar_volume": 5_000_000,
            "spread_bps": 20,
            "direction": "up",
        }
        rows = [
            {"ticker": "FRESH", "score": 80, "updated_at": now.isoformat(), "source_name": "feed", "source_event_id": "1", **metrics},
            {"ticker": "WATCH", "score": 45, "updated_at": now.isoformat(), "source_name": "feed", "source_event_id": "2", **metrics},
            {"ticker": "STALE", "score": 99, "updated_at": (now - timedelta(hours=1)).isoformat(), "source_name": "feed", "source_event_id": "3", **metrics},
        ]
        with patch.object(
            floatiq,
            "require_authenticated_tier",
            return_value=("user-1", floatiq.tier_entitlements("autonomous_bot")),
        ), patch.object(floatiq, "SUPERNOVA_FEED_ENABLED", True), patch.object(
            floatiq, "BETA_ALLOWLIST_USER_IDS", {"user-1"}
        ), patch.object(
            floatiq, "supabase", FakeSupabase(rows)
        ):
            result = floatiq.get_supernova_radar(limit=25)
        self.assertEqual([row["ticker"] for row in result["candidates"]], ["FRESH", "WATCH"])
        self.assertTrue(result["candidates"][0]["matches_active_alert_filters"])
        self.assertFalse(result["candidates"][1]["matches_active_alert_filters"])
        self.assertTrue(result["custom_thresholds_available"])

    def test_elite_supernova_alert_filters_are_validated(self):
        settings = floatiq.normalize_supernova_alert_settings({
            "minimum_score": 90,
            "directions": ["UP", "up"],
            "channels": ["push", "in_app"],
        })
        self.assertEqual(settings["directions"], ["up"])
        self.assertEqual(settings["minimum_score"], 90)
        with self.assertRaises(HTTPException):
            floatiq.normalize_supernova_alert_settings({"directions": ["sideways"]})

    def test_compounding_challenge_uses_correct_math_and_warning(self):
        response = client.get("/api/tools/compounding-scenario")
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["final_balance"], 146_974.94)
        self.assertTrue(result["paper_challenge_only"])
        self.assertIn("not a forecast", result["assumption_warning"])

    def test_analog_engine_ranks_explainable_features(self):
        reference = {
            "ticker": "TSLA",
            "features": {
                "total_return_pct": 90,
                "annualized_volatility_pct": 65,
                "max_drawdown_pct": 30,
                "volume_growth_pct": 120,
                "trend_strength": 0.8,
                "relative_strength_pct": 45,
                "revenue_growth_pct": 55,
                "market_cap": 50_000_000_000,
            },
        }
        close_match = {
            "ticker": "TESTA",
            "company_name": "Close Match",
            "features": {
                "total_return_pct": 85,
                "annualized_volatility_pct": 60,
                "max_drawdown_pct": 32,
                "volume_growth_pct": 110,
                "trend_strength": 0.75,
                "relative_strength_pct": 42,
                "revenue_growth_pct": 50,
                "market_cap": 45_000_000_000,
            },
        }
        weak_match = {
            "ticker": "TESTB",
            "company_name": "Weak Match",
            "features": {
                "total_return_pct": -20,
                "annualized_volatility_pct": 15,
                "max_drawdown_pct": 70,
                "volume_growth_pct": -30,
                "trend_strength": -0.5,
                "relative_strength_pct": -25,
                "revenue_growth_pct": 2,
                "market_cap": 500_000_000,
            },
        }
        close_result = floatiq.compare_analog_profiles(reference, close_match)
        weak_result = floatiq.compare_analog_profiles(reference, weak_match)

        self.assertGreater(close_result["similarity_score"], weak_result["similarity_score"])
        self.assertGreater(len(close_result["similarities"]), 0)
        self.assertGreater(len(weak_result["differences"]), 0)

    def test_analog_query_extracts_explicit_reference_ticker(self):
        self.assertEqual(
            floatiq.extract_reference_ticker("find a stock that mirrors TSLA before it blew up"),
            "TSLA",
        )

    def test_experimental_analog_endpoint_is_hidden_by_default(self):
        response = client.post(
            "/api/research/analog-search",
            json={"query": "find stocks like TSLA before it blew up"},
        )
        self.assertEqual(response.status_code, 404)

    def test_market_data_cache_avoids_duplicate_downloads(self):
        prices = synthetic_prices()
        floatiq._MARKET_DATA_CACHE.clear()
        with patch.object(floatiq.yf, "download", return_value=prices.copy()) as download:
            first = floatiq.download_market_data("PLTR", "1y", "1d")
            second = floatiq.download_market_data("PLTR", "1y", "1d")

        self.assertEqual(download.call_count, 1)
        self.assertTrue(first.equals(second))

    def test_position_input_validation(self):
        result = floatiq.calculate_position("PLTR", 0, 1, 25, 2)
        self.assertIn("must be positive", result["error"])

    def test_position_size_is_capped_by_account_value(self):
        with patch.object(floatiq.yf, "Ticker", return_value=FakeTicker()):
            result = floatiq.calculate_position("PLTR", 1_000, 100, 25, 1.5)
        self.assertEqual(result["shares_to_allocate"], 40)
        self.assertIn("capped", result["advisory_warning_badge"])
        self.assertFalse(result["investment_recommendation"])
        self.assertEqual(result["calculation_basis"], "user_supplied_values")

    def test_discipline_coach_uses_user_rules_and_allows_warning(self):
        order = floatiq.DisciplineOrderInput(
            ticker="PLTR",
            account_value=10_000,
            quantity=100,
            entry_price=100,
            stop_loss_price=95,
            take_profit_price=105,
            setup_confirmed=False,
            relative_volume=1.0,
            minutes_since_last_loss=5,
        )
        settings = floatiq.DisciplineSettingsInput(
            mode="coach",
            max_risk_per_trade_pct=1,
            max_position_value_pct=20,
            minimum_reward_to_risk=2,
            required_relative_volume=2,
            cooldown_after_loss_minutes=15,
        ).model_dump()
        result = floatiq.evaluate_discipline_order(order, settings)

        self.assertTrue(result["discipline_gate_passed"])
        self.assertGreater(len(result["violations"]), 0)
        self.assertEqual(result["product_role"], "evaluation_of_user_defined_rules")
        self.assertFalse(result["investment_recommendation"])
        self.assertFalse(result["broker_order_submitted"])

    def test_strict_mode_requires_acknowledgement(self):
        base = {
            "ticker": "PLTR",
            "account_value": 10_000,
            "quantity": 100,
            "entry_price": 100,
            "stop_loss_price": 95,
            "take_profit_price": 115,
            "setup_confirmed": True,
            "minutes_since_last_loss": 60,
        }
        settings = floatiq.DisciplineSettingsInput(
            mode="strict",
            max_risk_per_trade_pct=1,
            max_position_value_pct=20,
            require_setup_confirmation=False,
            cooldown_after_loss_minutes=0,
        ).model_dump()
        blocked = floatiq.evaluate_discipline_order(
            floatiq.DisciplineOrderInput(**base), settings
        )
        acknowledged = floatiq.evaluate_discipline_order(
            floatiq.DisciplineOrderInput(**base, acknowledge_override=True), settings
        )

        self.assertFalse(blocked["discipline_gate_passed"])
        self.assertTrue(blocked["requires_acknowledgement"])
        self.assertTrue(acknowledged["discipline_gate_passed"])

    def test_locked_mode_cannot_be_overridden(self):
        order = floatiq.DisciplineOrderInput(
            ticker="PLTR",
            account_value=10_000,
            quantity=100,
            entry_price=100,
            stop_loss_price=95,
            take_profit_price=115,
            setup_confirmed=True,
            minutes_since_last_loss=60,
            acknowledge_override=True,
        )
        settings = floatiq.DisciplineSettingsInput(
            mode="locked",
            max_risk_per_trade_pct=1,
            max_position_value_pct=20,
            require_setup_confirmation=False,
            cooldown_after_loss_minutes=0,
        ).model_dump()
        result = floatiq.evaluate_discipline_order(order, settings)

        self.assertFalse(result["discipline_gate_passed"])
        self.assertFalse(result["override_available"])

    def test_locked_mode_fails_closed_when_required_data_is_missing(self):
        order = floatiq.DisciplineOrderInput(
            ticker="PLTR",
            account_value=10_000,
            quantity=10,
            entry_price=100,
            stop_loss_price=99,
            take_profit_price=103,
            setup_confirmed=True,
        )
        settings = floatiq.DisciplineSettingsInput(
            mode="locked",
            required_relative_volume=2,
            cooldown_after_loss_minutes=15,
        ).model_dump()
        result = floatiq.evaluate_discipline_order(order, settings)
        self.assertFalse(result["discipline_gate_passed"])
        self.assertEqual(len(result["unable_to_evaluate"]), 2)

    def test_trade_review_separates_process_from_profit(self):
        disciplined_loss = floatiq.review_trade_process(floatiq.TradeProcessReviewInput(
            ticker="PLTR",
            pnl_percentage=-1,
            followed_entry_rule=True,
            used_planned_position_size=True,
            stop_present_before_entry=True,
            followed_stop_rule=True,
            followed_exit_rule=True,
            setup_confirmed_at_entry=True,
        ))
        undisciplined_win = floatiq.review_trade_process(floatiq.TradeProcessReviewInput(
            ticker="PLTR",
            pnl_percentage=3,
            followed_entry_rule=False,
            used_planned_position_size=False,
            stop_present_before_entry=False,
            followed_stop_rule=False,
            followed_exit_rule=False,
            setup_confirmed_at_entry=False,
        ))

        self.assertEqual(disciplined_loss["process_classification"], "disciplined_loss")
        self.assertEqual(undisciplined_win["process_classification"], "undisciplined_win")
        self.assertGreater(
            disciplined_loss["discipline_score"], undisciplined_win["discipline_score"]
        )

    def test_discipline_summary_finds_streak_and_recurring_conflicts(self):
        summary = floatiq.summarize_trade_reviews([
            {
                "discipline_score": 100,
                "process_classification": "disciplined_win",
                "findings": [],
            },
            {
                "discipline_score": 83.3,
                "process_classification": "disciplined_loss",
                "findings": [{"rule_key": "entry_rule"}],
            },
            {
                "discipline_score": 50,
                "process_classification": "undisciplined_win",
                "findings": [
                    {"rule_key": "entry_rule"},
                    {"rule_key": "position_size"},
                ],
            },
        ])

        self.assertEqual(summary["current_disciplined_streak"], 2)
        self.assertEqual(summary["recurring_rule_conflicts"][0]["rule_key"], "entry_rule")
        self.assertTrue(summary["profit_is_not_the_discipline_score"])

    def test_discipline_settings_require_authentication(self):
        response = client.get("/api/discipline/settings")
        self.assertEqual(response.status_code, 401)

    def test_order_workflow_honors_locked_discipline_evaluation(self):
        blocked_evaluation = [{
            "id": "evaluation-1",
            "discipline_gate_passed": False,
            "violations": [{"rule_key": "max_risk_per_trade_pct"}],
        }]
        with patch.object(floatiq, "supabase", FakeSupabase(blocked_evaluation)), patch.object(
            floatiq, "get_authenticated_user_id", return_value="user-1"
        ), patch.object(
            floatiq,
            "check_user_tier_permissions",
            return_value={"tier": "autonomous_bot", "is_authorized": True},
        ):
            result = floatiq.execute_autonomous_token(
                floatiq.OrderGateInput(
                    verification_code="received-not-validated",
                    discipline_evaluation_id="evaluation-1",
                ),
                authorization="Bearer test",
            )

        self.assertEqual(result["status"], "BLOCKED_BY_USER_RULES")
        self.assertFalse(result["broker_order_submitted"])

    def test_order_verification_code_is_in_request_body_schema(self):
        operation = floatiq.app.openapi()["paths"]["/api/execute-autonomous-token"]["post"]
        self.assertIn("requestBody", operation)
        query_names = {
            item["name"] for item in operation.get("parameters", []) if item["in"] == "query"
        }
        self.assertNotIn("verification_code", query_names)

    def test_invalid_ticker_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            floatiq.get_probabilities("not a ticker", "1y", "1d", "guest_user")
        self.assertEqual(raised.exception.status_code, 422)

    def test_private_endpoint_requires_authentication(self):
        with self.assertRaises(HTTPException) as raised:
            floatiq.get_user_journal_summary("test-user")
        self.assertEqual(raised.exception.status_code, 401)

    def test_user_id_query_does_not_unlock_paid_tier(self):
        prices = synthetic_prices()
        floatiq._MARKET_DATA_CACHE.clear()
        with patch.object(floatiq.yf, "download", return_value=prices.copy()), patch.object(
            floatiq.yf, "Ticker", return_value=FakeTicker()
        ):
            result = floatiq.get_probabilities(
                "PLTR",
                "1y",
                "1d",
                user_id="someone-elses-paid-id",
                authorization=None,
            )
        self.assertEqual(result["max_allowed_charts"], 2)


if __name__ == "__main__":
    unittest.main()
        self.assertFalse(result["custom_thresholds_available"])

    def test_supernova_radar_drops_stale_cache_rows(self):
        now = datetime.now(timezone.utc)
        metrics = {
            "relative_volume_at_time": 5,
            "price_change_pct": 4,
            "average_dollar_volume": 5_000_000,
            "spread_bps": 20,
            "direction": "up",
        }
        rows = [
            {"ticker": "FRESH", "score": 80, "updated_at": now.isoformat(), **metrics},
            {"ticker": "WATCH", "score": 45, "updated_at": now.isoformat(), **metrics},
            {"ticker": "STALE", "score": 99, "updated_at": (now - timedelta(hours=1)).isoformat(), **metrics},
        ]
        with patch.object(
            floatiq,
            "require_authenticated_tier",
            return_value=("user-1", floatiq.tier_entitlements("autonomous_bot")),
        ), patch.object(floatiq, "SUPERNOVA_FEED_ENABLED", True), patch.object(
            floatiq, "supabase", FakeSupabase(rows)
        ):
            result = floatiq.get_supernova_radar(limit=25)
        self.assertEqual([row["ticker"] for row in result["candidates"]], ["FRESH", "WATCH"])
        self.assertTrue(result["candidates"][0]["matches_active_alert_filters"])
        self.assertFalse(result["candidates"][1]["matches_active_alert_filters"])
        self.assertTrue(result["custom_thresholds_available"])

    def test_elite_supernova_alert_filters_are_validated(self):
        settings = floatiq.normalize_supernova_alert_settings({
            "minimum_score": 90,
            "directions": ["UP", "up"],
            "channels": ["push", "in_app"],
        })
        self.assertEqual(settings["directions"], ["up"])
        self.assertEqual(settings["minimum_score"], 90)
        with self.assertRaises(HTTPException):
            floatiq.normalize_supernova_alert_settings({"directions": ["sideways"]})

    def test_compounding_challenge_uses_correct_math_and_warning(self):
        response = client.get("/api/tools/compounding-scenario")
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["final_balance"], 146_974.94)
        self.assertTrue(result["paper_challenge_only"])
        self.assertIn("not a forecast", result["assumption_warning"])

    def test_analog_engine_ranks_explainable_features(self):
        reference = {
            "ticker": "TSLA",
            "features": {
                "total_return_pct": 90,
                "annualized_volatility_pct": 65,
                "max_drawdown_pct": 30,
                "volume_growth_pct": 120,
                "trend_strength": 0.8,
                "relative_strength_pct": 45,
                "revenue_growth_pct": 55,
                "market_cap": 50_000_000_000,
            },
        }
        close_match = {
            "ticker": "TESTA",
            "company_name": "Close Match",
            "features": {
                "total_return_pct": 85,
                "annualized_volatility_pct": 60,
                "max_drawdown_pct": 32,
                "volume_growth_pct": 110,
                "trend_strength": 0.75,
                "relative_strength_pct": 42,
                "revenue_growth_pct": 50,
                "market_cap": 45_000_000_000,
            },
        }
        weak_match = {
            "ticker": "TESTB",
            "company_name": "Weak Match",
            "features": {
                "total_return_pct": -20,
                "annualized_volatility_pct": 15,
                "max_drawdown_pct": 70,
                "volume_growth_pct": -30,
                "trend_strength": -0.5,
                "relative_strength_pct": -25,
                "revenue_growth_pct": 2,
                "market_cap": 500_000_000,
            },
        }
        close_result = floatiq.compare_analog_profiles(reference, close_match)
        weak_result = floatiq.compare_analog_profiles(reference, weak_match)

        self.assertGreater(close_result["similarity_score"], weak_result["similarity_score"])
        self.assertGreater(len(close_result["similarities"]), 0)
        self.assertGreater(len(weak_result["differences"]), 0)

    def test_analog_query_extracts_explicit_reference_ticker(self):
        self.assertEqual(
            floatiq.extract_reference_ticker("find a stock that mirrors TSLA before it blew up"),
            "TSLA",
        )

    def test_experimental_analog_endpoint_is_hidden_by_default(self):
        response = client.post(
            "/api/research/analog-search",
            json={"query": "find stocks like TSLA before it blew up"},
        )
        self.assertEqual(response.status_code, 404)

    def test_market_data_cache_avoids_duplicate_downloads(self):
        prices = synthetic_prices()
        floatiq._MARKET_DATA_CACHE.clear()
        with patch.object(floatiq.yf, "download", return_value=prices.copy()) as download:
            first = floatiq.download_market_data("PLTR", "1y", "1d")
            second = floatiq.download_market_data("PLTR", "1y", "1d")

        self.assertEqual(download.call_count, 1)
        self.assertTrue(first.equals(second))

    def test_position_input_validation(self):
        result = floatiq.calculate_position("PLTR", 0, 1, 25, 2)
        self.assertIn("must be positive", result["error"])

    def test_position_size_is_capped_by_account_value(self):
        with patch.object(floatiq.yf, "Ticker", return_value=FakeTicker()):
            result = floatiq.calculate_position("PLTR", 1_000, 100, 25, 1.5)
        self.assertEqual(result["shares_to_allocate"], 40)
        self.assertIn("capped", result["advisory_warning_badge"])
        self.assertFalse(result["investment_recommendation"])
        self.assertEqual(result["calculation_basis"], "user_supplied_values")

    def test_discipline_coach_uses_user_rules_and_allows_warning(self):
        order = floatiq.DisciplineOrderInput(
            ticker="PLTR",
            account_value=10_000,
            quantity=100,
            entry_price=100,
            stop_loss_price=95,
            take_profit_price=105,
            setup_confirmed=False,
            relative_volume=1.0,
            minutes_since_last_loss=5,
        )
        settings = floatiq.DisciplineSettingsInput(
            mode="coach",
            max_risk_per_trade_pct=1,
            max_position_value_pct=20,
            minimum_reward_to_risk=2,
            required_relative_volume=2,
            cooldown_after_loss_minutes=15,
        ).model_dump()
        result = floatiq.evaluate_discipline_order(order, settings)

        self.assertTrue(result["discipline_gate_passed"])
        self.assertGreater(len(result["violations"]), 0)
        self.assertEqual(result["product_role"], "evaluation_of_user_defined_rules")
        self.assertFalse(result["investment_recommendation"])
        self.assertFalse(result["broker_order_submitted"])

    def test_strict_mode_requires_acknowledgement(self):
        base = {
            "ticker": "PLTR",
            "account_value": 10_000,
            "quantity": 100,
            "entry_price": 100,
            "stop_loss_price": 95,
            "take_profit_price": 115,
            "setup_confirmed": True,
            "minutes_since_last_loss": 60,
        }
        settings = floatiq.DisciplineSettingsInput(
            mode="strict",
            max_risk_per_trade_pct=1,
            max_position_value_pct=20,
            require_setup_confirmation=False,
            cooldown_after_loss_minutes=0,
        ).model_dump()
        blocked = floatiq.evaluate_discipline_order(
            floatiq.DisciplineOrderInput(**base), settings
        )
        acknowledged = floatiq.evaluate_discipline_order(
            floatiq.DisciplineOrderInput(**base, acknowledge_override=True), settings
        )

        self.assertFalse(blocked["discipline_gate_passed"])
        self.assertTrue(blocked["requires_acknowledgement"])
        self.assertTrue(acknowledged["discipline_gate_passed"])

    def test_locked_mode_cannot_be_overridden(self):
        order = floatiq.DisciplineOrderInput(
            ticker="PLTR",
            account_value=10_000,
            quantity=100,
            entry_price=100,
            stop_loss_price=95,
            take_profit_price=115,
            setup_confirmed=True,
            minutes_since_last_loss=60,
            acknowledge_override=True,
        )
        settings = floatiq.DisciplineSettingsInput(
            mode="locked",
            max_risk_per_trade_pct=1,
            max_position_value_pct=20,
            require_setup_confirmation=False,
            cooldown_after_loss_minutes=0,
        ).model_dump()
        result = floatiq.evaluate_discipline_order(order, settings)

        self.assertFalse(result["discipline_gate_passed"])
        self.assertFalse(result["override_available"])

    def test_locked_mode_fails_closed_when_required_data_is_missing(self):
        order = floatiq.DisciplineOrderInput(
            ticker="PLTR",
            account_value=10_000,
            quantity=10,
            entry_price=100,
            stop_loss_price=99,
            take_profit_price=103,
            setup_confirmed=True,
        )
        settings = floatiq.DisciplineSettingsInput(
            mode="locked",
            required_relative_volume=2,
            cooldown_after_loss_minutes=15,
        ).model_dump()
        result = floatiq.evaluate_discipline_order(order, settings)
        self.assertFalse(result["discipline_gate_passed"])
        self.assertEqual(len(result["unable_to_evaluate"]), 2)

    def test_trade_review_separates_process_from_profit(self):
        disciplined_loss = floatiq.review_trade_process(floatiq.TradeProcessReviewInput(
            ticker="PLTR",
            pnl_percentage=-1,
            followed_entry_rule=True,
            used_planned_position_size=True,
            stop_present_before_entry=True,
            followed_stop_rule=True,
            followed_exit_rule=True,
            setup_confirmed_at_entry=True,
        ))
        undisciplined_win = floatiq.review_trade_process(floatiq.TradeProcessReviewInput(
            ticker="PLTR",
            pnl_percentage=3,
            followed_entry_rule=False,
            used_planned_position_size=False,
            stop_present_before_entry=False,
            followed_stop_rule=False,
            followed_exit_rule=False,
            setup_confirmed_at_entry=False,
        ))

        self.assertEqual(disciplined_loss["process_classification"], "disciplined_loss")
        self.assertEqual(undisciplined_win["process_classification"], "undisciplined_win")
        self.assertGreater(
            disciplined_loss["discipline_score"], undisciplined_win["discipline_score"]
        )

    def test_discipline_summary_finds_streak_and_recurring_conflicts(self):
        summary = floatiq.summarize_trade_reviews([
            {
                "discipline_score": 100,
                "process_classification": "disciplined_win",
                "findings": [],
            },
            {
                "discipline_score": 83.3,
                "process_classification": "disciplined_loss",
                "findings": [{"rule_key": "entry_rule"}],
            },
            {
                "discipline_score": 50,
                "process_classification": "undisciplined_win",
                "findings": [
                    {"rule_key": "entry_rule"},
                    {"rule_key": "position_size"},
                ],
            },
        ])

        self.assertEqual(summary["current_disciplined_streak"], 2)
        self.assertEqual(summary["recurring_rule_conflicts"][0]["rule_key"], "entry_rule")
        self.assertTrue(summary["profit_is_not_the_discipline_score"])

    def test_discipline_settings_require_authentication(self):
        response = client.get("/api/discipline/settings")
        self.assertEqual(response.status_code, 401)

    def test_order_workflow_honors_locked_discipline_evaluation(self):
        blocked_evaluation = [{
            "id": "evaluation-1",
            "discipline_gate_passed": False,
            "violations": [{"rule_key": "max_risk_per_trade_pct"}],
        }]
        with patch.object(floatiq, "supabase", FakeSupabase(blocked_evaluation)), patch.object(
            floatiq, "get_authenticated_user_id", return_value="user-1"
        ), patch.object(
            floatiq,
            "check_user_tier_permissions",
            return_value={"tier": "autonomous_bot", "is_authorized": True},
        ):
            result = floatiq.execute_autonomous_token(
                floatiq.OrderGateInput(
                    verification_code="received-not-validated",
                    discipline_evaluation_id="evaluation-1",
                ),
                authorization="Bearer test",
            )

        self.assertEqual(result["status"], "BLOCKED_BY_USER_RULES")
        self.assertFalse(result["broker_order_submitted"])

    def test_order_verification_code_is_in_request_body_schema(self):
        operation = floatiq.app.openapi()["paths"]["/api/execute-autonomous-token"]["post"]
        self.assertIn("requestBody", operation)
        query_names = {
            item["name"] for item in operation.get("parameters", []) if item["in"] == "query"
        }
        self.assertNotIn("verification_code", query_names)

    def test_invalid_ticker_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            floatiq.get_probabilities("not a ticker", "1y", "1d", "guest_user")
        self.assertEqual(raised.exception.status_code, 422)

    def test_private_endpoint_requires_authentication(self):
        with self.assertRaises(HTTPException) as raised:
            floatiq.get_user_journal_summary("test-user")
        self.assertEqual(raised.exception.status_code, 401)

    def test_user_id_query_does_not_unlock_paid_tier(self):
        prices = synthetic_prices()
        floatiq._MARKET_DATA_CACHE.clear()
        with patch.object(floatiq.yf, "download", return_value=prices.copy()), patch.object(
            floatiq.yf, "Ticker", return_value=FakeTicker()
        ):
            result = floatiq.get_probabilities(
                "PLTR",
                "1y",
                "1d",
                user_id="someone-elses-paid-id",
                authorization=None,
            )
        self.assertEqual(result["max_allowed_charts"], 2)


if __name__ == "__main__":
    unittest.main()
