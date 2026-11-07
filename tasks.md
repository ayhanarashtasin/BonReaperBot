# Bone Reaper-Style Bot | Codex Implementation Tasks

**Version:** 1.0  
**Prepared:** 2026-11-06  
**Status:** P0-P6 complete with evidence in `reports/progress.md`. Readiness verdict: FAIL (`reports/readiness_memo.md`). P7 blocked and untouched. BR-801 and BR-802 not started.  
**Read first:** `PRD.md` (what to build) and `Architecture.md` (how to build).  
**Command to Codex:** Execute tasks in dependency order. Never skip required verification gates or enable live orders without a later explicit user request.

## 0. Codex operating protocol

1. Read all three files fully. `PRD.md` is the product and safety contract, `Architecture.md` is the engineering blueprint, and `tasks.md` is the execution queue. If these files conflict, choose the safer interpretation and document the discrepancy in an ADR.
2. Implement the **first incomplete task whose prerequisites are satisfied**. Keep task IDs unchanged. Work in small, reviewable commits with tests and a short changelog.
3. After each task: run formatter, linter, type checker and relevant tests; update its checkbox only when its acceptance evidence exists; write a one-paragraph result and unresolved risks to `reports/progress.md` (create this output only during implementation).
4. The default mode is **read_only**, not live. No task in Phase 0-5 authorizes live orders. A later user message and separate operational approval are required to activate any live pathway.
5. Don't invent Polymarket SDK methods or historical depth data. First inspect the pinned official SDK and live API docs. If an endpoint is unavailable, use an explicitly named adapter stub, capture the blocker, and continue independent tasks rather than silently substituting wrong data.
6. Tests and samples must run without real trading credentials. In standard CI, `real.py` cannot submit an order; opt-in metered integration tests are in a separately protected test suite.
7. A task marked `[x]` requires objective evidence, not that the code compiles. Provide exact test results, files affected and any assumptions. If a test fails, leave the checkbox unchecked.
8. Never describe paper PnL as actual earnings. Log simulated fills distinctly from real fills. Never treat maker rebates or incomplete pairs as locked profits.
9. Code generation must prefer deterministic, typed, single-purpose modules and should not overengineer a distributed system before the local process is correct.
10. Before transitioning phases, run the stated gate and present its output. Do not silently cross from research into risk-bearing production.

## 1. Phase overview and dependency gates

| Phase | Delivery | Must follow | Exit gate |
|---|---|---|---|
| P0 | Repo, SDK audit, compliance guards | None | G0 read-only safety |
| P1 | Market discovery and synchronized recorder | P0 | G1 recording and rollover |
| P2 | Complete-set opportunity scanner | P1 | G2 exact edge calculations |
| P3 | Event-driven replay and paper simulator | P2 | G3 deterministic simulation |
| P4 | Passive market making and inventory risk | P3 | G4 quote/hedge and fault scenarios |
| P5 | Fair-value research and out-of-sample tests | P3; ideally P4 | G5 calibration and economic evaluation |
| P6 | Operational hardening and dry-run review | P0-P5 | G6 resiliency and paper/shadow evidence |
| P7 | Optional micro-live preparation | P6 + explicit separate user authorization | G7 compliance, runbook, operator go/no-go |
| P8 | Optional ETH/15m expansion | P6 and new instrument validation | G8 full regression and research review |

The MVP consists of **P0-P3**. It is useful even if immediate arbitrage opportunities prove unprofitable. The first strategy potentially to trade passively is delivered in P4.

## 2. P0: Foundation, current API audit and legal safety

