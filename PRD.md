# Bone Reaper-Style Bot | Product Requirements Document (PRD)

**Version:** 1.0  
**Prepared:** 2026-11-06  
**Product status:** Design specification, not a validated profitable strategy  
**Implementation order:** `PRD.md` -> `Architecture.md` -> `tasks.md`  
**Initial release:** Read-only BTC 5-minute research and conservative paper trading  
**Primary platform:** International Polymarket prediction markets, subject to account and geographic eligibility

## 1. Product summary

Build a testable, event-driven Python research and trading platform inspired by the *reported* behavior of the trader nicknamed Bone Reaper. The bot studies short-duration binary crypto prediction markets and can eventually attempt: (1) fee-aware complete-set arbitrage; (2) passive two-outcome market making; and (3) Chainlink-aligned probabilistic quote adjustment. The system must account for partial fills, queue position, market version, fees, settlement, latency, and risk. It must **not** assume that placing orders on both outcomes is an atomic hedge.

The reported profits and execution frequency of any third-party bot are unverified for this project, are not requirements, and must never be promised. The product goal is to measure executable edge, not to reproduce another trader's headline PnL.

## 2. Problem and motivation

The naive signal `YES ask + NO ask < 1` creates many false opportunities because actual filled quantities can differ; taker fees, depth, price changes, and rounding can consume the entire theoretical profit. Passive bids can avoid taker fees but introduce queue and one-sided-inventory risk. A historical backtest using only Binance candles or trade prints cannot reconstruct Polymarket maker fills. The product solves these research and execution problems with accurate synchronized data, an event-driven strategy, an auditable order-management system, and conservative simulations.

## 3. Intended user and key journeys

**Primary user:** An experienced Python/Codex user with historical Binance candles, aggregated trades, reference prices, and market microstructure data, building a proprietary quantitative trading bot.

Journeys, in recommended order:

1. Start a read-only recorder, discover current BTC 5-minute Up/Down markets, verify exact outcome identifiers and settlement rules, and collect both outcome books plus Chainlink reference observations.
2. Run a read-only scanner and inspect each apparent opportunity with executable depth, net matched payout, exact fees, freshness, and a reason it passed or failed.
3. Replay recorded Polymarket data against candidate strategies with realistic queue assumptions, and get reproducible net-PnL/risk reports.
4. Paper trade passive YES/NO bids and observe what happens when only one fills, including simulated hedging, cancellation, and settlement.
5. Optionally enable a calibrated fair-value model, comparing out-of-sample results against a simpler baseline.
6. Only after evidence and human review, consider a tightly constrained micro-live mode in a permitted jurisdiction.

## 4. Scope and phased releases

| Release | Name | In scope | Out of scope |
|---|---|---|---|
| R0 | Compliance and discovery | Official API audit, market rules, instrument versions, diagnostics | Any real order |
| R1 | Data + scanner | BTC 5m books, Chainlink TWAP, Binance inputs, market rollover, fee-aware signals | Auto-execution |
| R2 | Simulator | Replay, depth-aware taker fills, queue-aware maker fills, paper positions, accounting | Claims of real fillability |
| R3 | Passive market maker | Post-only bid simulation, hedge logic, inventory skew, risk controls, monitoring | Unlimited risk, unmanaged directional betting |
| R4 | Model research | Forecast probabilities, calibration, walk-forward evaluation | Using unvalidated models live |
| R5 | Conditional micro-live | Real order adapter, reconciliation, small supervised orders | Unsupervised high-frequency production at launch |
| R6 | Expansion | ETH 5m, BTC/ETH 15m, performance optimization | Assumed cross-market equivalence |

Do not implement R5 until R0 through R4 quality gates are satisfied. ETH or 15-minute support must not be enabled by just changing a market slug: verify every instrument's rules.

### Explicit non-goals

