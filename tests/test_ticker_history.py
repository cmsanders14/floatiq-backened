import unittest
from datetime import datetime, timezone
import pandas as pd
from floatiq_core.setup_scanner import SetupScanner, historical_ticker_row


class TickerHistoryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 18, 17, tzinfo=timezone.utc)
        self.sessions = [(pd.Timestamp('2026-10-16T13:30Z'), pd.Timestamp('2026-10-16T20:00Z'))]
        self.frame = pd.DataFrame({'Close': [100., 101., 999.]}, index=pd.to_datetime([
            '2026-10-16T19:30Z', '2026-10-16T19:45Z', '2026-10-16T20:00Z']))

    def test_weekend_returns_dated_history_not_live_signal(self):
        row = historical_ticker_row('AAPL', self.frame, self.now, 'iex', self.sessions)
        self.assertEqual(row['current_price'], 101)
        self.assertEqual(row['observed_at'], '2026-10-16T20:00:00+00:00')
        self.assertFalse(row['is_live_setup'])
        self.assertFalse(row['is_official_closing_price'])
        self.assertIn('2026-10-16 16:00 EDT', row['pattern_detected'])
        self.assertIsNone(row['gap_fill_probability'])

    def test_early_close_excludes_after_hours(self):
        sessions = [(pd.Timestamp('2026-10-16T13:30Z'), pd.Timestamp('2026-10-16T17:00Z'))]
        frame = pd.DataFrame({'Close': [10., 999.]}, index=pd.to_datetime(['2026-10-16T16:45Z', '2026-10-16T17:00Z']))
        row = historical_ticker_row('AAPL', frame, self.now, 'iex', sessions)
        self.assertEqual(row['current_price'], 10)

    def test_unfinished_bar_excluded(self):
        now = datetime(2026, 10, 16, 19, 50, tzinfo=timezone.utc)
        row = historical_ticker_row('AAPL', self.frame, now, 'iex', self.sessions)
        self.assertEqual(row['current_price'], 100)

    def test_holiday_has_no_synthetic_session(self):
        self.assertIsNone(historical_ticker_row('AAPL', self.frame, self.now, 'iex', []))

    def test_ticker_search_uses_separate_cache_and_preserves_setup_freshness(self):
        outer = self
        class Provider:
            feed = 'iex'
            calls = 0
            def download_many_range(self, *args, **kwargs):
                Provider.calls += 1
                return {'AAPL': outer.frame}
            def market_calendar(self, *args):
                return outer.sessions
        scanner = SetupScanner(Provider, ['AAPL'])
        rows, meta = scanner.search('aapl', self.now)
        self.assertEqual(rows[0]['ticker'], 'AAPL')
        self.assertEqual(meta['data_status'], 'historical_ticker')
        scanner.search('AAPL', self.now)
        self.assertEqual(Provider.calls, 1)
        self.assertEqual(scanner.search('bull', self.now)[0], [])


if __name__ == '__main__':
    unittest.main()