- [x] **BR-001 | Initialize repository**. Create `pyproject.toml`, package under `src/bone_reaper`, CLI entrypoint, `README.md`, `.env.example`, license choice, `.gitignore`, lockfile and formatting/testing config. Python >=3.11. **Depends:** none. **Accept:** fresh environment installs via documented command; `bone-reaper --help` works offline.
- [x] **BR-002 | Audit official SDK and pin version**. Verify official `polymarket-client` package, current version, `AsyncPublicClient`, market discovery models, stream specs, and optional `AsyncSecureClient`. Record SDK version and supported features as `reports/sdk_audit.md` with official URLs. Avoid `py-clob-client`. **Depends:** BR-001. **Accept:** typed read-only SDK smoke test and documented signatures.
- [x] **BR-003 | Build strict configuration parser**. Use Pydantic (or equivalent) and Decimal-valid YAML configs for read-only and paper modes; validate impossible limits and mutually incompatible flags. **Depends:** BR-001. **Accept:** invalid config exits nonzero, default never loads live adapter, configuration hash emitted.
- [x] **BR-004 | Implement typed domain models**. Add `EventEnvelope`, `MarketSpec`, `BookState`, `ReferenceState`, `ArbCandidate`, `QuoteIntent`, `FillRecord`, `RiskDecision`, `InventorySnapshot` and `ReplayManifest`. **Depends:** BR-001. **Accept:** schema roundtrip and Decimal/UTC/property tests.
- [x] **BR-005 | Add clock and UTC helpers**. Store source, UTC receive and monotonic arrival timestamps separately; detect unreasonable skew. **Depends:** BR-004. **Accept:** fake-clock tests cover late/reordered events and missing source timestamps.
- [x] **BR-006 | Implement geographic and approval gate**. Use official geoblock URL from actual execution IP in the future real execution environment. A blocked, unknown or failed check forbids opening real positions. Keep tests fully offline. **Depends:** BR-003. **Accept:** mock blocked/error states trigger zero sends; no VPN/proxy workaround logic.
- [x] **BR-007 | Enforce safe mode adapters**. Define separate `ReadOnlyTransport`, `PaperExecution`, `ShadowExecution`, and disabled real adapter factory. Read-only process must not construct an order-submission adapter. An optional TWAP-only authenticated facade must expose no order placement methods to strategy or OMS. **Depends:** BR-003, BR-006. **Accept:** spy exchange confirms zero trading calls in read-only/paper/shadow tests.
- [x] **BR-008 | Establish CI baseline**. Add `ruff`, `mypy` or Pyright, `pytest`, coverage checks, import sanity and secret scanning. **Depends:** BR-001, BR-004. **Accept:** offline CI success, zero credentials required, no metered tests executed.

**G0 exit checklist**

- [x] A fresh checkout boots `bone-reaper doctor` in read-only mode without a trading wallet.
- [x] CTF/V2 SDK audit and current venue docs are captured with version and links.
- [x] A blocked/unknown location cannot trigger real submissions even when a malicious config sets `live_enabled: true`.
- [x] Base unit tests, type check and lint pass.

## 3. P1: Market registry, L2/Chainlink streams and raw recording

