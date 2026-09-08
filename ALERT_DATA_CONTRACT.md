# FloatIQ alert data contract

FloatIQ keeps two alert types separate so the wording always matches the evidence.

## Institutional-scale trade

A market-data feed can flag an unusually large print using notional value, relative volume,
average daily volume, repeated blocks, venue, and trade conditions. It should not claim that
the buyer's identity is known.

FloatIQ must never classify activity from a fixed quantity such as 20,000 shares or coins.
The scoring contract compares each trade with that asset's normal trade size, average daily
volume, relative volume, float or circulating supply, and dollar notional value. Stock and
crypto notional baselines are calibrated separately.

Example statuses:

- `potential`: "Potential institutional-scale PLTR trade: 20,000 shares at $120.58 ($2.41M)."
- `confirmed_market_data`: "Large PLTR trade confirmed by the market-data feed. Buyer identity is not public."

## Verified insider purchase

An officer, director, or other reportable holder can be named only when a public filing or
another authoritative source identifies the person and transaction. These are stored as
`verified_insider_purchase` with status `verified_public_filing`, plus source name, URL, event
identifier, actor name, and actor role.

Example:

- "Verified insider purchase: [executive] reported buying [shares] of [ticker]. Source: SEC filing."

The ingestion worker must retain the source link and must never convert an anonymous large
trade into a named purchase merely because time has passed.
