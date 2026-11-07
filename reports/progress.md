# Progress log

Evidence for `tasks.md`. All work dated 2026-11-07 (UTC, some on 2026-11-06). Final state: 525 tests passing, 91%
coverage, `ruff` and `mypy --strict` clean, secret scan clean, verified from a clean clone.

Test files are named by phase: `tests/unit/test_p*`, `tests/property/test_invariants.py`,
`tests/fault/test_fault_injection.py`, `tests/integration/`. Each task below was checked with those plus the
evidence listed.

## P0: Foundation (G0 PASS)

| Task | Status | Evidence |
|---|---|---|
| BR-001 Repository | PASS | Clean clone: `uv sync --frozen`, `bone-reaper --help` offline |
| BR-002 SDK audit | PASS | `reports/sdk_audit.md`, `scripts/smoke_read_only.py` run against the live API |
| BR-003 Config | PASS | Invalid configs exit 2; floats, unknown keys and impossible limits rejected; hash printed |
| BR-004 Domain models | PASS | JSON roundtrip and invariant tests |
| BR-005 Clock | PASS | Fake-clock tests for reordering, missing source time and skew |
| BR-006 Geoblock gate | PASS | Mocked blocked, unknown and failed states; no proxy logic; `trust_env=False` |
| BR-007 Safe adapters | PASS | Read-only builds no adapter; spy and network guard prove zero sends |
| BR-008 CI baseline | PASS | `.github/workflows/ci.yml`, no secrets, no metered tests. **Workflow never run on GitHub**: no remote exists |

## P1: Market data (G1 PASS)

| Task | Status | Evidence |
|---|---|---|
| BR-101 Discovery | PASS | Real captured events; irrelevant and expired events filtered |
| BR-102 Version mapping | PASS | CTF and V2 fixtures; inverted outcome order safe; unknown version rejected. V2 fixture is derived: live BTC 5m markets are v1 |
| BR-103 Rules and fees | PASS | Changed rules, missing fees and neg-risk rejected |
| BR-104 Market stream | PASS | 5.6M real events, no credentials. Payload normalized rather than raw (ADR-0004) |
| BR-105 L2 state | PASS | 99.98% top-of-book against 148,034 stream snapshots |
| BR-106 TWAP | PASS | 9,604 real points from public RTDS. **Authenticated PolyBolt route tested with mocks only** |
| BR-107 Binance | PASS | Parsers, gap and duplicate metrics, live feed. No user Binance dataset was supplied, so files tested are fixtures |
| BR-108 Recorder | PASS | Record, stop, replay gives identical books; no overwrite; checksums verified |
| BR-109 Rollover | PASS | 37 sequential real markets, no stale-asset reuse |
| BR-110 CLI | PASS | `markets`, `record`, `doctor`, `health`, `coverage` |

## P2: Scanner (G2 PASS)

| Task | Status | Evidence |
|---|---|---|
| BR-201 Fees | PASS | Documented curve and 5-decimal rounding; both collection methods. Real collection unverified (ADR-0002) |
| BR-202 Depth | PASS | Multi-level and partial liquidity; property tests |
| BR-203 Classifier | PASS | 0.46/0.51 case is negative; matched net shares only |
| BR-204 Thresholds | PASS | Reason codes and reproducible threshold tests |
| BR-205 Reports | PASS | `reports/scan_real/scan.json`: 51,140 evaluations |
| BR-206 Sensitivity | PASS | Every complete candidate has adverse cases and funding |

## P3: Simulator (G3 PASS)

| Task | Status | Evidence |
|---|---|---|
| BR-301 Replay clock | PASS | Byte-identical metrics; truncation test proves later data cannot change earlier orders |
| BR-302 Taker sim | PASS | A vanished ask cannot fill; partial fills; FOK and FAK |
| BR-303 Maker queue | PASS | Touch without depletion does not fill; through-fills; three scenarios |
| BR-304 Paper OMS | PASS | Full state machine; batch legs independent; unknown never re-sent |
| BR-305 Ledger | PASS | Conservation property tests; 0 errors in all real runs |
| BR-306 Settlement | PASS | One pair pays one unit; merge cannot double count |
| BR-307 Reconciliation | PASS | Fill before ACK, unknown cancel, duplicate trade, restart; 37 of 37 clean |
| BR-308 Metrics | PASS | Manifests with commit, config and data hashes and assumptions |
| BR-309 Walk-forward | PASS | `reports/compare/`: train, validation, untouched test partitions |

## P4: Passive quoting (G4 PASS)

| Task | Status | Evidence |
|---|---|---|
| BR-401 Quotes | PASS | Property test: never crosses, always on tick, within bounds |
| BR-402 Refresh | PASS | No cancel on one-tick noise; cancel confirmed before replace |
| BR-403 Sizing | PASS | Worst-case limits hold if the other side fills |
| BR-404 Hedging | PASS | Violent-move scenario records the real loss; budget never exceeded |
| BR-405 Kill switch | PASS | Every fault injected, zero new risk sends |
| BR-406 Comparison | PASS | `reports/compare/compare.md`: all strategies negative or inactive |