- [x] **BR-101 | Public market discovery**. Discover active/upcoming BTC Up/Down 5m events using official SDK pagination; verify symbol, duration, expiry, trading status and outcome count. **Depends:** BR-002, BR-004. **Accept:** fixture includes valid market and irrelevant event, filtered correctly.
- [x] **BR-102 | Version-aware outcome mapping**. Map CTF `token_id` vs V2 `position_id` to canonical YES/NO asset IDs; store condition, event, rules hash and version. **Depends:** BR-101. **Accept:** no inverted pair, no guessed IDs, unknown version hard rejects.
- [x] **BR-103 | Rules, fee and constraint loading**. Fetch exact resolution instructions, fee flags/rates, tick, min order size, status and current market metadata. Revalidate on rollover. **Depends:** BR-101. **Accept:** missing/changed rule or unsupported negative risk causes market rejection.
- [x] **BR-104 | Polymarket market stream connector**. Subscribe to both outcomes, record `book`, `price_change`, `last_trade_price`, `tick_size_change` and supported lifecycle events with raw payload and timestamps. **Depends:** BR-002, BR-102. **Accept:** no trading credentials used for public market stream; fixture events parse and persist.
- [x] **BR-105 | L2 state machine and validation**. Build full book from snapshots and level-size updates; check tick, nonnegative sizes, bid/ask consistency, timestamp age and hash when available. **Depends:** BR-104. **Accept:** restored L2 state matches independent snapshot fixture; stale/disconnect -> INVALID.
- [x] **BR-106 | Chainlink TWAP connector**. Inspect official authenticated `CryptoTwapPriceSpec` availability and implement permitted streaming of `btcusd` 60-second TWAP with source timestamps, history snapshots, symbol and window metadata. **Depends:** BR-002, BR-004. **Accept:** mock snapshot/update normalize correctly; if auth unavailable, mark unavailable and keep scanner functioning without fair-value model.
- [x] **BR-107 | Binance adapter and historical-file discovery**. Parse user-provided aggTrade/candle/mark data with explicit spot/futures currency, timezone, symbol and quality checks. Implement optional live aggTrade/reference connector. **Depends:** BR-004. **Accept:** no Binance feed or file is mislabelled as Chainlink; gap/duplicate metrics exported.
- [x] **BR-108 | Append-only Parquet recorder**. Store raw envelopes, normalized book events and periodic checkpoints by source/date/market; write coverage and checksum manifests. **Depends:** BR-104, BR-105, BR-106. **Accept:** record -> stop -> replay -> identical book snapshots; no data silently overwritten.
- [x] **BR-109 | Market rollover controller**. Find next BTC 5m contract, validate new market rules, subscribe, retire old strategy, and continue old position/settlement tracking. **Depends:** BR-102, BR-105. **Accept:** fixture of 3 sequential markets produces zero stale-asset reuse.
- [x] **BR-110 | Data diagnostics CLI**. Build `markets`, `record`, `doctor`, and `health`; show snapshot freshness, coverage, rule/version/fee source and gaps. **Depends:** BR-108, BR-109. **Accept:** one-command BTC 5m read-only recording instructions and machine-readable health output.

**G1 exit checklist**

- [x] Full event capture plus deterministic book replay works across market rollover.
- [x] CTF and V2 mapping fixtures pass; missed data is marked invalid and recovered by resnapshot.
- [x] TWAP origin and market price-to-beat are distinguishable from Binance spot/futures.
- [x] Source coverage and recording gaps are disclosed in output.

## 4. P2: Fee-aware paired-arbitrage scanner (no order execution)

- [x] **BR-201 | Exact market fee module**. Use market-level fee availability, crypto curve, precision and fee asset handling. Taker BUY fee can result in fewer shares; maker rebates never pre-count as earned. **Depends:** BR-103, BR-004. **Accept:** fee curve and rounding fixtures match documented examples and adapter response behavior.
- [x] **BR-202 | Depth-integrating execution calculator**. For desired net shares, walk each leg's asks, compute gross order sizes, VWAP, cash requirement and resulting net shares with rounding. **Depends:** BR-105, BR-201. **Accept:** multi-level depth and partial-liquidity fixtures; reject if either leg lacks required net shares.
- [x] **BR-203 | Paired payout and opportunity classifier**. Calculate locked payout for matched net shares only; subtract real cash and explicit costs; distinguish gross, indicative, feasible-size and confirmed-locked edge. **Depends:** BR-202. **Accept:** 0.46/0.51 fee-negative crypto example is *not* signalled as positive net arbitrage.
- [x] **BR-204 | Configure risk-aware scanner thresholds**. Gate on absolute edge, ROI, book age, lot, price tick, expiry, market status and false-positive flags. **Depends:** BR-203, BR-003. **Accept:** specific reason codes and reproducible threshold tests.
- [x] **BR-205 | Opportunity event store and report**. Save all candidates and rejections with price levels, age, trade-size buckets, opportunity duration and versioned fee assumptions. **Depends:** BR-204, BR-108. **Accept:** `bone-reaper scan` exports machine-readable records and summary statistics.
- [x] **BR-206 | Sensitivity diagnostics**. Report edge under delayed execution, higher hedge price, incomplete second leg and no rebates; do not describe both API requests as atomic. **Depends:** BR-205. **Accept:** each candidate contains at least one adverse case and funding requirement.

**G2 exit checklist**

- [x] No signal is based on midprice-only YES+NO, assumed infinite top-level liquidity or identical gross/net shares.
- [x] Share-denominated fees, price ticks, rounding and one-side-fails tests all pass.
- [x] The scanner demonstrably cannot transmit orders.

