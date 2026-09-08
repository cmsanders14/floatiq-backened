import json
import unittest
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
from database_sync import build_scan_cache_payload
from workers.sync_supernova_candidates import build_supernova_cache_payload


class CoreComponentTests(unittest.TestCase):
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