- No automatic copy trading, wallet scraping, or reverse engineering of a specific trader's private logic.
- No guarantee of positive returns, risk-free operation, 42 transactions per minute, or specific profit figures.
- No latency race or co-location optimization before an experimentally demonstrated net edge.
- No geographic restriction circumvention or trading from blocked locations.
- No naive price-touch maker backtesting; no assumption that a batch submission fills both legs.
- No aggressive leveraged trading, lending, or perpetual hedges in the MVP.
- No web dashboard required for first release; CLI reports and structured logs suffice.

## 5. Strategy requirements

### S1. Depth- and fee-aware paired-outcome scanner

Scan a verified binary complementary market for equal **net** quantities of Up and Down shares. Calculate executable weighted-average purchase cost from available asks at each requested size; apply the market's actual fee configuration, share-received treatment, minimum lot rules, rounding, and a conservative execution reserve.

For a completed pair of `q` net shares:

`locked_gross_payout = q * 1.00`  
`locked_net_edge = locked_gross_payout - real_cash_spent - execution_costs - settlement_costs`

A signal is merely *candidate executable arbitrage* until fills, quantities, and final costs are confirmed. The scanner must also show the probability and financial consequence of an unpaired execution. No taker/taker order route is enabled by default.

Illustrative example: a 0.46 Up ask and 0.51 Down ask imply 0.03 gross profit per complete pair **before fees**. With a crypto taker fee coefficient of 0.07 on each leg, an indicative total fee-equivalent of about 0.034881 per pair makes this loss-making before execution costs. The actual implementation must apply the current market-level fee schedule and account for fees on buys reducing shares received; it must not simply subtract this illustration as a flat constant.

### S2. Passive dual-outcome market making

Generate maker-only resting BUY quotes on complementary outcomes with prices chosen from fair value, best bid/ask, market tick, expected hedge price, available capital, probability of fill, and inventory. Both buy orders may be submitted as a batch where supported, but each order outcome must be handled independently. The bot may later quote SELL orders only when sufficient outcome inventory is already held and the margin/risk rules are met.

Market making differs from locked arbitrage: one-sided fills, adverse selection, stale quotes, and time-to-expiry are risks. Cancel and reprice on material quote drift, not every single book update. Require price, size, and time guards before every new quote.

### S3. Inventory and hedge controller

Define `paired = min(net_yes, net_no)`, `unpaired_yes = max(net_yes - net_no, 0)`, `unpaired_no = max(net_no - net_yes, 0)`, with quantities after any share-denominated fees. After every fill, recompute cash reservations, open orders, remaining payout, available counterpart liquidity, and worst-case loss.

Preferred response priority:

1. Keep or place a passive counterpart quote if within risk and expiry limits.
2. Execute a conservative opposite-side hedge only if the all-in paired result is profitable, **or** if a specifically authorized risk-reduction policy accepts its known cost.
3. Reduce exposure with a permitted sell/unwind and account for fees and possible loss.
4. Stop placing new risk and alert if a safe action is unavailable.

The system must never hide a directional position inside a reported fully hedged profit number.

### S4. Chainlink-aligned fair-value research

Research a probability `p_up` for the official settlement event using the exact market's price-to-beat, Chainlink TWAP observations, seconds remaining, volatility, Polymarket books, and optionally Binance-derived aggTrade/order-flow features. Binance prices can inform the forecast but cannot substitute for the settlement oracle. Start with simple baseline models before gradient boosting or complex ML.

The model is initially **advisory only**, and must be evaluated for Brier score, log loss, calibration, and incremental strategy PnL on chronological held-out markets. Fail-closed if required features or oracle observations are missing or stale. Do not assume an observed probability mispricing is a realized trade profit.

## 6. Functional requirements

Each `FR` below must be traceable to at least one task in `tasks.md` and to tests or observable verification.