## 5. P3: Deterministic event replay, fill simulator and accounting

- [x] **BR-301 | Replay event clock and snapshot loader**. Load raw event manifests, reconstruct visible market state using arrival time and reproduce multiple market rolls. **Depends:** BR-108, BR-205. **Accept:** same seed/manifest produces identical signal list and checksum.
- [x] **BR-302 | Depth-aware taker matching simulator**. Apply simulated network delays, consume depth at venue-arrival time, partial fills, fees and account reservation constraints. **Depends:** BR-202, BR-301. **Accept:** a stale visible ask cannot fill at a price that vanished before arrival.
- [x] **BR-303 | Conservative maker queue estimator**. Queue behind resting shares at level, model trade depletion plus cancels only with evidence, bracket unknown queue priority, never fill on price-touch alone. **Depends:** BR-301. **Accept:** touch-only -> no-fill test, fill-through and pessimistic scenario tests.
- [x] **BR-304 | Paper OMS and order lifecycle**. Add intent journal, accepted/open/partial/filled/cancel-pending/canceled/rejected/unknown states and deterministic fake exchange. **Depends:** BR-004, BR-302, BR-303. **Accept:** batch legs independently handled; unknown status does not duplicate send.
- [x] **BR-305 | Double-entry paper ledger**. Track collateral, reservations, gross/net outcome shares, fee units, paired/unpaired quantities, unrealized/realized PnL and settlement status. **Depends:** BR-304. **Accept:** conservation tests and no double-count of fills.
- [x] **BR-306 | Resolution, merge and redemption simulation**. Model time to resolution, complete-set merge where protocol permits and winning-share redemption. Separate redeemable from spendable balance. **Depends:** BR-305. **Accept:** one complete pair settles to exactly one payout unit before explicit costs; incomplete share does not.
- [x] **BR-307 | Reconciliation engine**. Build fake exchange REST/account snapshot and compare positions, open orders, trades and balances; reject new risk until unknowns resolved. **Depends:** BR-304, BR-305. **Accept:** fill-before-ACK, unknown cancel, duplicate trade and process restart tests.
- [x] **BR-308 | Research metrics and export**. Generate equity/cash/position timelines, fees, simulated fill probabilities, drawdown, exposure duration, latency and data coverage. **Depends:** BR-306, BR-307. **Accept:** reproducibility manifest with git/config/data hashes and assumptions.
- [x] **BR-309 | Sensitivity and walk-forward harness**. Experiment with pessimistic/central/optimistic queue models, 2x latency, no rebates, late hedges and chronological held-out market windows. **Depends:** BR-308. **Accept:** report clearly separates train, validation and untouched test partitions.

**G3 exit checklist**

- [x] Backtester is deterministic and causal at simulated bot arrival time.
- [x] Paper ledger balances and every simulated fill has a unique identity and explicit fee treatment.
- [x] A maker order touched by market price does not automatically fill.
- [x] Backtest reports clearly identify unverified queue assumptions and imperfect data.

## 6. P4: Passive dual-outcome quoting and active risk simulation

- [x] **BR-401 | Quote price generator**. Implement one YES and one NO post-only BID per verified market with tick rounding, target pair buffer, fair-price bounds and market constraints. **Depends:** BR-304, BR-305, BR-204. **Accept:** neither quote crosses its own outcome ask; both orders reserve worst-case cash.
- [x] **BR-402 | Quote refresh controller**. Add minimum tick drift, minimum age, cancel/reconcile-before-replace, and old order worst-case exposure reservation. **Depends:** BR-401. **Accept:** no cancel storm on trivial tick changes; race-safe replacement.
- [x] **BR-403 | Inventory-aware sizing and skew**. Limit paired and directional shares based on cash, outstanding orders, market exposure, imbalance and expiry. **Depends:** BR-401, BR-305. **Accept:** opening either quote cannot breach worst-case capital/exposure limits even if other side fills concurrently.
- [x] **BR-404 | Counterpart hedge controller**. Implement passive counterpart first, then separately authorized fee-aware taker risk reduction, then safe stop. Negative-edge hedge cannot be mistaken for a profitable arbitrage entry. **Depends:** BR-403, BR-302. **Accept:** one-side fill + violent move scenario records actual loss and respects hedge limit.
- [x] **BR-405 | Risk kill switch and exposure budget**. Halt new orders for stale feed, unknown order, loss cap, recon mismatch, clock skew or near expiry; attempt only permitted cancellation/risk reduction. **Depends:** BR-404, BR-307. **Accept:** inject every fault and observe zero new risk submissions.
- [x] **BR-406 | Paper strategy comparison**. Compare scanner-only, passive maker and constrained hedge configurations by net PnL, markout, unpaired risk and capital at risk. **Depends:** BR-405, BR-309. **Accept:** written experiment report with at least conservative and adverse fill assumptions.

