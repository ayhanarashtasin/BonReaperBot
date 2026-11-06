"""P3: fill simulators and deterministic replay. Includes the mandated case 4 (touch is no fill)."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from bone_reaper.execution.factory import NoExecutionInReadOnlyError
from bone_reaper.execution.paper import EventScheduler
from bone_reaper.execution.reconciler import reconcile
from bone_reaper.research.experiments import chronological_partitions
from bone_reaper.research.latency import LatencyModel, LatencyProfile, percentiles
from bone_reaper.research.maker_queue import QueuePosition, QueueScenario
from bone_reaper.research.metrics import compute_metrics
from bone_reaper.research.replay import SimCore, SimScenario, replay_events
from bone_reaper.research.taker_fill import DepthOverlay, match_taker
from bone_reaper.settings import Settings
from bone_reaper.types import (
    IntentPurpose,
    OrderState,
    OrderType,
    Outcome,
    QuoteIntent,
    Side,
    canonical_json,
)
from helpers import (
    START,
    EnvelopeFactory,
    book_payload,
    book_state,
    change,
    make_spec,
    paper_settings,
    price_change_payload,
    trade_payload,
)

Q = QueueScenario


class TestMakerQueue:
    def test_mandated_case4_touch_without_queue_depletion_does_not_fill(self):
        position = QueuePosition.join(D("0.40"), D(10), D(100), Q.CENTRAL)
        assert position.on_trade_at_price(D(30)) == 0, (
            "trade touched our price; queue ahead took it"
        )
        assert position.ahead == D(70) and position.remaining == D(10)

    def test_fill_only_after_queue_ahead_is_consumed(self):
        position = QueuePosition.join(D("0.40"), D(10), D(100), Q.CENTRAL)
        assert position.on_trade_at_price(D(104)) == D(4)
        assert position.remaining == D(6) and position.ahead == 0
        assert position.on_trade_at_price(D(50)) == D(6)

    def test_fill_through_fills_remaining(self):
        position = QueuePosition.join(D("0.40"), D(10), D(100), Q.PESSIMISTIC)
        assert position.on_trade_through() == D(10)
        assert position.remaining == 0

    @pytest.mark.parametrize(
        ("scenario", "ahead_after"),
        [(Q.PESSIMISTIC, D(50) * D("1.25")), (Q.CENTRAL, D(50)), (Q.OPTIMISTIC, D(50))],
    )
    def test_unexplained_removal_is_bracketed(self, scenario, ahead_after):
        position = QueuePosition.join(D("0.40"), D(10), D(100), scenario)
        position.on_level_size(D(50))
        assert position.ahead == ahead_after

    def test_pessimistic_joins_deeper(self):
        assert QueuePosition.join(D("0.4"), D(1), D(100), Q.PESSIMISTIC).ahead == D(125)
        assert QueuePosition.join(D("0.4"), D(1), D(100), Q.OPTIMISTIC).ahead == D(100)

    def test_trade_explained_removal_does_not_double_count(self):
        position = QueuePosition.join(D("0.40"), D(10), D(100), Q.OPTIMISTIC)
        position.on_trade_at_price(D(30))
        position.on_level_size(D(70))
        assert position.ahead == D(70), "that size drop was the same trade, not a new cancel"

    def test_new_size_behind_us_changes_nothing(self):
        position = QueuePosition.join(D("0.40"), D(10), D(100), Q.CENTRAL)
        position.on_level_size(D(500))
        assert position.ahead == D(100)

    def test_scenarios_are_ordered_by_fill_speed(self):
        filled = {}
        for scenario in Q:
            position = QueuePosition.join(D("0.40"), D(10), D(100), scenario)
            position.on_level_size(D(60))
            filled[scenario] = position.on_trade_at_price(D(70))
        assert filled[Q.PESSIMISTIC] <= filled[Q.CENTRAL] <= filled[Q.OPTIMISTIC]
        assert filled[Q.PESSIMISTIC] == 0 and filled[Q.OPTIMISTIC] == D(10)


class TestTakerMatch:
    def book(self):
        return book_state("a", asks=(("0.40", "5"), ("0.41", "5"), ("0.45", "100")))

    def test_walks_depth_to_limit(self):
        matches = match_taker(self.book(), Side.BUY, D("0.41"), D(8), OrderType.FAK, DepthOverlay())
        assert [(m.price, m.gross_shares) for m in matches] == [
            (D("0.40"), D(5)),
            (D("0.41"), D(3)),
        ]

    def test_fak_partial_at_limit(self):
        matches = match_taker(
            self.book(), Side.BUY, D("0.41"), D(20), OrderType.FAK, DepthOverlay()
        )
        assert sum(m.gross_shares for m in matches) == D(10)

    def test_fok_all_or_nothing_and_consumes_nothing_on_failure(self):
        overlay = DepthOverlay()
        assert match_taker(self.book(), Side.BUY, D("0.41"), D(20), OrderType.FOK, overlay) == []
        assert not overlay.consumed

    def test_own_consumption_persists_until_venue_update(self):
        overlay = DepthOverlay()
        match_taker(self.book(), Side.BUY, D("0.40"), D(5), OrderType.FAK, overlay)
        assert match_taker(self.book(), Side.BUY, D("0.40"), D(5), OrderType.FAK, overlay) == []
        overlay.venue_update("a", "SELL", D("0.40"))
        assert match_taker(self.book(), Side.BUY, D("0.40"), D(5), OrderType.FAK, overlay)

    def test_sell_side(self):
        book = book_state("a", bids=(("0.40", "5"), ("0.39", "5")))
        matches = match_taker(book, Side.SELL, D("0.39"), D(7), OrderType.FAK, DepthOverlay())
        assert [m.price for m in matches] == [D("0.40"), D("0.39")]


class TestSchedulerAndLatency:
    def test_ties_break_by_priority_then_insertion(self):
        scheduler = EventScheduler()
        order = []
        scheduler.at(10, 2, order.append, "visible")
        scheduler.at(10, 0, order.append, "exchange-1")
        scheduler.at(10, 0, order.append, "exchange-2")
        scheduler.at(5, 3, order.append, "early")
        times = []
        scheduler.run_until(None, times.append)
        assert order == ["early", "exchange-1", "exchange-2", "visible"]
        assert times == sorted(times)

    def test_run_until_is_exclusive_of_later_work(self):
        scheduler = EventScheduler()
        seen = []
        scheduler.at(10, 0, seen.append, 1)
        scheduler.at(20, 0, seen.append, 2)
        scheduler.run_until(15, lambda t: None)
        assert seen == [1] and len(scheduler) == 1

    def test_same_seed_same_latency(self):
        first = LatencyModel(LatencyProfile(), 42)
        second = LatencyModel(LatencyProfile(), 42)
        assert [first.submit() for _ in range(50)] == [second.submit() for _ in range(50)]

    def test_scaled_profile_and_percentiles(self):
        assert LatencyProfile().scaled(2).submit_ms == 300
        report = percentiles(list(range(1, 101)))
        assert report["p50"] == 51 and report["p99"] == 100 and report["count"] == 100
        assert percentiles([])["p50"] is None


def _market_events(factory: EnvelopeFactory, spec, *, up_wins=True):
    """A quiet two-sided book with reported tops, updated every 100ms, then a resolution."""
    y, n = spec.yes_asset_id, spec.no_asset_id
    start = spec.start_utc_ms
    events = [
        factory.spec(spec, start - 5000),
        factory.make(
            book_payload(y, [("0.45", "100")], [("0.55", "100")], start),
            start + 10,
            market_id=spec.market_id,
        ),
        factory.make(
            book_payload(n, [("0.45", "100")], [("0.55", "100")], start),
            start + 11,
            market_id=spec.market_id,
        ),
    ]
    for tick in range(1, 2900):
        now = start + 100 * tick
        events.append(
            factory.make(
                price_change_payload(
                    now,
                    change(y, "BUY", "0.20", str(100 + tick % 7), "0.45", "0.55"),
                    change(n, "SELL", "0.80", str(100 + tick % 7), "0.45", "0.55"),
                ),
                now + 20,
                market_id=spec.market_id,
                asset_id="",
            )
        )
    events.append(factory.resolution(spec, spec.end_utc_ms + 30_000, up_wins))
    return events


def simple_settings(**changes):
    return paper_settings(
        min_quote_price="0.05",
        refresh_min_ticks=2,
        **changes,
    )


class TestReplayDeterminism:
    def events(self):
        factory = EnvelopeFactory()
        spec = make_spec(0)
        events = _market_events(factory, spec)
        y = spec.yes_asset_id
        # Takers sell through the quoted prices mid-market so that fills really occur.
        trade_time = spec.start_utc_ms + 60_000
        events.append(
            factory.make(
                trade_payload(y, "SELL", "0.30", "50", trade_time, "0xaaa"),
                trade_time + 20,
                market_id=spec.market_id,
            )
        )
        events.sort(key=lambda e: e.received_utc_ms)
        return events

    def test_same_seed_and_input_gives_byte_identical_metrics(self):
        events = self.events()
        runs = [
            canonical_json(compute_metrics(replay_events(events, simple_settings(), seed=11)))
            for _ in range(2)
        ]
        assert runs[0] == runs[1]

    def test_different_seed_changes_latency_samples(self):
        events = self.events()
        first = compute_metrics(replay_events(events, simple_settings(), seed=1))
        second = compute_metrics(replay_events(events, simple_settings(), seed=2))
        assert first["latency_ms"] != second["latency_ms"]

    def test_simulated_flag_and_ledger_clean(self):
        core = replay_events(self.events(), simple_settings(), seed=3)
        metrics = compute_metrics(core)
        assert metrics["simulated"] and "Not real earnings" in metrics["disclaimer"]
        assert metrics["ledger"]["conservation_errors"] == []
        assert metrics["safety"]["port_is_real"] is False
        assert metrics["safety"]["visibility_violations"] == 0
        assert metrics["reconciliation"]["runs"] == metrics["reconciliation"]["clean"]
        for fill in core.exchange.truth.trades:
            assert fill.simulated

    def test_read_only_settings_cannot_simulate_orders(self):
        with pytest.raises(NoExecutionInReadOnlyError):
            SimCore(Settings())


def _single_order_core(events_before, intent_fields, events_after=()):
    """Drive the exchange directly with one order against recorded events."""
    core = SimCore(paper_settings(passive_quotes=False, scan_pair=False), seed=1)
    for env in events_before:
        core.schedule_envelope(env)
    core.run_until(events_before[-1].received_utc_ms)
    now = core.scheduler.now
    spec = core.specs["m0"]
    fields = {
        "intent_id": "probe",
        "market_id": "m0",
        "asset_id": spec.yes_asset_id,
        "outcome": Outcome.YES,
        "side": Side.BUY,
        "price": D("0.40"),
        "size": D(10),
        "post_only": False,
        "order_type": OrderType.FOK,
        "expiry_cutoff_ms": spec.end_utc_ms,
        "strategy_id": "t",
        "purpose": IntentPurpose.PAIR_ENTRY,
    }
    fields.update(intent_fields)
    record = core.oms.submit(QuoteIntent(**fields))
    for env in events_after:
        core.schedule_envelope(env)
    core.run_until(now + 5000)
    return core, record


def _setup_events(ask="0.40"):
    factory = EnvelopeFactory()
    spec = make_spec(0)
    t = spec.start_utc_ms + 1000
    y, n = spec.yes_asset_id, spec.no_asset_id
    return (
        factory,
        spec,
        [
            factory.spec(spec, t - 2000),
            factory.make(book_payload(y, [("0.38", "100")], [(ask, "100")], t), t, source_ts=t),
            factory.make(
                book_payload(n, [("0.58", "100")], [("0.62", "100")], t), t + 1, source_ts=t
            ),
        ],
    )


class TestStaleLiquidity:
    def test_stale_visible_ask_cannot_fill_if_it_vanished_before_arrival(self):
        """The bot saw 0.40; the venue moved during the 150ms+ flight; a limit at 0.40 misses."""
        factory, spec, before = _setup_events()
        t = spec.start_utc_ms + 1000
        y = spec.yes_asset_id
        moved = factory.make(
            book_payload(y, [("0.38", "100")], [("0.50", "100")], t + 50),
            t + 60,
            source_ts=t + 50,
        )
        core, _ = _single_order_core(before, {}, [moved])
        assert core.oms.orders["probe"].state is OrderState.REJECTED
        assert core.account.shares_net(y) == 0
        assert core.exchange.stats["fok_kills"] == 1

    def test_fill_happens_when_depth_still_exists(self):
        _, spec, before = _setup_events()
        core, _ = _single_order_core(before, {})
        assert core.oms.orders["probe"].state is OrderState.FILLED
        assert core.account.shares_net(spec.yes_asset_id) < D(10), "fee taken in shares"

    def test_partial_liquidity_with_fak(self):
        factory, spec, before = _setup_events()
        t = spec.start_utc_ms + 1000
        y = spec.yes_asset_id
        thin = factory.make(
            book_payload(y, [("0.38", "100")], [("0.40", "4")], t + 10), t + 11, source_ts=t + 10
        )
        core, _ = _single_order_core([*before, thin], {"order_type": OrderType.FAK})
        record = core.oms.orders["probe"]
        assert record.matched_gross == D(4) and record.state is OrderState.EXPIRED


class TestPostOnlyAndExpiry:
    def test_post_only_that_would_cross_is_rejected_by_venue(self):
        _, _, before = _setup_events()
        core, _ = _single_order_core(
            before, {"post_only": True, "order_type": OrderType.GTC, "price": D("0.45")}
        )
        assert core.oms.orders["probe"].state is OrderState.REJECTED
        assert core.exchange.stats["post_only_rejects"] == 1

    def test_resting_order_expires_at_close(self):
        _, spec, before = _setup_events()
        core = SimCore(paper_settings(passive_quotes=False, scan_pair=False), seed=1)
        for env in before:
            core.schedule_envelope(env)
        core.run_until(before[-1].received_utc_ms)
        core.oms.submit(
            QuoteIntent(
                intent_id="probe",
                market_id="m0",
                asset_id=spec.yes_asset_id,
                outcome=Outcome.YES,
                side=Side.BUY,
                price=D("0.30"),
                size=D(10),
                post_only=True,
                order_type=OrderType.GTC,
                expiry_cutoff_ms=spec.end_utc_ms,
                strategy_id="t",
            )
        )
        core.run_until(spec.start_utc_ms + 5000)
        assert core.oms.orders["probe"].state is OrderState.OPEN
        core.run_until(spec.end_utc_ms + 1000)
        assert core.oms.orders["probe"].state is OrderState.EXPIRED
        assert core.account.cash_reserved == 0


class TestMakerOnExchange:
    def _rest(self, level_size="100", scenario=QueueScenario.CENTRAL):
        factory = EnvelopeFactory()
        spec = make_spec(0)
        t = spec.start_utc_ms + 1000
        y, n = spec.yes_asset_id, spec.no_asset_id
        core = SimCore(
            paper_settings(passive_quotes=False, scan_pair=False),
            SimScenario("t", scenario),
            seed=1,
        )
        # An empty queue at our price is an absent level, never a zero-size one.
        y_bids = [("0.40", level_size)] if level_size != "0" else [("0.39", "100")]
        n_asks = [("0.60", level_size)] if level_size != "0" else [("0.61", "100")]
        for env in [
            factory.spec(spec, t - 2000),
            factory.make(book_payload(y, y_bids, [("0.45", "100")], t), t, source_ts=t),
            factory.make(book_payload(n, [("0.55", "100")], n_asks, t), t, source_ts=t),
        ]:
            core.schedule_envelope(env)
        core.run_until(t)
        core.oms.submit(
            QuoteIntent(
                intent_id="q",
                market_id="m0",
                asset_id=y,
                outcome=Outcome.YES,
                side=Side.BUY,
                price=D("0.40"),
                size=D(10),
                post_only=True,
                order_type=OrderType.GTC,
                expiry_cutoff_ms=spec.end_utc_ms,
                strategy_id="t",
            )
        )
        core.run_until(t + 2000)
        return core, factory, spec, t + 2000

    def _feed(self, core, env):
        core.schedule_envelope(env)
        core.run_until(env.received_utc_ms + 2000)

    def test_trade_at_price_smaller_than_queue_is_no_fill(self):
        core, factory, spec, t = self._rest()
        self._feed(
            core,
            factory.make(
                trade_payload(spec.yes_asset_id, "SELL", "0.40", "30", t, "0x1"), t, source_ts=t
            ),
        )
        assert core.oms.orders["q"].matched_gross == 0
        assert core.account.shares_net(spec.yes_asset_id) == 0

    def test_mirrored_trade_on_other_outcome_consumes_the_same_level(self):
        """A taker BUY of NO at 0.60 hits the same liquidity as a YES bid at 0.40."""
        core, factory, spec, t = self._rest("0")
        self._feed(
            core,
            factory.make(
                trade_payload(spec.no_asset_id, "BUY", "0.60", "5", t, "0x2"), t, source_ts=t
            ),
        )
        assert core.oms.orders["q"].matched_gross == D(5)

    def test_taker_buying_yes_does_not_hit_our_bid(self):
        core, factory, spec, t = self._rest("0")
        self._feed(
            core,
            factory.make(
                trade_payload(spec.yes_asset_id, "BUY", "0.40", "5", t, "0x3"), t, source_ts=t
            ),
        )
        assert core.oms.orders["q"].matched_gross == 0

    def test_trade_above_our_bid_does_not_fill(self):
        core, factory, spec, t = self._rest("0")
        self._feed(
            core,
            factory.make(
                trade_payload(spec.yes_asset_id, "SELL", "0.45", "5", t, "0x4"), t, source_ts=t
            ),
        )
        assert core.oms.orders["q"].matched_gross == 0

    def test_trade_through_fills_at_our_price_as_maker(self):
        core, factory, spec, t = self._rest()
        self._feed(
            core,
            factory.make(
                trade_payload(spec.yes_asset_id, "SELL", "0.35", "1", t, "0x5"), t, source_ts=t
            ),
        )
        record = core.oms.orders["q"]
        assert record.state is OrderState.FILLED
        trade = core.exchange.truth.trades[0]
        assert trade.price == D("0.40") and trade.role.value == "maker" and trade.fee_shares == 0

    def test_duplicate_transaction_counted_once(self):
        core, factory, spec, t = self._rest("0")
        self._feed(
            core,
            factory.make(
                trade_payload(spec.yes_asset_id, "SELL", "0.40", "3", t, "0x6"), t, source_ts=t
            ),
        )
        self._feed(
            core,
            factory.make(
                trade_payload(spec.no_asset_id, "BUY", "0.60", "3", t, "0x6"), t, source_ts=t
            ),
        )
        assert core.oms.orders["q"].matched_gross == D(3)
        assert core.exchange.stats["trade_duplicates"] == 1

    def test_cancel_race_fill_arrives_before_cancel_reaches_venue(self):
        core, factory, spec, t = self._rest("0")
        core.oms.cancel("q")
        fill_time = t + 5
        self._feed(
            core,
            factory.make(
                trade_payload(spec.yes_asset_id, "SELL", "0.40", "4", fill_time, "0x7"),
                fill_time,
                source_ts=fill_time,
            ),
        )
        record = core.oms.orders["q"]
        assert record.matched_gross == D(4) and record.state is OrderState.CANCELED
        assert core.account.cash_reserved == 0
        assert core.account.shares_net(spec.yes_asset_id) == D(4)
        report = reconcile(core.oms, core.port, "race", core.limits.max_unexplained_reconciliation)
        assert report.clean


class TestPartitions:
    def test_chronological_with_embargo(self):
        specs = [make_spec(i) for i in range(20)]
        train, validation, test = chronological_partitions(specs)
        assert len(train.market_ids) == 12
        order = {s.market_id: s.start_utc_ms for s in specs}
        assert max(order[m] for m in train.market_ids) < min(
            order[m] for m in validation.market_ids
        )
        assert max(order[m] for m in validation.market_ids) < min(order[m] for m in test.market_ids)
        assert not (train.market_ids & validation.market_ids)
        assert not (validation.market_ids & test.market_ids)
        assert len(train.market_ids) + len(validation.market_ids) + len(test.market_ids) == 18


def test_start_constant_is_in_the_future_of_fixtures():
    assert START > 0
