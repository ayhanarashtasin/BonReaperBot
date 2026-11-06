# Bone Reaper research platform

A testable, event-driven research and paper-trading platform for short binary crypto prediction markets
on Polymarket. It measures whether an executable edge exists after fees, queue position, latency and
one-sided fills.

> **This is not a validated profitable strategy, and nothing here is financial advice.** All PnL produced
> by this software is simulated. No code path can place a real order: the live adapter is deliberately
> absent, and every route returns `LIVE_DISABLED`.

## Current status

See [reports/readiness_memo.md](reports/readiness_memo.md) for the evidence and verdict, and
[reports/progress.md](reports/progress.md) for task-by-task results.

## Quick start

```bash
uv sync --dev
```

```bash
uv run bone-reaper doctor --config config/read_only.yaml
```

```bash
uv run bone-reaper markets --symbol BTC --duration 5
```

```bash
uv run bone-reaper record --config config/read_only.yaml --output data/raw --seconds 900
```

```bash
uv run bone-reaper scan --input tests/fixtures/replay --config config/read_only.yaml --report reports/scan.json
```

```bash
uv run bone-reaper replay --input tests/fixtures/replay --config config/paper.yaml --output reports/replay
```

The full operating guide is [docs/runbook.md](docs/runbook.md). No credentials are needed for any of it.

## Modes

| Mode | Market data | Orders | Fills | Status |
|---|---|---|---|---|
| `read_only` (default) | Live public | No adapter exists | None | Available |
| `paper` | Live public or recorded | Simulated | Simulated | Available |
| `shadow` | Live public | Hypothetical, logged | Hypothetical, labelled uncertain | Available |
| `live` | n/a | **Refused** | n/a | `LIVE_DISABLED`; P7 needs separate authorization |

## Commands

| Command | Purpose |
|---|---|
| `doctor` | Validate environment and safety gates |
| `markets` | List verified markets and rejection reasons |
| `record` | Record public streams to append-only Parquet |
| `health` | Machine-readable recorder state |
| `coverage` | Data coverage, gaps and book validation |
| `scan` | Fee- and depth-aware pair scan, no orders |
| `replay` | Deterministic simulation with reproducibility manifest |
| `compare` | Strategy and sensitivity matrix with chronological partitions |
| `paper` / `shadow` | Live feed with simulated orders |
| `reconcile --dry-run` | Reports `NOT_RUN`: needs blocked account access |
| `live` | Always refuses |

## Layout

```text
src/bone_reaper/
  connectors/   public client, streams, TWAP, Binance, geoblock
  market/       discovery, rules, L2 book, validation, rollover
  strategy/     fees, pair scanner, quotes, hedging, fair value, orchestrator
  risk/         pre-trade gate, exposure, limits, kill switch
  execution/    OMS, simulated exchange, shadow, reconciler, settlement
  accounting/   double-entry ledger, positions, PnL
  storage/      Parquet recorder, checkpoints, Postgres store
  research/     replay, fill models, metrics, experiments, probability
  telemetry/    health and alerts
tests/          unit, property, fault, integration (all offline)
docs/           runbook and ADRs
reports/        SDK audit, research reports, readiness memo
```

## Documents

- [PRD.md](PRD.md): product and safety contract
- [Architecture.md](Architecture.md): engineering blueprint
- [tasks.md](tasks.md): task queue with evidence-based checkboxes
- [docs/adr/README.md](docs/adr/README.md): decisions, including where the SDK differed from the plan

## License

Proprietary. No license is granted.
