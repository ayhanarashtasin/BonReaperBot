# SDK and venue audit (BR-002)

Audited 2026-11-06 against the installed package source and live public responses.

| Item | Result |
|---|---|
| Package | `polymarket-client` 0.17.0, pinned in `pyproject.toml` and `uv.lock` |
| Legacy client | `py-clob-client` not used and not in the lockfile |
| Python | Requires >=3.11. Project pins 3.11 |
| Smoke test | `scripts/smoke_read_only.py` |

## Sources

- https://pypi.org/project/polymarket-client/
- https://docs.polymarket.com/getting-started/python
- https://docs.polymarket.com/trading/fees
- https://docs.polymarket.com/concepts/resolution
- https://docs.polymarket.com/market-data/realtime-data
- https://docs.polymarket.com/api-reference/geoblock

## Signatures in use

```python
AsyncPublicClient().list_series(*, slug, ..., page_size=20) -> AsyncPaginator[Series]
AsyncPublicClient().list_events(*, series_ids, closed=False, order, ascending,
                                end_date_min, version, page_size=20) -> AsyncPaginator[Event]
AsyncPublicClient().get_event(*, slug) -> Event
AsyncPublicClient().get_order_book(*, asset_id) -> OrderBook
AsyncPublicClient().list_price_history(*, asset_id, start, end, bucket_seconds) -> AsyncPaginator
AsyncPublicClient().subscribe(MarketSpec(asset_ids=[...], custom_feature_enabled=True))
AsyncPublicClient().subscribe(CryptoPricesChainlinkTwapSpec(window_seconds=60, symbols=[...]))
AsyncSecureClient().subscribe(CryptoTwapPriceSpec(symbols=["btcusd"]))   # never constructed here
```

## Models and how they differ from the planning documents

| Topic | Planning docs | Actual | Handling |
|---|---|---|---|
| Market start | Implied by `start_date` | `start_date` is listing time; window is `Event.schedule.start_time` | ADR-0003 |
| Asset IDs | `.outcomes.yes/.no` | Confirmed: `token_id` (v1) or `position_id` (v2). `yes` is just the first listed outcome | Mapped by label; Up is YES |
| Protocol version | CTF or V2 | `ProtocolVersion.V1` / `V2`. Current BTC 5m are `v1` | Unknown version rejected |
| Fee fields | `fees_enabled`, rate | Also `FeeSchedule(exponent, rate, taker_only, rebate_rate)` and `fee_type` | All captured |
| Fee formula | `shares * rate * p * (1-p)` | `shares * rate * (p*(1-p))**exponent`, rounded to 5 decimals | Implemented in full |
| Book order | Unspecified | REST and stream levels list the best price **last** | Always sorted locally |
| TWAP spec | `CryptoTwapPriceSpec` | Confirmed, requires `AsyncSecureClient` | Facade only |
| Public TWAP | Not mentioned | `CryptoPricesChainlinkTwapSpec`, RTDS, **shuts down 2026-11-30** | ADR-0008 |
| Trades and depth | Level updates reconstruct the book | Trades send no `price_change`; `book` follows | ADR-0005 |
| Tick size | Per-market constraint | Changes during a market near price extremes | ADR-0006 |
| Stream queue | Bounded, no silent drop | SDK uses drop-oldest with a `dropped` counter | Counter invalidates books |

## Current BTC 5m market facts (observed)

| Field | Value |
|---|---|
| Series | `btc-up-or-down-5m`, recurrence `5m` |
| Outcomes | `Up`, `Down` |
| Tick / minimum size | 0.01 (narrows to 0.001) / 5 shares |
| Fee | `crypto_fees_v2`: rate 0.07, exponent 1, taker only, rebate rate 0.2 |
| Oracle | `https://data.chain.link/streams/btc-usd-twap-60s-streams` |
| Rule | Up if TWAP over the range is greater than or equal to the price at its beginning |
| Normalized rule hash | `0958a68b74e364754cdf988749053ccce1592942c2cd7639420109897fd778f4` |

## Not verifiable without real account access

- How a real BUY fee is collected (ADR-0002).
- Real submit, ACK and cancel latency; real queue position; real rebates.
- The authenticated PolyBolt TWAP stream.
- Merge and redemption mechanics on a real wallet.