| ID | Requirement | Priority | Acceptance evidence |
|---|---|---|---|
| FR-01 | Discover the active and upcoming BTC 5m markets without hard-coded token identifiers | P0 | Correct market rollover and verified market metadata fixture |
| FR-02 | Support CTF and Protocol V2 outcome asset identifiers; reject unsupported market types | P0 | Both-version fixtures pass; invalid pairs rejected |
| FR-03 | Read and persist exact rules, price-to-beat, start/close timestamps, tick/lot, fee metadata, trading status | P0 | MarketSpec schema validation and rule-hash checks |
| FR-04 | Consume and persist both outcome L2 streams, snapshots, changes, trades, and receive/source timestamps | P0 | Replayed book matches sampled REST snapshot within documented tolerance |
| FR-05 | Detect missing events, stale streams, crossed/invalid books, and recover by resnapshot | P0 | Fault-injection reconnection tests |
| FR-06 | Capture synchronized Chainlink 60-second TWAP observations and optional Binance aggTrades/spot | P0 | Traceable event-time alignment, provenance and gap reports |
| FR-07 | Compute fee- and depth-aware paired executable prices for selectable sizes | P0 | Property tests for order-book depth, fees, and rounded net shares |
| FR-08 | Produce read-only opportunities and rejection reasons | P0 | CLI reports, no order endpoint reachable in read-only mode |
| FR-09 | Replay recorded events with deterministic simulated execution and queue assumptions | P0 | Same seed and inputs create byte-equivalent metric outputs |
| FR-10 | Model maker partial fills, cancel races, and adverse selection | P0 | Conservative maker fill and no-fill tests |
| FR-11 | Durable order manager with state transitions, fill dedupe and unknown-order recovery | P0 | Duplicate/timeout/restart integration tests |
| FR-12 | Full inventory, reserved capital, matched/unmatched positions and settlement ledger | P0 | Accounting invariants and post-redemption reconciliation |
| FR-13 | Pre-trade, intra-trade and operational risk gates | P0 | Every forbidden order is rejected before execution |
| FR-14 | Paper-trading mode using the same signal and risk code as live | P0 | Isolated paper adapter and replay/paper parity tests |
| FR-15 | Passive maker quote engine with post-only, bounded refresh, and hedge logic | P1 | One-sided fill scenarios stay within defined loss/exposure caps |
| FR-16 | Optional fair-value baseline, training and calibration reports | P1 | Strict chronological held-out comparison against baseline |
| FR-17 | Authenticated order adapter and user order/fill updates | P1 | Disabled by default; opt-in integration tests with mock exchange |
| FR-18 | Real-order launch gate checks jurisdiction, config, wallet and operator approval | P0 | Geoblocked/unknown approval means zero sends |
| FR-19 | Monitor streams, balances, orders, PnL, latency and kill switch | P0 | Alert and kill-switch tabletop drill |
| FR-20 | Export experiment reports and a reproducibility manifest | P0 | One-command report with config hash and data coverage |

## 7. Non-functional requirements and defaults

These are **prototype guardrails**, not optimized parameter estimates. Recalibrate after real feed measurement.

| Area | Requirement or default | Rationale |
|---|---|---|
| Language | Python >=3.11, typed asyncio services | Stream handling and correctness |
| Currency arithmetic | `Decimal` or exact integer atomic units; never binary floating point for money | Avoid rounding errors |
| Clock | UTC exchange/source and local receive time; monitor wall-clock skew | Preserve causal ordering |
| Read-only mode | Network trading methods not instantiated or authorized | Structural prohibition of unintended orders |
| Default symbol | BTC; duration 5 min; one active market | Control MVP complexity |
| Max paper order | $10 equivalent | Limited experimental exposure |
| Max paper unpaired | $20 equivalent | Limit directional risk |
| Max paper total gross | $100 equivalent | Bound simulator losses |
| Daily paper loss limit | $10 | Automatic halt demonstration |
| Maximum book age | 1000 ms, adjustable | Reject stale quotes |
| Maximum reference age | 2000 ms, adjustable and feed-specific | Reject stale fair values |
| Quoting cutoff | 20 seconds before close, adjustable | Avoid final-second execution risk |
| Minimum net paired edge | $0.50 AND 1% ROI in scanner demo | Filter noise; not a profitability claim |
| Maker rebates | Excluded from pre-trade guaranteed edge | Rebates are variable and paid later |
| Crash recovery | No new orders until fresh account reconciliation | Avoid duplicated/unknown live risk |
| Sensitive config | Credentials via environment/secret manager, not files or logs | Key protection |
| Availability | Fail closed for order entry on data outages; recorder may reconnect | Prioritize safety over uptime |

