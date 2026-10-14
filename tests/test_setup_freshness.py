"""Offline regression coverage for stale scanner records and setup matching."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from main import module as app


class CacheQuery:
    def __init__(self, rows):
        self.rows = list(rows)

    def table(self, name):
        assert name == "live_scan_cache"
        return self

    def select(self, *_):
        return self

    def ilike(self, key, expression):
        term = expression.strip("%").lower()
        self.rows = [r for r in self.rows if term in r[key].lower()]
        return self

    def gte(self, key, value):
        self.rows = [r for r in self.rows if r.get(key) and r[key] >= value]
        return self

    def lte(self, key, value):
        self.rows = [r for r in self.rows if r.get(key) and r[key] <= value]
        return self

    def order(self, key, desc=False):
        self.rows.sort(key=lambda r: r[key], reverse=desc)
        return self

    def limit(self, count):
        self.rows = self.rows[:count]
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows)


class SetupFreshnessTests(unittest.TestCase):
    def search(self, rows, term="bull"):
        with patch.object(app, "supabase", CacheQuery(rows)), patch.object(
            app, "get_authenticated_user_id", return_value=None
        ), patch.object(app, "SETUP_CACHE_MAX_AGE_SECONDS", 120):
            return app.search_live_setups(term, chart_slot=1, authorization=None)

    def row(self, ticker, seconds=30, pattern="Bullish Engulfing Breakout", score=50):
        return dict(ticker=ticker, pattern_detected=pattern,
                    updated_at=(datetime.now(timezone.utc)-timedelta(seconds=seconds)).isoformat(),
                    gap_fill_probability=score)

    def test_old_high_ranked_rows_do_not_displace_recent_results(self):
        rows = [self.row(t, 40*86400, score=99) for t in ("AAPL", "SMCI", "OLD")]
        rows += [self.row("MSFT")]
        result = self.search(rows)
        self.assertEqual([r["ticker"] for r in result["candidates"]], ["MSFT"])
        self.assertIsNone(result["candidates"][0]["win_rate"])
        self.assertTrue(result["candidates"][0]["win_rate_locked"])
        self.assertEqual(result["max_allowed_charts"], 2)

    def test_stale_missing_and_future_dates_return_no_matches(self):
        missing = self.row("MISSING")
        missing["updated_at"] = None
        result = self.search([self.row("OLD", 121), self.row("FUTURE", -60), missing])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["data_status"], "no_recent_matches")

    def test_setup_filter_still_distinguishes_patterns(self):
        rows = [self.row("BULL"), self.row("BEAR", pattern="Bearish Reversal")]
        self.assertEqual([r["ticker"] for r in self.search(rows, "bearish")["candidates"]], ["BEAR"])

    def test_blank_and_wildcard_queries_are_rejected(self):
        for term in ("   ", "%", "_", "*"):
            with self.subTest(term=term), self.assertRaises(app.HTTPException) as error:
                self.search([], term)
            self.assertEqual(error.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
