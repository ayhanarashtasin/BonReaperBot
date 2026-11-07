"""Command line entry point. Every command prints mode, config hash and allowed network actions."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import threading
import webbrowser
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .settings import ConfigError, Settings, allowed_network_actions, load_settings, parse_settings
from .types import Mode, to_jsonable

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2


def _emit(doc: Any) -> None:
    print(json.dumps(to_jsonable(doc), indent=1, sort_keys=True))


def _banner(settings: Settings, command: str) -> None:
    _emit(
        {
            "banner": {
                "command": command,
                "version": __version__,
                "mode": settings.mode.value,
                "config_hash": settings.config_hash(),
                "network": allowed_network_actions(settings.mode),
                "disclaimer": "research tool; simulated results are not earnings",
            }
        }
    )


def _settings(args: argparse.Namespace, default_mode: Mode = Mode.READ_ONLY) -> Settings:
    if getattr(args, "config", None):
        return load_settings(args.config)
    return Settings(mode=default_mode)


def cmd_doctor(args: argparse.Namespace) -> int:
    from .execution.factory import build_execution
    from .telemetry.health import doctor_report

    settings = _settings(args)
    _banner(settings, "doctor")
    report = doctor_report(settings, offline=args.offline, build_execution=build_execution)
    _emit({"doctor": report})
    return EXIT_OK if report["ok"] else EXIT_FAILURE


def cmd_markets(args: argparse.Namespace) -> int:
    from .connectors.polymarket_public import ReadOnlyTransport
    from .market.discovery import discover
    from .market.rules import get_instrument
    from .time import SystemClock

    settings = _settings(args)
    _banner(settings, "markets")
    instrument = get_instrument(args.symbol, args.duration)

    async def run() -> Any:
        transport = ReadOnlyTransport()
        try:
            return await discover(
                transport,
                instrument,
                SystemClock().utc_ms(),
                limit=args.limit,
                collection=settings.execution.fee_collection,
            )
        finally:
            await transport.close()

    result = asyncio.run(run())
    _emit(
        {
            "markets": [
                {
                    "market_id": s.market_id,
                    "symbol": s.symbol,
                    "slug": s.slug,
                    "start_utc_ms": s.start_utc_ms,
                    "end_utc_ms": s.end_utc_ms,
                    "version": s.version.value,
                    "yes_asset_id": s.yes_asset_id,
                    "no_asset_id": s.no_asset_id,
                    "tick_size": s.tick_size,
                    "min_order_shares": s.min_order_shares,
                    "fee": s.fee.version,
                    "rules_hash": s.rules_hash,
                    "oracle": s.oracle_source,
                    "status": s.status.value,
                }
                for s in result.accepted
            ],
            "rejected": result.rejected,
        }
    )
    return EXIT_OK


def cmd_record(args: argparse.Namespace) -> int:
    from .app import Recorder
    from .connectors.polymarket_public import ReadOnlyTransport
    from .logging import configure
    from .market.rules import get_instrument

    settings = _settings(args)
    _banner(settings, "record")
    configure()
    instrument = get_instrument(args.symbol, args.duration_minutes)
    out = Path(args.output or settings.storage.raw_dir)

    async def run() -> dict[str, Any]:
        transport = ReadOnlyTransport()
        try:
            recorder = Recorder(
                settings,
                instrument,
                out,
                transport=transport,
                enable_twap=not args.no_twap,
                enable_binance=args.binance,
            )
            return await recorder.run(args.seconds)
        finally:
            await transport.close()

    try:
        health = asyncio.run(run())
    except KeyboardInterrupt:
        print("recorder interrupted; flushed parts are safe, see health.json", file=sys.stderr)
        return EXIT_OK
    _emit({"record_final_health": health})
    return EXIT_OK


def cmd_health(args: argparse.Namespace) -> int:
    from .telemetry.health import health_report

    settings = _settings(args)
    _banner(settings, "health")
    _emit({"health": health_report(settings, Path(args.input or settings.storage.raw_dir))})
    return EXIT_OK


def cmd_coverage(args: argparse.Namespace) -> int:
    from .telemetry.health import coverage_report

    settings = _settings(args)
    _banner(settings, "coverage")
    _emit({"coverage": coverage_report(Path(args.input))})
    return EXIT_OK


def cmd_scan(args: argparse.Namespace) -> int:
    from .research.scan_runner import run_scan

    settings = _settings(args)
    _banner(settings, "scan")
    summary = run_scan(Path(args.input), settings, Path(args.report), max_events=args.max_events)
    _emit({"scan_summary": summary})
    return EXIT_OK


def cmd_replay(args: argparse.Namespace) -> int:
    from .research.experiments import run_replay_command

    settings = _settings(args, Mode.PAPER)
    _banner(settings, "replay")
    summary = run_replay_command(Path(args.input), settings, Path(args.output), args.seed)
    _emit({"replay_summary": summary})
    return EXIT_OK


def cmd_compare(args: argparse.Namespace) -> int:
    from .research.experiments import run_compare

    settings = _settings(args, Mode.PAPER)
    _banner(settings, "compare")
    summary = run_compare(Path(args.input), settings, Path(args.output), args.seed, args.workers)
    _emit({"compare_summary": summary})
    return EXIT_OK


def cmd_paper(args: argparse.Namespace) -> int:
    from .live_sim import run_live_sim

    settings = _settings(args, Mode.PAPER)
    _banner(settings, "paper")
    if settings.mode is not Mode.PAPER:
        print("paper command requires mode: paper", file=sys.stderr)
        return EXIT_CONFIG
    _emit({"paper": asyncio.run(run_live_sim(settings, Path(args.output), args.seconds))})
    return EXIT_OK


def cmd_shadow(args: argparse.Namespace) -> int:
    from .live_sim import run_live_sim

    settings = _settings(args, Mode.SHADOW)
    _banner(settings, "shadow")
    if settings.mode is not Mode.SHADOW:
        print("shadow command requires mode: shadow", file=sys.stderr)
        return EXIT_CONFIG
    _emit({"shadow": asyncio.run(run_live_sim(settings, Path(args.output), args.seconds))})
    return EXIT_OK


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .dashboard.server import make_server

    settings = _settings(args)
    _banner(settings, "dashboard")
    try:
        server = make_server(Path(args.root), settings, args.port)
    except OSError as exc:
        print(f"could not start on port {args.port}: {exc}. Try --port.", file=sys.stderr)
        return EXIT_FAILURE
    host, port = server.server_address[:2]
    print(f"Dashboard: http://{host!s}:{port} (read-only, this computer only). Ctrl+C stops it.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return EXIT_OK


def cmd_start(args: argparse.Namespace) -> int:
    from .dashboard.server import make_server
    from .live_sim import run_live_sim

    if getattr(args, "config", None):
        settings = load_settings(args.config)
    elif Path("config/paper.yaml").exists():
        settings = load_settings("config/paper.yaml")
    else:
        settings = parse_settings({"mode": "paper", "strategies": {"passive_quotes": True}})
    if settings.mode is Mode.LIVE:
        return cmd_live(args)
    _banner(settings, "start")
    try:
        server = make_server(Path(args.root), settings, args.port)
    except OSError:
        if args.port == 0:
            raise
        server = make_server(Path(args.root), settings, 0)
    host, port = server.server_address[:2]
    url = f"http://{host!s}:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Dashboard: {url} (read-only, this computer only). Ctrl+C stops bot and dashboard.")
    if not args.no_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(url)
    try:
        if settings.mode in (Mode.PAPER, Mode.SHADOW):
            result = asyncio.run(run_live_sim(settings, Path(args.output), args.seconds))
            _emit({settings.mode.value: result, "dashboard_url": url})
        else:
            from .app import Recorder
            from .connectors.polymarket_public import ReadOnlyTransport
            from .logging import configure
            from .market.rules import get_instrument

            configure()
            instrument = get_instrument(
                settings.markets.symbols[0], settings.markets.durations_minutes[0]
            )
            out = Path(args.output or settings.storage.raw_dir)

            async def record_only() -> dict[str, Any]:
                transport = ReadOnlyTransport()
                try:
                    recorder = Recorder(settings, instrument, out, transport=transport)
                    return await recorder.run(args.seconds)
                finally:
                    await transport.close()

            _emit({"record_final_health": asyncio.run(record_only()), "dashboard_url": url})
    except KeyboardInterrupt:
        print("stopped by operator; see dashboard/reports for final state", file=sys.stderr)
    finally:
        server.shutdown()
        server.server_close()
    return EXIT_OK


def cmd_reconcile(args: argparse.Namespace) -> int:
    settings = _settings(args)
    _banner(settings, "reconcile")
    if not args.dry_run:
        print("reconcile supports --dry-run only", file=sys.stderr)
        return EXIT_CONFIG
    _emit(
        {
            "reconcile": {
                "status": "NOT_RUN",
                "reason": "no authorized account access; real reconciliation is BR-703 (blocked). "
                "Simulated reconciliation runs inside replay and paper sessions.",
            }
        }
    )
    return EXIT_OK


def cmd_live(_args: argparse.Namespace) -> int:
    print(
        "LIVE_DISABLED: no live command exists. P7 is blocked pending explicit authorization.",
        file=sys.stderr,
    )
    return EXIT_FAILURE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bone-reaper",
        description="Research and paper-trading platform. Defaults to read_only; no live trading.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(
        name: str, handler: Any, help_text: str, *, aliases: Sequence[str] = ()
    ) -> argparse.ArgumentParser:
        command = sub.add_parser(name, aliases=list(aliases), help=help_text)
        command.set_defaults(handler=handler)
        command.add_argument("--config", help="YAML config path")
        return command

    p = add("doctor", cmd_doctor, "Validate environment and safety gates")
    p.add_argument("--offline", action="store_true", help="skip network checks")

    p = add("markets", cmd_markets, "List verified current and upcoming markets")
    p.add_argument("--symbol", default="BTC")
    p.add_argument("--duration", type=int, default=5)
    p.add_argument("--limit", type=int, default=6)

    p = add("record", cmd_record, "Record public streams to append-only Parquet")
    p.add_argument("--symbol", default="BTC")
    p.add_argument("--duration-minutes", type=int, default=5)
    p.add_argument("--markets", default="btc-updown", help="informational market family label")
    p.add_argument("--output", help="raw data directory")
    p.add_argument("--seconds", type=float, help="stop after this many seconds")
    p.add_argument("--no-twap", action="store_true")
    p.add_argument("--binance", action="store_true", help="also record BTCUSDT spot aggTrades")

    p = add("health", cmd_health, "Read-only health output (machine readable)")
    p.add_argument("--input", help="raw data directory")

    p = add("coverage", cmd_coverage, "Data coverage, gaps and REST-sample validation")
    p.add_argument("--input", required=True)

    p = add("scan", cmd_scan, "Fee-aware pair scan over recorded data (no orders)")
    p.add_argument("--input", required=True)
    p.add_argument("--report", required=True)
    p.add_argument("--max-events", type=int)

    p = add(
        "replay",
        cmd_replay,
        "Deterministic event replay with simulated execution",
        aliases=("backtest",),
    )
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--seed", type=int, default=7)

    p = add("compare", cmd_compare, "Strategy and sensitivity comparison across scenarios")
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--workers", type=int, default=1, help="parallel replays; results are identical")

    p = add("paper", cmd_paper, "Live feed, fully simulated orders and fills")
    p.add_argument("--output", default="reports/paper")
    p.add_argument("--seconds", type=float)

    p = add("shadow", cmd_shadow, "Live feed, hypothetical fills recorded, no network orders")
    p.add_argument("--output", default="reports/shadow")
    p.add_argument("--seconds", type=float)

    p = add("dashboard", cmd_dashboard, "Local read-only dashboard over reports and status files")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--root", default=".", help="project folder holding reports/ and data/")

    p = add("start", cmd_start, "Start bot session and local read-only dashboard together")
    p.add_argument("target", nargs="?", default="bone", help=argparse.SUPPRESS)
    p.add_argument("--output", default="reports/paper")
    p.add_argument("--seconds", type=float)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--root", default=".", help="project folder holding reports/ and data/")
    p.add_argument("--no-browser", action="store_true", help="do not open browser automatically")

    p = add("reconcile", cmd_reconcile, "Exchange reconciliation (needs authorized access)")
    p.add_argument("--dry-run", action="store_true")

    add("live", cmd_live, "Refuses: live trading is disabled")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    os.environ.setdefault("PYTHONUTF8", "1")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code: int = args.handler(args)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    return code


if __name__ == "__main__":
    raise SystemExit(main())
