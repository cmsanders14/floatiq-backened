# Bounded setup search

When MARKET_DATA_PROVIDER=alpaca, /api/setup-search now scans actual Alpaca bars on demand instead of reading the legacy live_scan_cache table. No schema or database writes are required. The default universe contains 30 stocks; SETUP_SCAN_SYMBOLS can override this with 1–50 comma-separated symbols. This is not a whole-market scanner.

The scan uses completed 15-minute regular-session candles. A signal's observed_at is the candle's end time, not the fetch time. Signals expire after 20 minutes and cannot carry over into another New York calendar day. Holidays, closed markets, and missing feed data produce no fresh candidates rather than old results. IEX is single-venue data; volume is not consolidated market volume.

Supported searches: bull/bullish, bear/bearish, engulfing, breakout, breakdown, and combinations such as bullish engulfing. An engulfing pattern requires opposite candle bodies with the new body fully covering the previous body and consecutive candles. A breakout closes above the prior 20 regular-session highs; a breakdown closes below the prior 20 lows. Relative volume compares the latest candle with those prior 20 bars, not a time-of-day historical baseline.

Requests share an unfiltered 30-second in-memory data snapshot; freshness and query matching are checked each time. A lock bounds concurrent refreshes. Fetch failures discard prior data and back off for 30 seconds. Batch pagination is bounded and failures do not publish partial history. Results are ranked by relative volume with ticker as a deterministic tie breaker. No win rate is inferred from these signals, and requests without an estimated win rate do not consume the Free daily reveal.

The response preserves candidates, ticker, pattern_detected, current_price, current_rvol, tier, result_limit, and max_allowed_charts. New metadata reports feed, coverage, requested/fresh symbol counts, interval, supported queries, and a message. Clients should display message when there are no candidates. Status values: available, no_matching_setups, no_fresh_bars, unsupported_setup; provider failures return HTTP 502 for the existing retry flow.

The non-Alpaca legacy path filters database write timestamps to 120 seconds before ranking/limiting. This is only a conservative fallback and does not establish market observation freshness. With Alpaca selected, that cache is not queried.

Pattern analysis now distinguishes insufficient_history from unavailable and returns bars_available, minimum_bars_required, and message. The frontend should show the message rather than claiming a feed outage for a short date range.

Validation includes synthetic positive/negative signals, query-specific results, stale and unfinished candle rejection, failure cache clearing, batch pagination, malformed OHLC rejection, and Free entitlement/reveal protection. Hosted verification and frontend status display must also be checked before calling the release finished.
