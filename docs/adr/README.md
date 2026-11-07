# Architecture decision records

Architecture.md holds ADR-01 to ADR-10. These records cover decisions and discrepancies found during
implementation. Where the docs and the installed SDK disagreed, the safer reading was taken.

## ADR-0001: Project root

The documents sketch a `bone-reaper/` folder. The repository root is used instead, since the documents
were placed there. The layout inside matches Architecture section 3, with these additions:
`strategy/fees.py`, `execution/factory.py`, `research/scan_runner.py`, `research/experiments.py`,
`research/model_research.py`, `live_sim.py`.

## ADR-0002: BUY fee collection

**Conflict.** The PRD and Architecture say a taker BUY can be charged through fewer shares received.
The venue docs state fees "in USDC", and SDK 0.17.0's `adjust_buy_amount_for_fees` sizes market buys
with the fee on top of the notional.

**Decision.** Both are modelled as `FeeCollection`. `SHARES_ON_BUY` is the default because a property
test shows it never gives a better net cost than `CASH_ON_TOP`.

**Status.** Unverified against a real fill, since none exists. Confirm at BR-702 before relying on it.

## ADR-0003: Market window times

**Finding.** `Market.state.start_date` is the listing time, about 24 hours before the window. The
5-minute window comes from `Event.schedule.start_time`.

**Decision.** Discovery reads the event schedule, verifies the duration is exactly 5 minutes, and never
parses the slug. The market's series and rule hash must also match.

## ADR-0004: Raw payload capture

**Conflict.** Architecture 4 asks for the exact raw payload. The SDK exposes typed events, not frames.

**Decision.** Keep the official SDK. `payload_json` stores a lossless normalized form with Decimals as
exact strings. Unknown fields the SDK drops are not captured. Accepted because bypassing the SDK with a
private socket client is the larger risk.

## ADR-0005: Book semantics measured from real data

**Finding** (530,171 recorded events). Every `price_change` has exactly one entry per outcome. A trade
produces no `price_change`: consumed levels change only at the next `book` message. So a book rebuilt
strictly from the documented model carried stale levels, which caused about 31,000 false invalidations
and 795 REST recoveries in 15 minutes. There are no sequence numbers. Book hashes could not be verified
because their algorithm is not documented.

**Decision.** Use each `price_change`'s reported best bid and ask to prune provably stale levels.
Reported prices better than ours mean a missing level: the book goes `SYNCING` (unusable), then
`INVALID` after 2 seconds without repair. REST recovery is limited to one per asset per 5 seconds.

**Evidence.** Invalidations fell to zero. See `reports/readiness_memo.md` for validation rates. Sizes of
partly consumed levels stay stale until the next `book`, usually milliseconds later.

## ADR-0006: Dynamic tick size

**Finding.** The venue narrows ticks from 0.01 to 0.001 near price extremes during a market.

**Decision.** Pricing and validation read the live tick from the book, not from `MarketSpec`. A tick
change is normal and is not a data gap.

## ADR-0007: Clock offset

**Finding.** This machine's clock runs about 780 ms ahead of Binance server time. Most of the observed
870 ms feed lag is that offset, with real latency nearer 100 ms.

**Decision.** The recorder stores a periodic `clock_probe`, and replay shifts source timestamps by it.
The clock itself is not changed, since that is a system setting and the operator's call. Binance is used
only as a time reference.

## ADR-0008: TWAP source and its deadline

**Finding.** The authenticated `prices.crypto.twap` route needs credentials. A public RTDS TWAP stream
exists but is announced to shut down on 2026-11-30.

**Decision.** Record from RTDS for now, labelled deprecated in every record. `SecureTwapFacade` is
implemented and tested with an injected subscribe function, holds no client, and exposes only a stream.
After the deadline, the state is explicitly unavailable unless an operator supplies credentials.

## ADR-0009: Price to beat

**Finding.** Official `priceToBeat` appears in event metadata only after the market starts or closes,
and `finalPrice` several minutes after close.

**Decision.** Live strikes are the observed TWAP at window start, with `strike_provenance` recording
that. Official values are recorded after close. Outcomes come only from official outcome prices.

## ADR-0010: Forecast dataset

**Finding.** Historical Chainlink TWAP is not available in bulk.

**Decision.** Forecast research uses official labels with Binance 1-minute closes as a labelled proxy,
measuring distance from Binance's own price at window start so the Chainlink/Binance basis does not
leak in. The economic audit uses the real recorded TWAP. Forecast scores never decide economic use.

## ADR-0011: Simulator stream mirroring

**Finding.** The YES and NO books mirror each other, and each match appears once, on one token.

**Decision.** A resting YES bid at p is also consumed by a taker BUY of NO at 1-p. Trades are
deduplicated by transaction, size and price so a venue change to report both sides cannot double-count.

## ADR-0012: Postgres in tests

**Decision.** The Postgres store is verified against a real embedded PostgreSQL 16 (`pgserver`, dev
only), including a real second process competing for the writer lock. The ledger default for research
remains in-memory; ADR-05's rule that real trading uses Postgres stands.

## ADR-0013: Research runs without the daily stop

**Finding.** The $10 daily stop latched after 7 to 8 traded markets, leaving most of the sample unevaluated.

**Decision.** `compare` reports the capped strategies as configured, and adds clearly named `*_no_loss_stop` variants
that lift only that stop. Order and exposure limits stay. These variants exist only in research code, and no config
file can produce them.

## ADR-0014: What a candidate model must show

**Finding.** The first audit returned `CANDIDATE` for a model used in 0.7% of decisions, and that also lost money.

**Decision.** A variant counts only if it used the model in at least half of its decisions, beat the midpoint in every
scenario, and made a profit in every scenario. Even then the model stays disabled until an operator enables it.

## ADR-0015: A read-only dashboard

**Context.** The PRD does not require a web dashboard for the first release. The operator asked for one to see
everything in one place.

**Decision.** `bone-reaper dashboard` is a standard-library HTTP server with one page and no new dependencies. It binds
to `127.0.0.1` as a constant, refuses other `Host` names, serves a fixed list of GET paths, and has no write route. It
shows reports as written and marks anything missing as unavailable. The verdict is parsed from the readiness memo, and
the live-trading blocks come from `live_gate_reasons`, so the page cannot disagree with either.

**Live view.** Paper and shadow sessions write `live.json` every 5 seconds. A failed snapshot is logged and skipped,
because observing a session must never stop it, and a test confirms that taking snapshots leaves the metrics checksum
unchanged.

**Accepted.** The browser fetches Fira fonts from Google Fonts, as the chosen design system specifies. It is the page's
only outside request, carries no project data, and falls back to system fonts.
