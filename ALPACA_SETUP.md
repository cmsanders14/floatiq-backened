# Alpaca read-only prototype

This adds a market-data adapter, not an order API or a scheduled setup scanner.
No Alpaca SDK or new dependency is required. No deployed settings have been changed.

1. Merge floatiq_core/alpaca_data.py, the market_data.py provider selection,
   scripts/check_alpaca_data.py and tests/test_alpaca_data.py into the current backend.
2. Keep APCA_API_KEY_ID and APCA_API_SECRET_KEY in Render secrets, never in GitHub.
3. Run `python scripts/check_alpaca_data.py` in that environment. This requests
   seven calendar days of AAPL daily bars and prints only feed, count and last timestamp.
4. After successful verification, set MARKET_DATA_PROVIDER=alpaca and
   ALPACA_DATA_FEED=iex and redeploy. Existing Yahoo remains the default until then.

IEX is single-venue test data, not consolidated US volume. SIP requires appropriate
access. Neither this adapter nor a personal data subscription establishes rights
to redistribute data or analytics to customers. Confirm commercial access separately.

Supported intervals: 1m, 5m, 15m, 1h, 1d. Supported periods: 1d, 5d, 7d, 1mo,
6mo, 1y, max (requests from 2016; actual available history depends on the feed).
Pagination is bounded and fails rather than returning silently truncated history.
Bars use corporate-action-adjusted prices and a UTC index. Empty data is not fabricated.
Other direct Yahoo calls elsewhere in the app are unchanged.

Next required implementation: a bounded symbol universe, actual setup detection,
observed-at timestamps and feed provenance, missing/expired signal handling,
scheduled ingestion and hosted verification. The current database write timestamp
is not sufficient evidence of market-data freshness. The scanner is NOT fixed merely
by selecting this provider.

Local validation: seven mocked adapter tests pass. Live credentials and hosted
connectivity have not been verified. These tests do not place paper or live orders.

Sources:
- https://docs.alpaca.markets/us/reference/stockbars
- https://docs.alpaca.markets/us/docs/about-market-data-api