**G4 exit checklist**

- [x] No account can exceed reserved-cash or worst-case inventory limits under one-sided fills.
- [x] Fees and losses of risk-reducing hedges are visible and never disguised as locked edge.
- [x] Kill-switch tests, cancel races and expiry cutoffs pass.

## 7. P5: Probability model and quantitative research

- [x] **BR-501 | Feature definitions and provenance**. Use official price-to-beat/TWAP distance, remaining time, Polymarket L2 and optionally Binance aggressor imbalance, volatility and reference basis with causality timestamps. **Depends:** BR-106, BR-107, BR-301. **Accept:** no future/oracle-close data leaks into pre-expiry features.
- [x] **BR-502 | Transparent benchmark model**. Fit or specify interpretable baseline; document label, horizon and treatment of missing TWAP. **Depends:** BR-501. **Accept:** validation Brier/log loss and reliability plot produced.
- [x] **BR-503 | Optional supervised model**. Implement time-split logistic/gradient-boosting training with strict market-level split and embargo for overlapping windows. **Depends:** BR-502. **Accept:** only improves if out-of-sample calibration and decision metrics beat baseline.
- [x] **BR-504 | Fair-value quote integration**. Constrain quote price/size using validated `p_up` with fallbacks on stale/model errors and versioned model hash. **Depends:** BR-503, BR-401. **Accept:** cannot change live quote behavior without explicit model-enable flag.
- [x] **BR-505 | Economic model audit**. Compare maker economics, adverse selection, net PnL and drawdown across baseline/model variants, including zero rebates and delayed TWAP. **Depends:** BR-504, BR-406. **Accept:** quantitative go/no-go memo; if worse than baseline, leave model disabled.

**G5 exit checklist**

- [x] Market-level train/test leakage tests pass and held-out Brier/calibration are published.
- [x] An improved probability forecast is not called a profitable strategy unless after-cost execution improves.
- [x] Model fallback never produces unbounded/NaN quotes or exposure.

## 8. P6: Operational hardening, shadow observation, full documentation

- [x] **BR-601 | Durable single-writer transactional store**. Implement Postgres schema, migrations, wallet advisory lock, atomic intent/reservation/fill transactions and unique identifiers. **Depends:** BR-305, BR-307. **Accept:** two concurrent bot processes cannot both claim wallet writer ownership.
- [x] **BR-602 | Fault-injection and chaos suite**. Exercise websocket gap, book inconsistency, clock skew, disk full, DB down, unknown ACK, cancel/fill races, rate limit, API outage, rollover and matching-engine restart. **Depends:** BR-405, BR-601. **Accept:** all risk-increasing paths freeze closed; clear recovery rules.
- [x] **BR-603 | Metrics and alerts**. Implement structured logs, read-only health CLI and machine-readable statistics for latency, gaps, queue backlog, PnL, open/unknown orders, mismatches and kill-switch state. **Depends:** BR-308, BR-601. **Accept:** deterministic alert tests for all critical failures.
- [x] **BR-604 | Shadow mode**. Feed live books to paper order intents and save hypothetical execution with explicit uncertainty and source timestamps, but no network submission. **Depends:** BR-405, BR-603. **Accept:** packet capture/spied adapter proves zero real sends.
- [x] **BR-605 | Operator runbook**. Document setup, read-only start, recording, backtest, shadow start, stop, emergency actions, recover/reconcile, data restoration and report interpretation. **Depends:** BR-604. **Accept:** unrelated developer can follow it without privileged credentials.
- [x] **BR-606 | Research review / readiness memo**. Publish evidence for data coverage, conservative net PnL, queue/latency sensitivity, exposure and failure tests. **Depends:** BR-309, BR-505, BR-605. **Accept:** explicit PASS, FAIL or INCONCLUSIVE; never infer a pass from missing data.

