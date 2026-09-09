import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from floatiq_core.analog_profiles import compute_market_profile
from floatiq_core.brokers import (
    BrokerAdapterRegistry,
    BrokerIntegrationError,
    broker_capability_manifest,
    normalize_broker_provider,
    order_request_fingerprint,
    validate_idempotency_key,
)
from floatiq_core.rate_limit import SlidingWindowRateLimiter
from floatiq_core.market_sessions import (
    assess_market_availability,
    infer_market_session,
    rank_session_candidate,
)
from floatiq_core.launch_safety import (
    assess_feed_batch,
    beta_access_status,
    classify_market_regime,
    statistical_claim_status,
)
from database_sync import build_scan_cache_payload
from workers.sync_session_opportunities import build_session_cache_payload
from workers.sync_supernova_candidates import build_supernova_cache_payload


class CoreComponentTests(unittest.TestCase):
    def test_market_regime_is_objective_and_requires_complete_inputs(self):
        bullish = classify_market_regime(
            benchmark_change_pct=1.2,
            benchmark_above_20d_ma=True,
            market_breadth_pct=72,
            volatility_percentile=45,
            liquidity_status="normal",
        )
        incomplete = classify_market_regime(
            benchmark_change_pct=1.2,
            benchmark_above_20d_ma=None,
            market_breadth_pct=72,
            volatility_percentile=45,
            liquidity_status="normal",
        )
        self.assertEqual(bullish["market_regime"], "bullish_trend")
        self.assertEqual(incomplete["market_regime"], "insufficient_data")
        self.assertFalse(bullish["regime_prediction_claimed"])

    def test_high_volatility_overrides_directional_regime(self):
        result = classify_market_regime(
            benchmark_change_pct=2,
            benchmark_above_20d_ma=True,
            market_breadth_pct=80,
            volatility_percentile=95,
            liquidity_status="stressed",
        )
        self.assertEqual(result["market_regime"], "high_volatility")

    def test_statistical_claims_are_hidden_below_declared_sample_floor(self):
        small = statistical_claim_status(successes=18, total=20, minimum_sample_size=30)
        ready = statistical_claim_status(successes=27, total=30, minimum_sample_size=30)
        self.assertFalse(small["win_rate_display_allowed"])
        self.assertIsNone(small["observed_win_rate_pct"])
        self.assertTrue(ready["win_rate_display_allowed"])
        self.assertEqual(ready["observed_win_rate_pct"], 90)

    def test_feed_circuit_breaker_halts_bad_batches(self):
        now = datetime(2026, 9, 12, 14, tzinfo=timezone.utc)
        healthy_row = {
            "source_name": "feed",
            "source_event_id": "1",
            "ticker": "BTC-USD",
            "price": 60_000,
            "observed_at": (now - timedelta(seconds=10)).isoformat(),
        }
        healthy = assess_feed_batch([healthy_row], now=now, max_age_seconds=120)
        bad = assess_feed_batch(
            [healthy_row, {**healthy_row, "observed_at": (now + timedelta(minutes=5)).isoformat()}],
            now=now,
            max_age_seconds=120,
        )
        self.assertFalse(healthy["publication_halted"])
        self.assertTrue(bad["publication_halted"])
        self.assertIn("duplicate_source_events", bad["reason_codes"])
        self.assertIn("future_timestamps", bad["reason_codes"])
        invalid = assess_feed_batch(
            [{**healthy_row, "source_event_id": None, "price": 0}],
            now=now,
            max_age_seconds=120,
        )
        self.assertTrue(invalid["publication_halted"])
        self.assertEqual(invalid["invalid_count"], 1)

    def test_beta_access_is_allowlisted_until_public_launch(self):
        denied = beta_access_status(
            user_id="user-2",
            public_launch_enabled=False,
            beta_mode_enabled=True,
            allowlist={"user-1"},
        )
        public = beta_access_status(
            user_id="any-user",
            public_launch_enabled=True,
            beta_mode_enabled=True,
            allowlist=set(),
        )
        self.assertFalse(denied["access_allowed"])
        self.assertTrue(public["access_allowed"])

    def test_market_sessions_separate_weekend_charting_from_tradability(self):
        saturday = datetime(2026, 9, 12, 14, tzinfo=timezone.utc)
        equity = infer_market_session(saturday, "equity")
        crypto = infer_market_session(saturday, "crypto")

        self.assertEqual(equity["market_session"], "closed")
        self.assertTrue(equity["schedule_estimate"])
        self.assertEqual(crypto["market_session"], "continuous")
        self.assertTrue(crypto["is_weekend"])

        availability = assess_market_availability(
            observed_at=saturday - timedelta(seconds=30),
            now=saturday,
            asset_type="crypto",
            reported_session="continuous",
            provider_supports_session=True,
            broker_supports_session=None,
        )
        self.assertTrue(availability["chartable"])
        self.assertEqual(availability["data_status"], "fresh")
        self.assertEqual(availability["tradability_status"], "broker_support_unknown")
        self.assertFalse(availability["potentially_tradable"])

    def test_session_availability_requires_fresh_provider_and_broker_support(self):
        now = datetime(2026, 9, 12, 14, tzinfo=timezone.utc)
        fresh = assess_market_availability(
            observed_at=now - timedelta(seconds=20),
            now=now,
            asset_type="crypto",
            reported_session="continuous",
            provider_supports_session=True,
            broker_supports_session=True,
        )
        stale = assess_market_availability(
            observed_at=now - timedelta(hours=1),
            now=now,
            asset_type="crypto",
            reported_session="continuous",
            provider_supports_session=True,
            broker_supports_session=True,
        )
        self.assertTrue(fresh["potentially_tradable"])
        self.assertFalse(stale["potentially_tradable"])
        self.assertEqual(stale["tradability_status"], "not_confirmed_stale_data")

    def test_session_availability_rejects_future_provider_timestamp(self):
        now = datetime(2026, 9, 12, 14, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, "more than 30 seconds in the future"):
            assess_market_availability(
                observed_at=now + timedelta(minutes=5),
                now=now,
                asset_type="crypto",
                reported_session="continuous",
                provider_supports_session=True,
                broker_supports_session=True,
            )

    def test_session_candidate_ranking_is_bounded_and_explainable(self):
        strong = rank_session_candidate(
            relative_volume=6,
            average_dollar_volume=20_000_000,
            spread_bps=10,
            price_change_pct=6,
            range_breakout_pct=4,
            vwap_distance_pct=3,
        )
        weak = rank_session_candidate(
            relative_volume=0.5,
            average_dollar_volume=100_000,
            spread_bps=140,
            price_change_pct=0.2,
            range_breakout_pct=0.1,
            vwap_distance_pct=0.1,
        )
        self.assertGreater(strong["ranking_score"], weak["ranking_score"])
        self.assertLessEqual(strong["ranking_score"], 100)
        self.assertTrue(strong["reason_codes"])
        self.assertFalse(strong["prediction_claimed"])

    def test_session_worker_rejects_broker_claims_and_stale_rows(self):
        now = datetime(2026, 9, 12, 14, tzinfo=timezone.utc)
        record = {
            "source_event_id": "weekend-1",
            "ticker": "BTC-USD",
            "asset_type": "crypto",
            "price": 60_000,
            "relative_volume": 2.5,
            "average_dollar_volume": 1_000_000_000,
            "spread_bps": 5,
            "price_change_pct": 2,
            "range_breakout_pct": 1,
            "vwap_distance_pct": 1,
            "observed_at": now - timedelta(seconds=10),
            "market_session": "continuous",
            "provider_supports_session": True,
        }
        payload = build_session_cache_payload(record, "licensed_feed", now=now)
        self.assertEqual(payload["market_session"], "continuous")
        self.assertEqual(payload["ticker"], "BTC-USD")
        self.assertNotIn("broker_supports_session", payload)
        self.assertEqual(payload["market_regime"], "insufficient_data")
        self.assertEqual(payload["algorithm_version"], "session_opportunity_v1")

        stale = {**record, "source_event_id": "weekend-2", "observed_at": now - timedelta(hours=1)}
        self.assertIsNone(build_session_cache_payload(stale, "licensed_feed", now=now))
        with self.assertRaises(ValueError):
            build_session_cache_payload(
                {**record, "broker_supports_session": True}, "licensed_feed", now=now
            )

    def test_broker_manifest_cannot_enable_live_orders_with_environment_flag(self):
        manifest = broker_capability_manifest(
            connections_enabled=True,
            paper_enabled=True,
            live_enabled=True,
            global_kill_switch=False,
        )
        self.assertTrue(manifest["paper_trading_enabled"])
        self.assertFalse(manifest["live_order_submission_enabled"])
        self.assertTrue(manifest["requested_live_flag_ignored"])

    def test_broker_provider_and_idempotency_validation(self):
        self.assertEqual(normalize_broker_provider("thinkorswim"), "schwab")
        self.assertEqual(validate_idempotency_key("order:2026-09-08:abc123"), "order:2026-09-08:abc123")
        with self.assertRaises(BrokerIntegrationError):
            normalize_broker_provider("unapproved-broker")
        with self.assertRaises(BrokerIntegrationError):
            validate_idempotency_key("short")

    def test_order_fingerprint_is_stable_and_detects_changes(self):
        first = order_request_fingerprint("schwab", {"ticker": "PLTR", "quantity": 10})
        same = order_request_fingerprint("thinkorswim", {"quantity": 10, "ticker": "PLTR"})
        changed = order_request_fingerprint("schwab", {"ticker": "PLTR", "quantity": 11})
        self.assertEqual(first, same)
        self.assertNotEqual(first, changed)

    def test_unregistered_broker_adapter_fails_closed(self):
        registry = BrokerAdapterRegistry()
        with self.assertRaises(BrokerIntegrationError):
            registry.get("schwab")

    def test_rate_limiter_rejects_request_over_window_limit(self):
        limiter = SlidingWindowRateLimiter()
        self.assertTrue(limiter.check("market:user", 2)[0])
        self.assertTrue(limiter.check("market:user", 2)[0])
        allowed, retry_after = limiter.check("market:user", 2)
        self.assertFalse(allowed)
        self.assertGreaterEqual(retry_after, 1)

    def test_rate_limiter_bounds_unique_client_keys(self):
        limiter = SlidingWindowRateLimiter(max_keys=1)
        self.assertTrue(limiter.check("market:first", 2)[0])
        self.assertFalse(limiter.check("market:second", 2)[0])

    def test_scanner_cache_payload_requires_real_validated_metrics(self):
        payload = build_scan_cache_payload({
            "status": "success",
            "ticker": "pltr",
            "pattern": "bull flag",
            "live_price": 120.5,
            "relative_volume_rvol": 2.8,
            "gap_fill_prob": 74,
        })
        self.assertEqual(payload["ticker"], "PLTR")
        self.assertEqual(payload["gap_fill_probability"], 74.0)
        with self.assertRaises(ValueError):
            build_scan_cache_payload({"status": "success", "ticker": "PLTR"})
        with self.assertRaises(ValueError):
            build_scan_cache_payload({
                "status": "success",
                "ticker": "PLTR",
                "pattern": "bull flag",
                "live_price": float("nan"),
                "relative_volume_rvol": 2,
                "gap_fill_prob": 70,
            })

    def test_supernova_worker_only_publishes_qualified_rows(self):
        base = {
            "source_event_id": "event-1",
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
            "market_session": "regular",
        }
        payload = build_supernova_cache_payload(base, "licensed_feed")
        self.assertEqual(payload["ticker"], "PLTR")
        self.assertEqual(payload["stage"], "TRIGGERED")
        normal = {**base, "source_event_id": "event-2", "relative_volume_at_time": 0.5,
                  "volume_acceleration": 0.5, "price_change_pct": 0.2,
                  "range_breakout_pct": 0.1, "vwap_distance_pct": -1}
        self.assertIsNone(build_supernova_cache_payload(normal, "licensed_feed"))

    def test_analog_profile_calculates_normalized_features(self):
        index = pd.date_range("2025-01-01", periods=80, freq="B")
        prices = pd.DataFrame({
            "Close": np.linspace(20, 35, len(index)),
            "Volume": np.linspace(1_000_000, 2_000_000, len(index)),
        }, index=index)
        benchmark = pd.DataFrame({
            "Close": np.linspace(100, 110, len(index)),
            "Volume": np.full(len(index), 10_000_000),
        }, index=index)
        profile = compute_market_profile(prices, benchmark, {"market_cap": 1_000_000_000})

        self.assertEqual(profile["observation_count"], 80)
        self.assertGreater(profile["total_return_pct"], 0)
        self.assertGreater(profile["relative_strength_pct"], 0)
        self.assertEqual(profile["market_cap"], 1_000_000_000)

    def test_demo_fixture_is_valid_and_explicitly_non_live(self):
        path = Path(__file__).parents[1] / "demo" / "demo_fixtures.json"
        fixture = json.loads(path.read_text())
        self.assertIn("DEMO DATA ONLY", fixture["warning"])
        self.assertTrue(all(event["ticker"] == "DEMO" for event in fixture["market_alerts"]))
        self.assertFalse(fixture["discipline_evaluation"]["broker_order_submitted"])


if __name__ == "__main__":
    unittest.main()
