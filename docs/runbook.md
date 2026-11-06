# Operator runbook

Everything here runs without trading credentials. No command in this repository can place, cancel or
fund anything on a real account. Live trading (P7) is blocked and needs a separate decision.

## 1. Setup

Requires [uv](https://docs.astral.sh/uv/). It installs the pinned Python 3.11 and all dependencies.

```bash
uv sync --dev
```

```bash
uv run bone-reaper doctor --config config/read_only.yaml
```

`doctor` should end with `"ok": true`. Check these fields:

| Field | Expected | If not |
|---|---|---|
| `read_only_no_adapter` | `true` | Stop. The safety gate is broken. |
| `live_disabled` | `true` | Stop. Should be impossible in this release. |
| `geoblock.status` | `allowed` | Research still works. Real trading could never start. |
| `secret_like_env_var_names` | `[]` | Remove those variables. Nothing here needs them. |

Every command first prints its mode, config hash, and allowed and forbidden network actions. Read it.

## 2. Check the clock

Replay separates clock error from network latency using a recorded offset, but keep the clock correct.
`clock_offset_ms` is in `data/raw/health.json` while the recorder runs. Above 1000 ms an alert fires
and the pre-trade gate treats it as clock skew once wired to a live feed. Fix it with Windows time
sync; the bot never changes system settings.

## 3. List markets

```bash
uv run bone-reaper markets --symbol BTC --duration 5
```

Rejected markets are listed with reasons. A reason such as `rules_hash_changed` or
`oracle_source_mismatch` means the venue changed its terms. Review them before changing anything: see
section 10.

## 4. Record

```bash
uv run bone-reaper record --config config/read_only.yaml --output data/raw --binance --seconds 3600
```

Storage use is about 200 MB per hour for BTC 5m. Parts are append-only and flushed every 5 seconds, so
a crash loses at most those seconds. Watch it from another terminal:

```bash
uv run bone-reaper health --config config/read_only.yaml --input data/raw
```

| Health field | Healthy | Meaning if not |
|---|---|---|
| `invalidations` | empty or rare | Books needed REST recovery. Sustained growth means a feed change. |
| `resnapshots` | near 0 | As above. Recovery is limited to one per asset per 5 s. |
| `source_age_ms.poly_market` | under 1000 | The stream stalled. It reconnects by itself. |
| `markets_resolved` | close to markets seen | Outcomes are arriving. Otherwise run the backfill below. |
| `twap_status` | `available:...` | No TWAP means no model; the scanner still works. |

**TWAP deadline:** the public RTDS TWAP feed this recorder uses shuts down on **2026-11-30**. After that
it reports `unavailable` until the authenticated PolyBolt route is set up (`SecureTwapFacade`, which
needs credentials and an operator decision). The scanner and maker simulation keep working; only
fair-value features stop.

If outcomes are missing (for example the recorder stopped early), add them. This is append-only:

```bash
uv run python scripts/backfill_resolutions.py --input data/raw
```

Check what was captured:

```bash
uv run bone-reaper coverage --input data/raw
```

## 5. Scan

```bash
uv run bone-reaper scan --input data/raw --config config/read_only.yaml --report reports/scan.json
```

A `passed_candidates` count is indicative only. It means the books once showed a price. It says nothing
about whether both legs would have filled, and the two requests are never atomic.

## 6. Replay and compare

```bash
uv run bone-reaper replay --input data/raw --config config/paper.yaml --output reports/replay
```

```bash
uv run bone-reaper compare --input data/raw --config config/paper.yaml --output reports/compare --workers 4
```

Same data, config and seed give byte-identical results. A full comparison takes about 30 minutes per
5M events with 4 workers. Each worker uses about 500 MB.

## 7. Read a report correctly

- **All PnL is simulated.** It is not earnings and not evidence of fillability.
- Start with `coverage_grade`, then the `untouched_test` rows. Ignore `train` for conclusions.
- `realized_settled` counts settled markets. Unresolved cost is listed separately and is never
  force-settled.
- Compare pessimistic with central. A strategy that only works under optimistic queue assumptions is
  not working.
- A negative 5s markout means fills tend to be followed by adverse moves: adverse selection.
- The opportunity count is never a pass metric.

## 8. Paper and shadow

```bash
uv run bone-reaper paper --config config/paper.yaml --output reports/paper --seconds 900
```

```bash
uv run bone-reaper shadow --config config/shadow.yaml --output reports/shadow --seconds 900
```

Both use the live public feed with fully simulated orders. Shadow also writes every hypothetical order
and fill to `shadow_records.jsonl`, with an explicit uncertainty label. Check `shutdown` in the output:
`clean: false` lists exactly what was still open or unresolved.

## 9. Stop and emergency actions

| Situation | Action |
|---|---|
| Stop any command | Ctrl+C. Flushed parts are safe. Check `health.json` afterwards. |
| Kill switch active (paper) | Read `safety.kill_events`. Transient reasons clear when the condition does. |
| Latched kill (`loss_cap`, `reconciliation_mismatch`, `storage_failure`, `operator`) | Only `KillSwitch.operator_reset(ts, operator)` clears these. Find the cause first. |
| Disk full | The recorder stops with `RecorderError` rather than recording with gaps silently. Free space and restart. |
| Rate limits | Discovery logs a gap and retries. REST recovery is already bounded. Do not lower the intervals. |

The kill switch stops new risk and tries to cancel. It cannot force a position closed.

## 10. Recover, reconcile and restore

**Restart.** The OMS rebuilds from its journal. Anything in flight becomes `UNKNOWN`, never "rejected",
and new risk stays frozen until reconciliation runs:

```python
from bone_reaper.execution.reconciler import reconcile

report = reconcile(oms, port, "after-restart")
```

`report.clean` must be true before trading resumes. Otherwise read `cash_diff`, `position_diffs` and
`unexplained_remote_orders`. Do not edit history: corrections go through
`PaperAccount.post_adjustment`, which records them.

`bone-reaper reconcile --dry-run` reports `NOT_RUN`, because real account reconciliation needs the
authenticated access blocked under BR-703.

**Restoring data.** `data/raw` is self-describing. Copy it with its `manifest.jsonl`; every read
verifies the SHA-256 of each part. A checksum failure means the copy is damaged, so restore that part
from backup and never hand-edit the manifest.

**Postgres.** `PostgresStore` requires the `postgres` extra and `BONE_REAPER_POSTGRES_DSN`. The schema
is `src/bone_reaper/storage/schema.sql`, applied by `migrate()`. One process owns a wallet through an
advisory lock, released automatically if it dies. `WalletLockedError` means another process is running.

**Rule or fee change.** If the venue changes rule text, oracle or fee schedule, markets are rejected
and nothing trades. Do not just update the constant: read the new rules, write an ADR, and only then
update `BTC_5M_RULES_HASH` in `market/rules.py` and add a regression fixture.

## 11. Verify a checkout

```bash
uv run ruff check .
```

```bash
uv run mypy src
```

```bash
uv run pytest tests -q
```

```bash
uv run python scripts/check_secrets.py
```

Tests are offline: an autouse guard fails any test that reaches a non-local host. Metered tests are
never collected.