**Important GTD behavior:** Polymarket documents a minimum future expiration and a 60-second security offset. Therefore 5-minute rapid repricing cannot rely on short GTD lifetime alone. Prefer post-only GTC with explicit cancel/reconcile and an expiry cutoff. Never assume a cancel request prevents a concurrent fill.

## 8. Eligibility, safety and governance

- Live action must comply with applicable laws, account eligibility, market terms, and Polymarket's current geographic restrictions. At startup and periodically, call `GET https://polymarket.com/api/geoblock` from the *actual execution location*. If blocked, uncertain, or API unavailable, do not submit new real orders. Do not route around restrictions.
- Existing position reduction should follow the platform's actual restriction class, not an assumed universal close-only permission.
- The default `mode` is `read_only`, then optionally `paper` or `shadow`. `live` requires explicit configuration plus a separate operator authorization, and must never be auto-selected.
- Never place real orders in CI, standard tests, or a development startup; opt-in metered tests must have their own protected environment.
- Do not store private keys, API secrets, seed phrases, or personally identifying account data in raw telemetry or exported reports.
- Journal all intents, order acknowledgements, fills, cancels, adjustments, risk rejections, manual overrides, and settlement events.

## 9. Data dependency matrix

| Data | Must-have? | Source and usage | Limitation |
|---|---|---|---|
| Polymarket market metadata and rules | Yes | Official SDK/API; pairing and eligibility | Rules and protocol versions may change |
| Polymarket outcome L2 + trades | Yes | Official public market stream; live scanner | Historical complete depth must be independently collected |
| Polymarket user order and fill events | Yes for real execution | Authenticated user stream and REST reconciliation | Stream gaps and delayed fills are possible |
| Polymarket market fee configuration | Yes | Market-level metadata/fee API | Do not assume a global constant applies to every market |
| Chainlink 60s TWAP | Yes for probability model | Authenticated `prices.crypto.twap` reference stream | Capturing exact window boundary is important |
| Binance aggTrades/candles | Optional for initial scanner, required for model experiments using them | User's historical Binance dataset and live feed | Not Polymarket fill or settlement evidence |
| Historical Polymarket L2 | Yes for credible maker backtest | Recorder or quality-audited third-party source | Price history alone cannot determine maker fills |
| Settlement and accounting | Yes | Official resolution/positions APIs | Winning shares are not spendable cash until redeemed/merged |

**One-time provenance check:** External datasets, even if they contain promising order-book data, must have timestamps, market IDs, completeness checks, and reproducible loading scripts before analysis.

## 10. Reporting and user-facing outputs

First releases use a CLI and generated research artifacts:

- `markets`: market ID, symbol, start/end, version, outcome asset IDs, fee/rule status.
- `record`: continuous raw streams with data-gap and market-rollover diagnostics.
- `scan`: timestamped candidate signals with `q_net`, YES/NO effective prices, fee breakdown, simulated locked edge, available size and reject reasons.
- `backtest`: equity curve, cash ledger, paired/unpaired inventory, realized/mark-to-market PnL, maker/taker fill breakdown, time-in-exposure and drawdown.
- `compare`: performance by market duration, symbol, fee regime, model type, volatility regime and latency stress level.
- `health`: clock skew, stale feeds, queue backlog, open/unknown orders, fill mismatches and kill-switch state.
- `reconcile`: exchange vs local order/fill/balance reconciliation and unresolved differences.

Every report includes git commit, UTC generation time, SDK version, input manifest hashes, model version, fee schedule, market list, config hash, simulator assumptions and known data gaps.

## 11. Success measures, not profit guarantees

### Engineering quality gates