## P5: Fair value (G5 PASS as a process; economics negative)

| Task | Status | Evidence |
|---|---|---|
| BR-501 Features | PASS | Rewriting the future leaves past features unchanged |
| BR-502 Baseline | PASS | Brier 0.1443 on test; reliability plots |
| BR-503 Supervised | PASS | Did not beat baseline out of sample, so not adopted |
| BR-504 Quote integration | PASS | No change without the flag; falls back on failure; hash recorded |
| BR-505 Economic audit | PASS | `reports/model/model_audit.json`: NO-GO, model stays disabled |

G5 passes because the evaluation is sound. The strategy itself fails, which is reported as such.

## P6: Operations (G6 PASS, net economics classified negative)

| Task | Status | Evidence |
|---|---|---|
| BR-601 Postgres | PASS | Real PostgreSQL 16 and a real second process. **Paper and replay still use the in-memory ledger**; the store is not wired into a running session |
| BR-602 Fault suite | PASS | All listed faults, including matching-engine restart per venue docs |
| BR-603 Metrics and alerts | PASS | Deterministic rule tests; alerts in `health` |
| BR-604 Shadow | PASS | 11 minutes live: 189 hypothetical orders, 0 real sends |
| BR-605 Runbook | PASS | `docs/runbook.md`, commands run from a clean clone. **Not yet followed by another person** |
| BR-606 Readiness memo | PASS | `reports/readiness_memo.md`: FAIL verdict |

G6 "tabletop drills" were run as automated fault tests, not as a live exercise with an operator.

## P7: Live adapter: BLOCKED, not started

Requires separate authorization, confirmed eligibility and a passed readiness review. The review failed. For BR-702:
implement the venue's restart handling (honour the server's retry delay, post-only for two minutes after a restart),
and confirm how BUY fees are really collected.

## P8: Expansion: partial

| Task | Status | Evidence |
|---|---|---|
| BR-801 ETH 5m | NOT STARTED | Needs its own recorded data and rule review. Enabling it by name is refused |
| BR-802 15m markets | NOT STARTED | As above |
| BR-803 Profile | PASS | `reports/performance.json`: 22,700 events/s, decisions p50 0.32 ms, p99 2.65 ms. Real ACK times not measurable |
| BR-804 Optimize | PASS | 5,426 to 16,836 events/s on the same 150,000 events, with an identical metrics checksum |

## Defects found by running on real data

Fixtures alone would not have shown any of these.

| Defect | Found by | Effect if missed |
|---|---|---|
| Trades send no `price_change` | Recorder health | 31,000 false invalidations; REST hammered |
| Tick narrows mid-market | Recorder health, scan report | False gaps; valid books rejected |
| `new_market` events attributed to the wrong market | Event counts | Polluted market data |
| Window start is not `start_date` | SDK audit | Wrong strike time and duration |
| Rollover events consumed before use | `markets_resolved` stuck at 0 | No settlement data recorded |
| Outcome recording waited for `finalPrice` | Live data | Late or missing resolutions |
| Replay loaded all events into memory | RAM down to 0.9 GB | Could exhaust the machine |
| Non-terminating fee and queue arithmetic | Property tests, ledger guard | Inexact balances; crash in central scenario |
| Daily-loss baseline reset on first use | Unit test | A restart would disarm the stop |
| Empty book marked positions at zero | $99.99 drawdown in the report | False risk numbers |
| Model freshness used venue time against the local clock | Audit source counts | Model unused in over 99% of decisions; false `CANDIDATE` |
| `CANDIDATE` awarded to a loss-making variant | Same review | A smaller loss read as a go signal |

## Known limits

- 34 resolved markets from one evening. See the memo before drawing conclusions.
- No real fill, ACK, rebate or fee collection has been observed.
- Public TWAP shuts down 2026-11-30 (ADR-0008).
- This machine's clock runs about 800 ms fast (ADR-0007).
- Paper and shadow details in `reports/paper` and `reports/shadow` are local only, not in git.

## After the readiness memo: dashboard

Added `bone-reaper dashboard` at the operator's request (ADR-0015). Building it found three defects, all now fixed with
regression tests.

| Defect | Found by | Effect if missed |
|---|---|---|
| Live sessions took an equity sample per event, not every 5 s | Sample count equal to event count; 280 hours "sampled" in 11 minutes | Memory grows with every event; exposure-time figures in `reports/paper` and `reports/shadow` are overstated |
| Replacing `health.json` fails on Windows while a reader has it open | Reviewing the write path before polling it | A dashboard or `health` read could stop a recording |
| Ctrl+C on a paper or shadow session lost the final report | Reviewing the shutdown path | The documented way to stop left no report |

Checks after these changes: full test suite passing at 92% coverage, `ruff` and `mypy --strict` clean, secret scan clean.
The full replay of the 5.6M events gives the same metrics checksum as before (`c3db45c0...`), so the sampler fix does
not change any research result. A 90-second live paper session took 18 equity samples and reported `clean: true`.
Exposure-time fields in the older `reports/paper` and `reports/shadow` metrics are overstated by the sampler defect, so
the dashboard does not display them.
