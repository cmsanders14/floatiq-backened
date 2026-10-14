import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import pandas as pd
from floatiq_core.setup_scanner import SetupScanner, detect_setups
from floatiq_core.alpaca_data import AlpacaDataError

NOW = datetime(2026, 10, 14, 18, 45, tzinfo=timezone.utc)


def bars():
    index = pd.date_range('2026-10-14T13:30:00Z', periods=21, freq='15min')
    frame = pd.DataFrame({'Open': 10.5, 'High': 12., 'Low': 9., 'Close': 10., 'Volume': 100.}, index=index)
    frame.iloc[-1] = [9.9, 13., 9., 12.5, 200.]
    return frame


class ScannerTests(unittest.TestCase):
    def test_actual_engulfing_and_breakout(self):
        row = detect_setups('AAPL', bars(), NOW, 'iex')
        self.assertEqual(row['pattern_detected'], 'Bullish Engulfing; Bullish Breakout')
        self.assertEqual(row['current_rvol'], 2)
        self.assertIsNone(row['gap_fill_probability'])
        self.assertEqual(row['observed_at'], NOW.isoformat())

    def test_bearish_is_not_bullish(self):
        frame = bars()
        frame.iloc[-2] = [10., 12., 9., 11., 100.]
        frame.iloc[-1] = [11.5, 12., 8., 8.5, 200.]
        self.assertEqual(detect_setups('TSLA', frame, NOW, 'iex')['pattern_detected'], 'Bearish Engulfing; Bearish Breakdown')

    def test_stale_and_unfinished_excluded(self):
        self.assertIsNone(detect_setups('AAPL', bars(), NOW+timedelta(minutes=21), 'iex'))
        self.assertIsNone(detect_setups('AAPL', bars(), NOW-timedelta(minutes=1), 'iex'))

    def test_doji_not_engulfing(self):
        frame = bars()
        frame.iloc[-1] = [10., 12., 9., 10., 100.]
        self.assertEqual(detect_setups('AAPL', frame, NOW, 'iex')['pattern_detected'], '')

    def test_query_filter_and_cached_expiration(self):
        class Provider:
            feed = 'iex'
            def download_many_range(self, *args, **kwargs):
                return {'AAPL': bars()}
        scanner = SetupScanner(Provider, ['AAPL'])
        rows, metadata = scanner.search('bull', NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(metadata['symbols_requested'], 1)
        self.assertEqual(scanner.search('bear', NOW)[0], [])
        self.assertEqual(scanner.search('bull', NOW+timedelta(minutes=21))[0], [])

    def test_failure_does_not_reuse_old_success(self):
        class Provider:
            feed = 'iex'
            def download_many_range(self, *args, **kwargs):
                return {'AAPL': bars()}
        scanner = SetupScanner(Provider, ['AAPL'])
        scanner.search('bull', NOW)
        scanner._refreshed = 0
        with patch.object(Provider, 'download_many_range', side_effect=RuntimeError('private error')):
            with self.assertRaises(AlpacaDataError) as caught:
                scanner.search('bull', NOW)
            self.assertNotIn('private', str(caught.exception))
            self.assertIsNone(scanner._frames)

    def test_unknown_query_does_not_fetch(self):
        scanner = SetupScanner(lambda: self.fail('must not fetch'), ['AAPL'])
        self.assertEqual(scanner.search('head shoulders', NOW)[1]['data_status'], 'unsupported_setup')


class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from main import module
        cls.app = module

    def test_short_history_has_distinct_status(self):
        with patch.object(self.app, 'download_market_data', side_effect=[pd.DataFrame(), bars().iloc[:20]]):
            result = self.app.get_probabilities(ticker='AAPL', timeframe='1mo', chart_interval='1d', authorization=None)
        self.assertEqual(result['data_status'], 'insufficient_history')
        self.assertEqual(result['bars_available'], 20)

    def test_scanner_preserves_free_limit_and_does_not_consume_reveal(self):
        row = detect_setups('AAPL', bars(), NOW, 'iex')
        with patch.dict('os.environ', {'MARKET_DATA_PROVIDER':'alpaca'}), patch.object(self.app.SCANNER, 'search', return_value=([dict(row,ticker=s) for s in ['AAPL','NVDA','AMD','TSLA']], {'data_status':'available'})), patch.object(self.app, 'claim_daily_win_rate_reveal') as claim:
            result = self.app.search_live_setups('bull', chart_slot=1, reveal_ticker='AAPL', authorization=None)
        self.assertEqual(len(result['candidates']), 3)
        self.assertEqual(result['max_allowed_charts'], 2)
        self.assertTrue(all(r['win_rate'] is None for r in result['candidates']))
        claim.assert_not_called()