- **G0 Discovery:** All supported market fixtures map the correct pair; unsupported/ambiguous markets are rejected. No live orders possible.
- **G1 Data:** Can record and replay a complete market lifespan including rollover. Validate bid/ask state against independent snapshots, with every discontinuity marked; source-availability and recording completeness are reported, not guessed.
- **G2 Scanner:** Zero intentional false positives in a deterministic test corpus covering share-denominated fees, depth changes, rounding and one-sided fills. Reports separate gross, indicative and actually filled edge.
- **G3 Simulator:** Replay with fixed seed is deterministic; queue order and latency assumptions are explicit, configurable and stress tested.
- **G4 OMS/Ledger:** Zero unexplained duplicate fills or cash/position inconsistencies in fault tests; unknown orders freeze risk-increasing submissions until reconciled.
- **G5 Research:** Positive net results, if any, must remain positive in a **chronological held-out sample** under conservative fill, latency and fee stress. If not, classify the strategy as unproven or negative.
- **G6 Micro-live:** Requires independent human authorization, real venue eligibility, audited wallet state, small limits and a tested emergency cancel path. Absent these, stay in paper/shadow mode.

### Research metrics to optimize, in priority order

1. Realized net PnL after fees, slippage, hedging losses and actually received rebates.
2. Max drawdown and worst-case unpaired loss.
3. Maker fill quality, markout and adverse selection.
4. Capital utilization and opportunity-adjusted ROI.
5. Reliability: reconciliation failures, stale-data trading attempts, and execution errors.

Observed opportunity count or hypothetical gross arbitrage is never a sufficient pass metric.

## 12. Risks, assumptions and open decisions

| ID | Issue | Required action |
|---|---|---|
| R-01 | Competition can remove apparent spreads before orders arrive | Stress execution delays and realistic queue share |
| R-02 | Maker bids can fill only on the wrong side | Test adverse-selection and one-sided expiry paths |
| R-03 | Fee or protocol-version changes can invert expected edge | Fetch fee and version per market; fail closed on uncertainty |
| R-04 | GTD constraints can exceed desired lifetime | Use GTC with explicit cancel plus reconciliation |
| R-05 | Oracle/market contract mismatch | Verify rule and source at discovery and rollover |
| R-06 | Binance data can be stale or diverge from Chainlink | Store separate reference types and latencies |
| R-07 | Historical L2 may be incomplete | Build recorder; label each backtest by coverage grade |
| R-08 | Paper fill assumptions may be unrealistic | Compare shadow and micro-live measurements before any scaling |
| R-09 | Settlement and fund availability may lag | Track cash, reserved cash and redeemable value separately |
| R-10 | Geographic or legal restrictions vary | Check in the actual execution environment, never bypass |

Open decisions for the implementer should be resolved through short Architecture Decision Records (ADRs), not silent guesses: authoritative historical L2 source; preferred encrypted secret storage; exact fair-value model and feature frequency; permissible venues and wallets; whether live trading should ever be activated; alert destinations.

## 13. Definition of done

A release is done only when its code, typed interface contracts, unit tests, failure tests, sample configuration, CLI examples, runbook and a reproducible report are committed. No phase may claim profit without correctly reconciled **filled** quantities, fees and cash flows. See `Architecture.md` for how modules enforce these rules and `tasks.md` for the ordered work queue.

## 14. Source of truth and API links (checked 2026-11-06)

- Official unified Python SDK: https://docs.polymarket.com/getting-started/python and https://github.com/Polymarket/py-sdk
- Official market-making guidance: https://docs.polymarket.com/trading/market-making
- Order types and expiry behavior: https://docs.polymarket.com/trading/place-orders
- Market, reference-price and Chainlink streams: https://docs.polymarket.com/market-data/realtime-data
- Fee schedule and formula: https://docs.polymarket.com/trading/fees
- Official settlement mechanics: https://docs.polymarket.com/concepts/resolution
- Geographic restrictions: https://docs.polymarket.com/api-reference/geoblock

If a documented API changes, prefer the current official documentation and installed SDK implementation. Add an ADR and regression test before changing strategy semantics.