**G6 exit checklist**

- [x] Safe startup, restart, shutdown and kill-switch behavior demonstrated by tests and tabletop drills.
- [x] Offline, read-only, paper and shadow all work as documented; none can submit real orders.
- [x] No unexplained accounting differences remain in the test corpus.
- [x] Net economics are supported by held-out conservative simulation or classified honestly as unproven.

## 9. P7: Conditional live adapter preparation (DO NOT ENABLE AUTOMATICALLY)

These tasks are **blocked** until a separate, explicit user request authorizes preparation for actual real orders, local legal/geographic eligibility is confirmed, and G6 is passed. Writing or checking these boxes does **not** by itself authorize live trading.

- [ ] **BR-701 | Authorized secure client and key hygiene**. Scope a dedicated small-balance wallet, secure credentials, allowances/approvals and account isolation. **Depends:** G6 + explicit authorization. **Accept:** no secrets in source/logs and restricted integration environment.
- [ ] **BR-702 | Real execution adapter**. Map approved OMS intents to official `AsyncSecureClient` order APIs with verified market version, post-only, fee handling and response state. **Depends:** BR-701. **Accept:** full offline mock exchange integration; tests cannot send by default.
- [ ] **BR-703 | Authenticated user stream and reconciliation**. Listen to account-wide orders/trades, reconcile with authoritative REST and chain state after any disconnect or unknown response. **Depends:** BR-702, BR-601. **Accept:** duplicate/late/missing events never double-count or free funds early.
- [ ] **BR-704 | Safety gate and live feature lock**. Require `mode=live`, explicit config flag, separate human approval, eligible location, fresh account reconciliation and tiny limits. Any failure returns `LIVE_DISABLED`. **Depends:** BR-703, BR-006. **Accept:** comprehensive prohibited-path test suite, CI can never activate it.
- [ ] **BR-705 | Supervised micro-live verification plan**. Define manual stop, capital/loss caps, expected fill-quality audit, order/cancel/settlement tracking and a post-run comparison to paper assumptions. **Depends:** BR-704. **Accept:** human-approved go/no-go plan; do not auto-run or scale.

**G7 exit checklist**

- [ ] Only permitted eligible operators can authorize any real trades.
- [ ] All preceding research and execution safety checks have passed.
- [ ] Live monitoring, emergency stop, reconciliation and controlled rollback are validated.
- [ ] Any real trading remains a new, separate and explicit operational decision.

## 10. P8: Optional market expansion and performance tuning

- [ ] **BR-801 | Add ETH 5-minute support**. Validate market-specific rules, TWAP symbol, metadata and data coverage; do not inherit BTC model calibration automatically. **Depends:** G6. **Accept:** independent ETH 5m backtest and tests.
- [ ] **BR-802 | Add BTC/ETH 15-minute support**. Verify exact duration, start/end strike, reference and tick/fee rules. **Depends:** BR-801. **Accept:** independent rollover fixtures and causal evaluation.
- [x] **BR-803 | Profile performance**. Measure p50/p95/p99 end-to-end decision and ACK times, CPU/memory, queue latency, disk and network bottlenecks. **Depends:** BR-602. **Accept:** profile-supported optimization plan, not speculative rewrite.
- [x] **BR-804 | Optimize only proven bottlenecks**. Consider batching, partitioning, lower-allocation book state or compiled hot-path implementation without losing risk invariants. **Depends:** BR-803. **Accept:** same deterministic strategy/PnL tests as original with measured performance gain.

**G8 exit checklist**

- [ ] Every new market and protocol version passes instrument-specific rules, fee and resolution checks.
- [x] Faster execution does not break accounting or risk invariants.

## 11. Traceability from PRD functional requirements

