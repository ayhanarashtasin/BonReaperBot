# Readiness memo (BR-606)

**Date:** 2026-11-07
**Verdict: FAIL. Do not proceed to live preparation (P7) on this evidence.**

| Question | Verdict |
|---|---|
| Is the platform built and safe to run in read-only, paper and shadow modes? | **PASS** |
| Can it place a real order? | **No.** Every route returns `LIVE_DISABLED` |
| Is there an executable pair-arbitrage edge? | **FAIL:** none observed |
| Is the passive maker strategy, as configured, profitable? | **FAIL:** negative in every scenario tested |
| Does the fair-value model make it profitable? | **FAIL (NO-GO):** reduces losses, still negative |
| Could a different design work? | **INCONCLUSIVE:** not tested, and this sample is too small to tune on |

All PnL below is **simulated**. It is not earnings. It depends on queue and latency assumptions that have
never been checked against a real fill.

## The data behind this

| Item | Value |
|---|---|
| Recorded | 5,623,846 events, 2026-11-06, about 2.9 hours, one continuous session |
| Markets | 37 BTC 5-minute markets: 34 fully captured and officially resolved |
| Forecast set | 2,500 closed markets, about 8.8 days (2026-10-28 to 2026-11-06) |
| Market data gaps | 0 |
| Other gaps | 4 (2 discovery retries, 1 Binance reconnect, 1 recorder start) |
| Coverage grade | B: continuous capture, conclusions still need out-of-sample review |

**The main limitation is sample size.** 34 markets from one evening is one market regime. The negative
results are consistent enough to act on, but they cannot be generalized, and the untouched test partition
holds only 4 settled markets.

## Finding 1: no executable pair arbitrage

| Scanner result | Count |
|---|---|
| Evaluations across 5 sizes (5 to 100 shares) | 51,140 |
| Passed all checks | **0** |
| Positive gross edge | 20 |
| Of those, on two fresh books | **0** |

All 20 positive readings had one book 2.4 to 29.5 seconds stale, so the second price no longer existed. The
largest would have looked like a $4.77 profit without the freshness gate. The taker route made **zero** trades
across the whole dataset, including at twice the latency.

## Finding 2: passive quoting loses in every scenario

With the daily stop lifted so that all 34 markets are evaluated (order and exposure limits unchanged):

| Scenario | Realized | Mean per market | 95% interval | Win / loss |
|---|---:|---:|---|---|
| Optimistic queue | -$77.77 | -$2.29 | -3.50 to -1.07 | 6 / 27 |
| Central queue | -$91.89 | -$2.70 | -3.80 to -1.60 | 3 / 30 |
| Pessimistic queue | -$108.76 | -$3.20 | -4.39 to -2.01 | 4 / 29 |
| Central, 2x latency | -$102.13 | -$3.00 | -3.92 to -2.09 | 5 / 28 |
| Pessimistic, 2x latency | -$82.11 | -$2.42 | -3.54 to -1.29 | 8 / 25 |
| Central, 1.5x fees | -$88.90 | -$2.61 | -3.70 to -1.53 | 7 / 27 |
| Central, later hedge | -$97.25 | -$2.86 | -3.72 to -2.00 | 2 / 30 |
| Central, authorized $2 loss-making hedges | -$79.88 | -$2.35 | -3.35 to -1.35 | 5 / 28 |

Why it loses:

- **One-sided fills.** 81 to 92% of traded markets closed with unpaired inventory. The two quotes rarely both
  fill, so the position is a directional bet rather than a locked pair.
- **Adverse selection.** Five seconds after a fill, the price had moved 2.2 to 3.4 cents per share against the
  position on average. Bids get hit when the market is falling through them.
- **Latency is not the deciding factor.** Doubling it did not change the sign. One doubled scenario even lost
  less than its baseline, which shows how much of the gap between scenarios is noise in a sample this size.

Held-out partitions agree: validation lost $3.86 to $4.27 per market (6 of 6 losing) and the untouched test
lost $2.24 to $2.68 per market (4 of 4 losing), under both queue assumptions.

With the stop on, as configured, the **$10 daily loss limit latched after 7 to 8 traded markets**. The safety
control behaved as designed, and at these limits the strategy cannot get through a session.

## Finding 3: no forecast edge over the market

On the untouched test partition, at the same moments (1,184 rows from 300 markets; lower is better):

| Forecast | Brier | Log loss |
|---|---:|---:|
| **Market's own price** | **0.1268** | **0.3873** |
| Random-walk baseline | 0.1422 | 0.4304 |
| Logistic model | 0.1422 | 0.4311 |

