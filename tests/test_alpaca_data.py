import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from floatiq_core.alpaca_data import AlpacaMarketDataProvider, AlpacaDataError


def bar(day):
    return dict(t=f"2026-10-{day:02d}T14:00:00Z", o=10, h=12, l=9, c=11, v=100)


class AlpacaTests(unittest.TestCase):
    def test_batch_pagination_does_not_drop_later_symbols(self):
        p = self.provider([{"bars": {"AAPL": [bar(12)]}, "next_page_token": "next"},
                           {"bars": {"TSLA": [bar(13)]}, "next_page_token": None}])
        frames = p.download_many_range(["AAPL", "TSLA"], "2026-10-12", "2026-10-14", "15m")
        self.assertEqual(len(frames["AAPL"]), 1)
        self.assertEqual(len(frames["TSLA"]), 1)
        self.assertEqual(parse_qs(urlparse(self.requests[0].full_url).query)["symbols"], ["AAPL,TSLA"])

    def test_invalid_ohlc_rejected(self):
        bad = bar(12)
        bad['h'] = 1
        p = self.provider([{"bars": {"AAPL": [bad]}}])
        with self.assertRaises(AlpacaDataError):
            p.download("AAPL", "1d", "1m")

    def test_render_credential_names(self):
        with patch.dict("os.environ", {"APCA_API_KEY_ID": "test-key", "APCA_API_SECRET_KEY": "test-secret"}, clear=True):
            p = AlpacaMarketDataProvider()
            self.assertEqual(p._key, "test-key")
            self.assertEqual(p._secret, "test-secret")
            self.assertEqual(p.feed, "iex")

    def provider(self, pages):
        self.requests = []
        responses = iter(pages)
        def opener(request, timeout):
            self.requests.append(request)
            return io.BytesIO(json.dumps(next(responses)).encode())
        return AlpacaMarketDataProvider("test-key", "test-secret", "iex", opener)

    def test_pagination_normalization_and_read_only_host(self):
        p = self.provider([{"bars": {"AAPL": [bar(12)]}, "next_page_token": "next"},
                           {"bars": {"AAPL": [bar(13)]}, "next_page_token": None}])
        frame = p.download_range("aapl", "2026-10-12", "2026-10-14", "5m")
        self.assertEqual(len(frame), 2)
        self.assertEqual(frame["Close"].tolist(), [11, 11])
        self.assertEqual(frame.attrs["feed"], "iex")
        for req in self.requests:
            self.assertEqual(urlparse(req.full_url).netloc, "data.alpaca.markets")
            self.assertEqual(req.get_method(), "GET")
            self.assertNotIn("test-secret", req.full_url)
        params = parse_qs(urlparse(self.requests[1].full_url).query)
        self.assertEqual(params["page_token"], ["next"])
        self.assertEqual(params["timeframe"], ["5Min"])

    def test_empty_data_is_not_fabricated(self):
        p = self.provider([{"bars": {}, "next_page_token": None}])
        self.assertTrue(p.download("AAPL", "1d", "1m").empty)

    def test_repeated_pagination_fails_instead_of_returning_partial_history(self):
        p = self.provider([{"bars": {}, "next_page_token": "same"}]*2)
        with self.assertRaises(AlpacaDataError):
            p.download("AAPL", "1d", "1m")

    def test_auth_errors_do_not_echo_secret(self):
        def fail(req, timeout):
            raise HTTPError(req.full_url, 403, "test-secret", {}, None)
        p = AlpacaMarketDataProvider("test-key", "test-secret", "iex", fail)
        with self.assertRaises(AlpacaDataError) as caught:
            p.download("AAPL", "1d", "1m")
        self.assertIn("403", str(caught.exception))
        self.assertNotIn("test-secret", str(caught.exception))

    def test_malformed_bars_rejected(self):
        p = self.provider([{"bars": {"AAPL": [{"t": "invalid"}]}}])
        with self.assertRaises(AlpacaDataError):
            p.download("AAPL", "1d", "1m")

    def test_unsupported_interval_rejected_before_request(self):
        p = self.provider([])
        with self.assertRaises(AlpacaDataError):
            p.download("AAPL", "1d", "bad")
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