| PRD ID | Primary tasks |
|---|---|
| FR-01 | BR-101, BR-109 |
| FR-02 | BR-102, BR-103 |
| FR-03 | BR-103, BR-106 |
| FR-04 | BR-104, BR-105, BR-108 |
| FR-05 | BR-105, BR-602 |
| FR-06 | BR-106, BR-107 |
| FR-07 | BR-201, BR-202, BR-203 |
| FR-08 | BR-204, BR-205, BR-206 |
| FR-09 | BR-301, BR-308, BR-309 |
| FR-10 | BR-303, BR-304 |
| FR-11 | BR-304, BR-307, BR-601, BR-703 |
| FR-12 | BR-305, BR-306, BR-307 |
| FR-13 | BR-006, BR-405, BR-704 |
| FR-14 | BR-007, BR-304, BR-604 |
| FR-15 | BR-401, BR-402, BR-403, BR-404 |
| FR-16 | BR-501, BR-502, BR-503, BR-504, BR-505 |
| FR-17 | BR-702, BR-703 |
| FR-18 | BR-006, BR-007, BR-704 |
| FR-19 | BR-405, BR-602, BR-603 |
| FR-20 | BR-108, BR-205, BR-308, BR-606 |

## 12. Minimum test matrix and commands

These are **target commands** to support once repository bootstrap has been implemented:

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/unit tests/property tests/fault -q
uv run bone-reaper doctor --config config/read_only.yaml
uv run bone-reaper markets --symbol BTC --duration 5
uv run bone-reaper scan --input tests/fixtures/replay --config config/read_only.yaml --report reports/scan.json
uv run bone-reaper replay --input tests/fixtures/replay --config config/paper.yaml --output reports/replay
```

Before ticking any gate, use fixed fixtures to prove these six economically important cases:

1. A 0.46 YES ask + 0.51 NO ask creates nominal 0.03 gross spread but is **negative after current illustrative crypto taker fees**.
2. A YES-only maker fill creates directional risk and zero locked-pair PnL, regardless of the other order still being open.
3. Fee-in-shares rounds actual YES and NO holdings differently, so unpaired shares are explicitly recorded.
4. A historical maker order with limit-price touch but insufficient queue depletion does **not** fill.
5. Cancel acknowledgment arrives after a partial fill; the fill is accounted once, and reservation is released only when safe.
6. Wrong market version, expired market, stale TWAP or blocked geographic eligibility results in zero new real orders.

## 13. Completion report template

After each completed task, append a compact log with:

```markdown
### BR-XXX | Title
- Status: PASS / FAIL / BLOCKED
- Date (UTC): YYYY-MM-DD
- Changed files: ...
- Checks run: ...
- Evidence / results: ...
- Known assumptions: ...
- Risks or blockers: ...
- Next recommended task: BR-YYY
```

**Never mark tasks complete solely because Codex generated files.** Evidence should be reproducible from a fresh checkout or a recorded versioned dataset. If a task cannot be verified due to unavailable real data, leave it blocked or marked partial in the progress log, not `[x]`.

## 14. Initial Codex prompt

Paste this after placing all three documents into the root of the target project:

> Read `PRD.md`, `Architecture.md`, and `tasks.md` in that order. Build this project by working through task IDs in dependency order, starting with BR-001. Implement only P0 and P1 initially. Use the current official `polymarket-client` Python SDK, pin the verified version, and check real API signatures before coding connectors. Default to `read_only`. No live orders, wallet funding, or unapproved network trading. Write typed, well-tested Python, maintain explicit money/share arithmetic, and keep `tasks.md` checkboxes in sync with actual acceptance evidence. After each task update `reports/progress.md` with tests and blockers. Do not infer market maker fills from price touches or treat Binance data as Polymarket's Chainlink settlement source. Stop at G1 and report the readiness gaps before starting P2.

## 15. Maintainer notes

These tasks are intentionally ordered for evidence of **executable net economics**, not trading speed. Even if a scanner finds no profitable taker/taker arbitrage, it remains valuable for pricing, dataset validation and the safer evaluation of passive market-making hypotheses. Further market additions, more complex ML and optimization are contingent on reliable accounting and realistic historical L2 observations.