Across the wider test set the logistic model does not reliably beat the simple baseline: it was slightly better
on test (0.1435 against 0.1443) and slightly worse on validation (0.1591 against 0.1585), so it was not adopted.
Features use Binance 1-minute closes as a labelled proxy, so this is not the last word, but a model would have to
overcome a clear deficit to the market price. Reliability plots are in `reports/model/`.

## Finding 4: using the model loses less, still loses

Replay over the same 34 markets, with the model in 88 to 93% of decisions:

| Variant | Pessimistic | Central | Fills (pess.) |
|---|---:|---:|---:|
| Midpoint baseline | -$108.76 | -$91.89 | 506 |
| Random-walk model | -$3.34 | -$6.10 | 268 |
| Logistic model | -$20.74 | -$17.52 | 276 |
| Logistic, TWAP delayed 5s | -$30.79 | -$15.92 | 329 |

**Audit decision: NO-GO. The model stays disabled.** Do not read this as a near-breakeven strategy. The variants
fill about half as often, so part of the improvement is simply taking less risk. They leave $20 to $28 of cost in
markets with no resolution in the data, excluded from these totals. The random-walk result is the most promising
lead here, and a 34-market sample cannot tell whether it is real.

## Engineering evidence

| Check | Result |
|---|---|
| Book rebuild against stream snapshots | 148,002 of 148,034 top-of-book (99.98%), 147,479 full depth (99.6%) |
| Book rebuild against REST samples | 653 of 678 top-of-book (96.3%); the samples are not simultaneous |
| Book invalidations on the corrected feed model | 0 |
| Ledger conservation errors, all runs | 0 |
| Reconciliation | 37 of 37 clean in replay; cash and position differences 0 |
| Unknown orders or orphan fills remaining | 0 |
| Causality violations | 0 |
| Reproducibility | Byte-identical metrics from a clean clone |
| Shadow on the live feed, 11 minutes | 189 hypothetical orders, 25 hypothetical fills, 0 real sends, clean shutdown |
| Paper on the live feed, 11 minutes | 67 simulated orders, 8 simulated fills, reconciled clean |
| Decision latency | p50 0.32 ms, p99 2.65 ms |
| Simulator throughput | 22,700 events/s |
| Tests | 525 passing, 91% coverage, `ruff` and `mypy --strict` clean |

The paper session reported `clean: false`, because one order was still in flight at stop. That reporting was
correct. The cause, cancelling before the ACK landed, was then fixed and tested. The session was not repeated.

## What this does not establish

- **Real fillability.** No real order has ever been sent. Queue position is estimated, not observed.
- **Real latency.** Submit, ACK and cancel timing are configured assumptions.
- **How BUY fees are really collected** (ADR-0002). The conservative assumption is used.
- **Real rebates.** Excluded. A market maker earns them, so they are a known omission. On these results they
  would need to cover about $2 to $3 per market on roughly $5 to $10 quotes, which is not plausible, but this is
  an inference and has not been measured.
- **Other market regimes, times of day or parameter settings.** Only one quote configuration was run.

## Recommendation

1. **Do not start P7.** Its gate requires positive conservative results on a chronological holdout, and these are
   negative. This is not a close call on the current design.
2. **If research continues,** collect weeks of data before changing anything. Tuning on 34 markets would only fit
   noise. Then test whether quoting only when a fair-value signal disagrees with the book holds up on fresh,
   untouched data.
3. **Set up the TWAP replacement before 2026-11-30.** The public RTDS feed used here shuts down that day. After it,
   recording has no TWAP without authenticated PolyBolt access, which needs credentials and your decision.
4. **Fix this machine's clock.** It runs about 800 ms ahead. Replay corrects for it, but it sits near the 1,000 ms
   skew limit, and that limit would block real orders.

## Reproduce

```bash
uv run bone-reaper coverage --input data/raw
```

```bash
uv run bone-reaper scan --input data/raw --config config/read_only.yaml --report reports/scan_real/scan.json
```

```bash
uv run bone-reaper compare --input data/raw --config config/paper.yaml --output reports/compare --workers 5
```

```bash
uv run python scripts/run_forecast_research.py --markets 2500 --benchmark-markets 300
```

```bash
uv run python scripts/run_model_audit.py --input data/raw --workers 5
```

Recorded data stays local in `data/raw` (557 MB, not in git). Reports carry their manifests: code commit, config
hash, input checksums, assumptions and known gaps. The forecast report and the model audit show `+dirty` commits
because unrelated files were being edited while they ran; `compare` and `replay_real` are from the clean commit
`397ee5d`, and the audit is from `8b757a7`.
