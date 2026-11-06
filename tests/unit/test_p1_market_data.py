"""P1: discovery, version mapping, rules, normalization, L2 state, TWAP, Binance, recorder."""

from __future__ import annotations

import asyncio
import copy
import json
from decimal import Decimal as D

import pytest
from polymarket import Event
from polymarket.streams import CryptoTwapPriceSpec

from bone_reaper.app import Recorder
from bone_reaper.connectors.binance import (
    ingest_agg_trades,
    ingest_candles,
    normalize_live_agg_trade,
)
from bone_reaper.connectors.polymarket_market_stream import parse_wire_event
from bone_reaper.connectors.polymarket_public import book_to_payload, resolution_payload
from bone_reaper.connectors.polymarket_twap import (
    FEED_RTDS,
    ReferenceTracker,
    SecureTwapFacade,
    TwapAvailability,
    UnavailableTwapSource,
    normalize_polybolt_event,
)
from bone_reaper.market.book import BookManager, L2Book
from bone_reaper.market.discovery import (
    DiscoveryResult,
    MarketRejectedError,
    filter_events,
    market_spec_from_event,
)
from bone_reaper.market.rollover import RolloverController
from bone_reaper.market.rules import (
    BTC_5M_RULES_HASH,
    UnsupportedInstrumentError,
    get_instrument,
    rules_hash,
)
from bone_reaper.market.validation import book_problems, compare_snapshots
from bone_reaper.settings import Settings
from bone_reaper.storage.checkpoints import Checkpoint, CheckpointWriter, read_checkpoints
from bone_reaper.storage.raw_events import (
    RawEventWriter,
    RecorderError,
    load_manifest,
    read_events,
    verify_manifest,
)
from bone_reaper.types import BookValidity, MarketStatus, MarketVersion, ReferenceSource
from helpers import (
    NO,
    START,
    YES,
    EnvelopeFactory,
    book_payload,
    change,
    make_spec,
    price_change_payload,
)

INSTRUMENT = get_instrument("BTC", 5)


def parse(raw: dict) -> Event:
    return (
        Event.parse_response(raw) if hasattr(Event, "parse_response") else Event.model_validate(raw)
    )


def mutate(raw: dict, **market_changes) -> dict:
    copied = copy.deepcopy(raw)
    copied["markets"][0].update(market_changes)
    return copied


def now_in(raw: dict) -> int:
    return int(parse(raw).schedule.start_time.timestamp() * 1000) + 1000


class TestDiscovery:
    def test_real_event_maps_with_exact_window(self, gamma_upcoming):
        event = parse(gamma_upcoming)
        spec = market_spec_from_event(event, INSTRUMENT, now_in(gamma_upcoming))
        assert spec.end_utc_ms - spec.start_utc_ms == 300_000
        assert spec.version is MarketVersion.CTF
        assert spec.yes_label == "Up"
        assert spec.rules_hash == BTC_5M_RULES_HASH
        assert spec.fee.rate == D("0.07") and spec.fee.taker_only
        assert spec.tick_size == D("0.01") and spec.min_order_shares == D(5)
        assert spec.yes_asset_id == json.loads(gamma_upcoming["markets"][0]["clobTokenIds"])[0]

    def test_start_comes_from_event_not_listing_time(self, gamma_upcoming):
        spec = market_spec_from_event(parse(gamma_upcoming), INSTRUMENT, now_in(gamma_upcoming))
        listing = parse(gamma_upcoming).markets[0].state.start_date
        assert spec.start_utc_ms > int(listing.timestamp() * 1000)

    def test_upcoming_status(self, gamma_upcoming):
        early = now_in(gamma_upcoming) - 10_000
        spec = market_spec_from_event(parse(gamma_upcoming), INSTRUMENT, early)
        assert spec.status is MarketStatus.UPCOMING

    def test_inverted_outcome_order_never_inverts_the_pair(self, gamma_upcoming):
        raw = copy.deepcopy(gamma_upcoming)
        market = raw["markets"][0]
        tokens = json.loads(market["clobTokenIds"])
        market["outcomes"] = json.dumps(["Down", "Up"])
        market["clobTokenIds"] = json.dumps(list(reversed(tokens)))
        spec = market_spec_from_event(parse(raw), INSTRUMENT, now_in(raw))
        assert spec.yes_asset_id == tokens[0] and spec.no_asset_id == tokens[1]

    def test_v2_uses_position_ids(self, gamma_upcoming):
        raw = mutate(
            gamma_upcoming,
            version="v2",
            positionIds=json.dumps(["111", "222"]),
        )
        spec = market_spec_from_event(parse(raw), INSTRUMENT, now_in(raw))
        assert spec.version is MarketVersion.V2
        assert (spec.yes_asset_id, spec.no_asset_id) == ("111", "222")

    def test_v2_without_position_ids_rejected_rather_than_guessed(self, gamma_upcoming):
        raw = mutate(gamma_upcoming, version="v2", positionIds=None)
        with pytest.raises(MarketRejectedError) as info:
            market_spec_from_event(parse(raw), INSTRUMENT, now_in(raw))
        assert "outcome_asset_ids_unavailable" in info.value.reasons

    @pytest.mark.parametrize(
        ("changes", "reason"),
        [
            ({"version": None}, "unknown_market_version"),
            ({"negRisk": True}, "unsupported_neg_risk_or_unknown"),
            ({"description": "", "resolutionSource": "x"}, "rules_missing"),
            ({"description": "Resolves using Binance."}, "rules_hash_changed"),
            ({"resolutionSource": "https://binance.com"}, "oracle_source_mismatch"),
            ({"feesEnabled": None}, "fee_config_unknown"),
            ({"feesEnabled": True, "feeSchedule": None}, "fee_config_unknown"),
            ({"orderPriceMinTickSize": None}, "tick_size_missing"),
            ({"orderMinSize": None}, "min_order_size_missing"),
            ({"outcomes": json.dumps(["Yes", "No"])}, "outcome_asset_ids_unavailable"),
        ],
    )
    def test_rejections(self, gamma_upcoming, changes, reason):
        raw = mutate(gamma_upcoming, **changes)
        if changes.get("description") == "":
            raw["description"] = ""
        with pytest.raises(MarketRejectedError) as info:
            market_spec_from_event(parse(raw), INSTRUMENT, now_in(raw))
        assert reason in info.value.reasons

    def test_duration_and_series_checked(self, gamma_upcoming):
        raw = copy.deepcopy(gamma_upcoming)
        raw["endDate"] = "2099-01-01T00:00:00Z"
        with pytest.raises(MarketRejectedError):
            market_spec_from_event(parse(raw), INSTRUMENT, now_in(gamma_upcoming))
        other = copy.deepcopy(gamma_upcoming)
        other["series"][0]["slug"] = "eth-up-or-down-5m"
        with pytest.raises(MarketRejectedError) as info:
            market_spec_from_event(parse(other), INSTRUMENT, now_in(other))
        assert info.value.reasons == ["series_mismatch"]

    def test_filter_drops_irrelevant_and_expired(self, gamma_upcoming, gamma_closed):
        good = parse(gamma_upcoming)
        irrelevant = copy.deepcopy(gamma_upcoming)
        irrelevant["series"][0]["slug"] = "something-else"
        now = now_in(gamma_upcoming)
        result = filter_events([parse(irrelevant), good, parse(gamma_closed)], INSTRUMENT, now)
        assert isinstance(result, DiscoveryResult)
        assert [s.market_id for s in result.accepted] == [good.markets[0].id]
        assert any(r.reasons == ("series_mismatch",) for r in result.rejected)

    def test_not_accepting_orders_is_rejected(self, gamma_upcoming):
        raw = mutate(gamma_upcoming, acceptingOrders=False)
        result = filter_events([parse(raw)], INSTRUMENT, now_in(raw))
        assert not result.accepted and result.rejected[0].reasons == ("not_accepting_orders",)

    def test_closed_event_keeps_official_resolution(self, gamma_closed):
        payload = resolution_payload(parse(gamma_closed))
        assert payload["closed"] is True
        assert payload["price_to_beat"] is not None and payload["final_price"] is not None
        assert {payload["yes_price"], payload["no_price"]} == {"1", "0"}

    def test_instruments_cannot_be_enabled_by_name(self):
        for symbol, minutes in (("ETH", 5), ("BTC", 15)):
            with pytest.raises(UnsupportedInstrumentError):
                get_instrument(symbol, minutes)

    def test_rule_hash_ignores_whitespace_only(self):
        assert rules_hash("a  b\n c") == rules_hash("a b c")
        assert rules_hash("a b") != rules_hash("a c")


class TestStreamNormalization:
    def test_sdk_wire_book_is_lossless(self):
        wire = {
            "event_type": "book",
            "market": "0x" + "a" * 64,
            "asset_id": "123",
            "bids": [{"price": "0.40", "size": "10.50"}],
            "asks": [{"price": "0.60", "size": "1"}],
            "hash": "h",
            "timestamp": "1800000000000",
            "tick_size": "0.01",
        }
        payload = parse_wire_event(wire)
        assert payload["bids"][0] == {"price": "0.40", "size": "10.50"}
        assert payload["timestamp"] == 1800000000000
        assert payload["tick_size"] == "0.01"

    def test_sdk_wire_price_change_and_trade(self):
        market = "0x" + "a" * 64
        change_payload = parse_wire_event(
            {
                "event_type": "price_change",
                "market": market,
                "timestamp": "1800000000000",
                "price_changes": [
                    {
                        "asset_id": "1",
                        "price": "0.4",
                        "size": "0",
                        "side": "buy",
                        "best_bid": "0.39",
                        "best_ask": "0.41",
                    }
                ],
            }
        )
        assert change_payload["price_changes"][0]["side"] == "BUY"
        assert change_payload["price_changes"][0]["best_bid"] == "0.39"
        trade = parse_wire_event(
            {
                "event_type": "last_trade_price",
                "market": market,
                "asset_id": "1",
                "price": "0.4",
                "size": "2",
                "side": "SELL",
                "timestamp": "1800000000000",
            }
        )
        assert trade["size"] == "2" and trade["side"] == "SELL"

    def test_rest_book_normalizes_to_the_same_shape(self):
        from polymarket import OrderBook

        book = OrderBook.model_validate(
            {
                "market": "0x" + "a" * 64,
                "asset_id": "1",
                "bids": [{"price": "0.40", "size": "1"}],
                "asks": [{"price": "0.60", "size": "2"}, {"price": "0.50", "size": "3"}],
                "min_order_size": "5",
                "tick_size": "0.01",
                "neg_risk": False,
                "hash": "h",
            }
        )
        manager = BookManager()
        manager.register("m", "1", D("0.01"))
        manager.apply(book_to_payload(book), 10)
        assert manager.books["1"].best_ask == D("0.50"), "best ask must not assume wire order"


def _ready_book() -> tuple[BookManager, L2Book]:
    manager = BookManager()
    book = manager.register("m0", YES, D("0.01"))
    manager.apply(
        book_payload(YES, [("0.40", "10"), ("0.39", "20")], [("0.42", "5"), ("0.43", "6")], 1), 100
    )
    return manager, book


class TestL2Book:
    def test_snapshot_sorts_and_ready(self):
        _, book = _ready_book()
        state = book.state()
        assert state.validity is BookValidity.READY
        assert [lv.price for lv in state.bids] == [D("0.40"), D("0.39")]
        assert [lv.price for lv in state.asks] == [D("0.42"), D("0.43")]

    def test_updates_before_snapshot_are_not_applied(self):
        manager = BookManager()
        book = manager.register("m0", YES, D("0.01"))
        manager.apply(price_change_payload(1, change(YES, "BUY", "0.40", "1")), 100)
        assert book.validity is BookValidity.UNINITIALIZED and not book.bids
        assert book.ignored_before_snapshot == 1

    def test_size_replacement_and_deletion(self):
        manager, book = _ready_book()
        manager.apply(
            price_change_payload(2, change(YES, "BUY", "0.39", "99", "0.40", "0.42")), 110
        )
        assert book.bids[D("0.39")] == D(99)
        manager.apply(price_change_payload(3, change(YES, "BUY", "0.39", "0", "0.40", "0.42")), 120)
        assert D("0.39") not in book.bids
        assert book.validity is BookValidity.READY

    def test_trade_removed_level_is_pruned_from_reported_top(self):
        """Recorded behaviour: trades send no price_change, the reported top reveals them."""
        manager, book = _ready_book()
        manager.apply(price_change_payload(2, change(YES, "BUY", "0.41", "7", "0.41", "0.43")), 110)
        assert book.best_ask == D("0.43") and book.pruned_levels == 1
        assert book.validity is BookValidity.READY

    def test_new_bid_at_a_stale_ask_price_does_not_invalidate(self):
        """Real sequence: a bid lands at a price whose ask was just consumed by a trade."""
        manager, book = _ready_book()
        result = manager.apply(
            price_change_payload(2, change(YES, "BUY", "0.42", "35", "0.42", "0.43")), 110
        )
        assert book.best_bid == D("0.42") and book.best_ask == D("0.43")
        assert book.validity is BookValidity.READY and not result.invalidated

    def test_reported_top_better_than_book_means_syncing_then_invalid(self):
        manager, book = _ready_book()
        manager.apply(price_change_payload(2, change(YES, "BUY", "0.39", "1", "0.41", "0.42")), 110)
        assert book.validity is BookValidity.SYNCING
        assert book_problems(book.state(), 110, 1000) == ["book_syncing"]
        expired = manager.expire_syncing(110 + manager.sync_grace_ms + 1)
        assert expired == [(YES, "desync_timeout")]
        assert book.validity is BookValidity.INVALID

    def test_snapshot_repairs_syncing_and_invalid(self):
        manager, book = _ready_book()
        book.invalidate("stream_disconnected")
        manager.apply(book_payload(YES, [("0.40", "1")], [("0.42", "1")], 5), 200)
        assert book.validity is BookValidity.READY and book.invalid_reason is None

    def test_syncing_recovers_when_top_matches_again(self):
        manager, book = _ready_book()
        manager.apply(price_change_payload(2, change(YES, "BUY", "0.39", "1", "0.41", "0.42")), 110)
        manager.apply(price_change_payload(3, change(YES, "BUY", "0.41", "1", "0.41", "0.42")), 120)
        assert book.validity is BookValidity.READY

    @pytest.mark.parametrize(
        ("bids", "asks", "reason"),
        [
            ([("0.50", "1")], [("0.40", "1")], "crossed_book"),
            ([("0.40", "1")], [("0.40", "1")], "crossed_book"),
            ([("0.405", "1")], [("0.42", "1")], "off_tick_price"),
            ([("0.40", "0")], [("0.42", "1")], "nonpositive_size_in_snapshot"),
            ([("0.40", "-1")], [("0.42", "1")], "nonpositive_size_in_snapshot"),
        ],
    )
    def test_invalid_snapshots(self, bids, asks, reason):
        manager = BookManager()
        book = manager.register("m0", YES, D("0.01"))
        result = manager.apply(book_payload(YES, bids, asks, 1), 100)
        assert book.validity is BookValidity.INVALID and book.invalid_reason == reason
        assert result.invalidated == [(YES, reason)]

    def test_negative_update_and_off_tick_update_invalidate(self):
        manager, book = _ready_book()
        manager.apply(price_change_payload(2, change(YES, "BUY", "0.39", "-1")), 110)
        assert book.invalid_reason == "negative_size"
        manager, book = _ready_book()
        manager.apply(price_change_payload(2, change(YES, "BUY", "0.395", "1")), 110)
        assert book.invalid_reason == "off_tick_price"

    def test_empty_levels_are_valid(self):
        manager = BookManager()
        book = manager.register("m0", YES, D("0.01"))
        manager.apply(book_payload(YES, [], [], 1), 100)
        assert book.validity is BookValidity.READY
        assert book.best_bid is None and book.best_ask is None

    def test_tick_change_allows_finer_prices(self):
        manager, book = _ready_book()
        manager.apply(
            {"event_type": "tick_size_change", "asset_id": YES, "new_tick_size": "0.001"}, 110
        )
        assert book.tick_size == D("0.001") and book.validity is BookValidity.READY
        manager.apply(
            price_change_payload(2, change(YES, "BUY", "0.395", "1", "0.40", "0.42")), 120
        )
        assert book.validity is BookValidity.READY

    def test_coarser_tick_invalidates_until_snapshot(self):
        manager, book = _ready_book()
        manager.apply(price_change_payload(2, change(YES, "BUY", "0.39", "1", "0.40", "0.42")), 110)
        manager.apply(
            {"event_type": "tick_size_change", "asset_id": YES, "new_tick_size": "0.1"}, 110
        )
        assert book.validity is BookValidity.INVALID

    def test_stale_and_uninitialized_problems(self):
        _, book = _ready_book()
        assert book_problems(book.state(), 100, 1000) == []
        assert book_problems(book.state(), 2000, 1000) == ["book_stale"]
        assert "book_uninitialized" in book_problems(L2Book("m", "a", D("0.01")).state(), 0, 1000)

    def test_disconnect_invalidates_all(self):
        manager, book = _ready_book()
        manager.invalidate_all("stream_disconnected")
        assert book.validity is BookValidity.INVALID

    def test_unregistered_assets_never_create_books(self):
        manager = BookManager()
        manager.apply(book_payload("999", [("0.4", "1")], [], 1), 1)
        assert manager.unregistered_events == 1 and "999" not in manager.books

    def test_compare_snapshots(self):
        _, book = _ready_book()
        same = compare_snapshots(book.state(), book.state())
        assert same.matches and same.top_of_book_equal
        other = L2Book("m0", YES, D("0.01"))
        other.apply_snapshot([{"price": "0.40", "size": "11"}], [], 1)
        diff = compare_snapshots(book.state(), other.state())
        assert not diff.matches and diff.bid_level_diffs == 2 and diff.ask_level_diffs == 2
        assert diff.max_size_diff == D(20)


class TestRollover:
    def test_three_sequential_markets_no_stale_asset_reuse(self):
        controller = RolloverController()
        specs = [make_spec(i) for i in range(3)]
        controller.update(specs[:2], START - 1)
        assert controller.active is None and controller.prepared[0].market_id == "m0"
        controller.update([], START + 1)
        assert controller.active.market_id == "m0"
        assert controller.subscription_assets() == (f"{YES}0", f"{NO}0", f"{YES}1", f"{NO}1")
        controller.update(specs, START + 300_001)
        assert controller.active.market_id == "m1"
        assert [m.market_id for m in controller.retired] == ["m0"]
        controller.update([], START + 600_001)
        assert controller.active.market_id == "m2"
        assert not controller.failures
        kinds = [e.kind for e in controller.events if e.market_id == "m1"]
        assert kinds == ["prepared", "activated", "retired"]

    def test_stale_asset_reuse_is_refused(self):
        controller = RolloverController()
        first = make_spec(0)
        reuse = make_spec(1, yes_asset_id=first.yes_asset_id)
        controller.update([first, reuse], START + 1)
        assert controller.active.market_id == "m0"
        assert any("stale_asset_reuse" in f for f in controller.failures)

    def test_retired_market_never_reactivates(self):
        controller = RolloverController()
        spec = make_spec(0)
        controller.update([spec], START + 1)
        controller.update([], spec.end_utc_ms + 1)
        controller.update([spec], spec.end_utc_ms + 2)
        assert controller.active is None and len(controller.retired) == 1

    def test_quote_cutoff(self):
        controller = RolloverController(quote_cutoff_seconds=20)
        spec = make_spec(0)
        controller.update([spec], START + 1)
        assert controller.may_quote("m0", spec.end_utc_ms - 20_001)
        assert not controller.may_quote("m0", spec.end_utc_ms - 20_000)
        assert not controller.may_quote("other", START + 2)


class TestTwap:
    def test_unavailable_source_yields_nothing_and_says_so(self):
        source = UnavailableTwapSource("no_credentials")

        async def collect():
            return [x async for x in source.stream()]

        assert asyncio.run(collect()) == []
        assert source.availability is TwapAvailability.UNAVAILABLE

    def test_polybolt_update_and_snapshot_normalize(self):
        from polymarket.models.price_events import (
            CryptoTwapPriceSnapshotEvent,
            CryptoTwapPriceUpdateEvent,
        )

        update = CryptoTwapPriceUpdateEvent.model_validate(
            {
                "timestamp": "2026-11-06T18:25:00Z",
                "seq": 4,
                "payload": {
                    "timestamp": "2026-11-06T18:25:00Z",
                    "value": "75000.5",
                    "symbol": "btcusd",
                    "source": "chainlink",
                },
            }
        )
        normalized = normalize_polybolt_event(update)
        assert normalized["event_type"] == "twap_update" and normalized["value"] == "75000.5"
        assert normalized["window_seconds"] == 60 and normalized["source"] == "chainlink"
        snapshot = CryptoTwapPriceSnapshotEvent.model_validate(
            {
                "timestamp": "2026-11-06T18:25:00Z",
                "payload": {
                    "symbol": "btcusd",
                    "source": "chainlink",
                    "data": [{"timestamp": "2026-11-06T18:24:59Z", "value": "74999"}],
                },
            }
        )
        doc = normalize_polybolt_event(snapshot)
        assert doc["event_type"] == "twap_snapshot" and doc["history"][0]["value"] == "74999"

    def test_secure_facade_exposes_only_a_stream(self):
        facade = SecureTwapFacade(lambda spec: None, "btcusd")
        public = [n for n in dir(facade) if not n.startswith("_")]
        assert set(public) == {"availability", "reason", "stream"}

    def test_facade_subscribes_with_official_spec(self):
        seen = []

        class Handle:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        async def subscribe(spec):
            seen.append(spec)
            return Handle()

        async def run():
            return [x async for x in SecureTwapFacade(subscribe, "btcusd").stream()]

        assert asyncio.run(run()) == []
        assert isinstance(seen[0], CryptoTwapPriceSpec) and seen[0].symbols == ("btcusd",)

    def test_reference_tracker_strike_and_state(self):
        tracker = ReferenceTracker("btcusd")
        for ts, value in ((1000, "100"), (2000, "101"), (3000, "102")):
            tracker.on_payload(
                {"event_type": "twap_update", "feed": FEED_RTDS, "timestamp": ts, "value": value},
                ts + 50,
            )
        assert tracker.observed_strike(2500) == D("101")
        assert tracker.observed_strike(500) is None, "never uses a later observation"
        assert tracker.observed_strike(60_000) is None, "too old to trust"
        state = tracker.state(2500)
        assert state.twap == D("102") and state.source is ReferenceSource.CHAINLINK_TWAP
        assert state.start_strike == D("101")
        assert state.strike_provenance == "observed_twap_at_window_start"
        assert state.feed == FEED_RTDS

    def test_out_of_order_twap_ignored(self):
        tracker = ReferenceTracker("btcusd")
        tracker.on_payload({"event_type": "twap_update", "timestamp": 2000, "value": "2"}, 1)
        tracker.on_payload({"event_type": "twap_update", "timestamp": 1000, "value": "1"}, 2)
        assert tracker.state().twap == D("2")


class TestBinance:
    def test_agg_trades_quality_and_labels(self, tmp_path):
        path = tmp_path / "agg.csv"
        path.write_text(
            "agg_trade_id,price,quantity,first,last,transact_time,is_buyer_maker\n"
            "1,75000.10,0.5,1,1,1700000000000,true\n"
            "1,75000.10,0.5,1,1,1700000000000,true\n"
            "2,75001.00,1.0,2,2,1700000000500,false\n"
            "3,75002.00,1.0,3,3,1700000100000,false\n"
            "4,75002.00,1.0,4,4,1700000050000,false\n"
        )
        trades, manifest = ingest_agg_trades(path, "BTCUSDT", ReferenceSource.BINANCE_SPOT)
        assert trades[0].aggressor == "SELL" and trades[2].aggressor == "BUY"
        assert trades[0].price == D("75000.10")
        assert manifest.duplicates == 1 and manifest.out_of_order == 1 and manifest.gaps == 1
        assert manifest.source is ReferenceSource.BINANCE_SPOT
        assert manifest.quote_currency == "USDT" and len(manifest.sha256) == 64

    def test_microsecond_timestamps_normalize(self, tmp_path):
        path = tmp_path / "agg.csv"
        path.write_text("1,75000,0.5,1,1,1700000000000000,true,true\n")
        trades, _ = ingest_agg_trades(path, "BTCUSDT", ReferenceSource.BINANCE_SPOT)
        assert trades[0].ts_ms == 1700000000000

    def test_candles_and_gap_detection(self, tmp_path):
        path = tmp_path / "k.csv"
        path.write_text(
            "1700000000000,1,2,0.5,1.5,10,1700000059999,0,0,0,0,0\n"
            "1700000060000,1.5,2,1,1.6,11,1700000119999,0,0,0,0,0\n"
            "1700000240000,1.6,2,1,1.7,12,1700000299999,0,0,0,0,0\n"
        )
        candles, manifest = ingest_candles(path, "BTCUSDT", ReferenceSource.BINANCE_FUTURES)
        assert candles[1].close == D("1.6") and manifest.gaps == 1
        assert manifest.largest_gap_ms == 180_000

    def test_binance_can_never_be_ingested_as_chainlink(self, tmp_path):
        path = tmp_path / "a.csv"
        path.write_text("1,1,1,1,1,1700000000000,true\n")
        with pytest.raises(ValueError):
            ingest_agg_trades(path, "BTCUSDT", ReferenceSource.CHAINLINK_TWAP)
        with pytest.raises(ValueError):
            ingest_candles(path, "BTCUSDT", ReferenceSource.CHAINLINK_TWAP)
        with pytest.raises(ValueError):
            normalize_live_agg_trade({}, ReferenceSource.CHAINLINK_TWAP)

    def test_live_frame_normalizes_with_own_label(self):
        doc = normalize_live_agg_trade(
            {"s": "BTCUSDT", "a": 5, "p": "75000", "q": "1", "T": 123, "m": True},
            ReferenceSource.BINANCE_SPOT,
        )
        assert doc["reference_source"] == "binance_spot" and doc["aggressor"] == "SELL"


class TestRecorderStorage:
    def _events(self):
        factory = EnvelopeFactory()
        spec = make_spec(0)
        yes, no = spec.yes_asset_id, spec.no_asset_id
        return [
            factory.spec(spec, START),
            factory.make(book_payload(yes, [("0.40", "10")], [("0.42", "5")], START), START + 1),
            factory.make(book_payload(no, [("0.58", "5")], [("0.60", "10")], START), START + 2),
            factory.make(
                price_change_payload(
                    START + 3,
                    change(yes, "BUY", "0.39", "4", "0.40", "0.42"),
                    change(no, "SELL", "0.61", "4", "0.58", "0.60"),
                ),
                START + 4,
                asset_id=None,
            ),
        ]

    def test_record_stop_replay_gives_identical_books(self, tmp_path):
        events = self._events()
        writer = RawEventWriter(tmp_path, run_id="r1")
        live = BookManager()
        spec = make_spec(0)
        for env in events:
            writer.append(env)
        writer.flush()
        for asset in (spec.yes_asset_id, spec.no_asset_id):
            live.register("m0", asset, spec.tick_size)
        for env in events[1:]:
            live.apply(env.payload(), env.received_utc_ms)
        replayed = BookManager()
        for asset in (spec.yes_asset_id, spec.no_asset_id):
            replayed.register("m0", asset, spec.tick_size)
        restored = list(read_events(tmp_path))
        assert [e.event_id for e in restored] == [e.event_id for e in events]
        assert [e.payload_json for e in restored] == [e.payload_json for e in events]
        for env in restored:
            if env.topic != "market_spec":
                replayed.apply(env.payload(), env.received_utc_ms)
        for asset in (spec.yes_asset_id, spec.no_asset_id):
            assert live.books[asset].state() == replayed.books[asset].state()

    def test_partition_layout_and_manifest(self, tmp_path):
        writer = RawEventWriter(tmp_path, run_id="r1", symbol="BTC")
        for env in self._events():
            writer.append(env)
        writer.flush()
        records = load_manifest(tmp_path)
        assert {r.source for r in records} == {"control", "poly_market"}
        path = next(r.path for r in records if r.source == "poly_market")
        parts = path.split("/")
        assert parts[0] == "poly_market" and parts[2] == "BTC" and parts[3] == "m0"
        assert verify_manifest(tmp_path, records) == []
        assert writer.rows_written == 4

    def test_history_is_never_overwritten(self, tmp_path):
        writer = RawEventWriter(tmp_path, run_id="same")
        events = self._events()
        writer.append(events[1])
        writer.flush()
        second = RawEventWriter(tmp_path, run_id="same")
        second.append(events[2])
        with pytest.raises(RecorderError):
            second.flush()

    def test_tampering_detected(self, tmp_path):
        writer = RawEventWriter(tmp_path, run_id="r1")
        writer.append(self._events()[1])
        writer.flush()
        record = load_manifest(tmp_path)[0]
        (tmp_path / record.path).write_bytes(b"corrupt")
        assert verify_manifest(tmp_path, [record])[0].startswith("checksum_mismatch")
        with pytest.raises(RecorderError):
            list(read_events(tmp_path))

    def test_disk_failure_raises_recorder_error(self, tmp_path, monkeypatch):
        writer = RawEventWriter(tmp_path, run_id="r1")
        writer.append(self._events()[1])

        def full(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("pathlib.Path.open", full)
        with pytest.raises(RecorderError):
            writer.flush()

    def test_checkpoints_roundtrip_and_reference_offset(self, tmp_path):
        _manager, book = _ready_book()
        writer = CheckpointWriter(tmp_path / "c.jsonl")
        writer.write(Checkpoint(5, 42, (book.state(),)))
        restored = read_checkpoints(tmp_path / "c.jsonl")
        assert restored[0].raw_event_offset == 42 and restored[0].books[0] == book.state()


class FakeTransport:
    """Duck-typed read-only transport for recorder fault tests."""

    client = object()

    def __init__(self, specs_event, rest_payload):
        self.event = specs_event
        self.rest_payload = rest_payload
        self.book_calls = 0
        self.discovery_calls = 0
        self.fail_discovery = False

    async def series_id(self, slug):
        return 1

    async def open_events(self, series_id, now, limit):
        self.discovery_calls += 1
        if self.fail_discovery:
            from polymarket import RateLimitError

            raise RateLimitError("slow down")
        return [self.event]

    async def book_payload(self, asset_id):
        self.book_calls += 1
        return self.rest_payload | {"asset_id": asset_id}

    async def event_by_slug(self, slug):
        return self.event


class TestRecorderFaults:
    def _recorder(self, tmp_path, gamma_upcoming, stream, transport=None):
        event = parse(gamma_upcoming)
        now = now_in(gamma_upcoming)
        from bone_reaper.time import FakeClock

        transport = transport or FakeTransport(
            event, book_payload("x", [("0.40", "1")], [("0.42", "1")], now)
        )
        recorder = Recorder(
            Settings(),
            INSTRUMENT,
            tmp_path,
            transport=transport,
            clock=FakeClock(now),
            enable_twap=False,
            market_stream=stream,
        )
        return recorder, transport

    def test_stream_failure_marks_gap_invalid_and_retries(self, tmp_path, gamma_upcoming):
        condition = parse(gamma_upcoming).markets[0].condition_id
        calls = []

        async def stream(client, assets):
            calls.append(assets)
            yield (
                book_payload(assets[0], [("0.40", "1")], [("0.42", "1")], 1, condition),
                0,
            )
            raise ConnectionError("socket lost")

        recorder, _ = self._recorder(tmp_path, gamma_upcoming, stream)

        async def run():
            await recorder.run(0.2)

        asyncio.run(run())
        reasons = [g["reason"] for g in recorder.state.gaps]
        assert "stream_error:ConnectionError" in reasons
        assert all(b.validity is not BookValidity.READY for b in recorder.books.books.values())

    def test_sdk_drop_counter_invalidates_books(self, tmp_path, gamma_upcoming):
        condition = parse(gamma_upcoming).markets[0].condition_id

        async def stream(client, assets):
            yield book_payload(assets[0], [("0.40", "1")], [("0.42", "1")], 1, condition), 0
            yield book_payload(assets[1], [("0.40", "1")], [("0.42", "1")], 1, condition), 3
            await asyncio.sleep(10)

        recorder, _ = self._recorder(tmp_path, gamma_upcoming, stream)
        asyncio.run(recorder.run(0.2))
        assert any(g["reason"] == "sdk_queue_dropped_events" for g in recorder.state.gaps)

    def test_invalid_book_triggers_bounded_rest_resnapshot(self, tmp_path, gamma_upcoming):
        condition = parse(gamma_upcoming).markets[0].condition_id

        async def stream(client, assets):
            yield book_payload(assets[0], [("0.50", "1")], [("0.40", "1")], 1, condition), 0
            for _ in range(20):
                yield book_payload(assets[0], [("0.50", "1")], [("0.40", "1")], 1, condition), 0
                await asyncio.sleep(0)
            await asyncio.sleep(10)

        recorder, transport = self._recorder(tmp_path, gamma_upcoming, stream)
        asyncio.run(recorder.run(0.2))
        assert 1 <= transport.book_calls <= 2, "REST recovery must be rate limited"
        assert recorder.state.resnapshots >= 1

    def test_foreign_new_market_events_are_not_attributed(self, tmp_path, gamma_upcoming):
        async def stream(client, assets):
            yield {"event_type": "new_market", "market": "0xother", "timestamp": 1}, 0
            await asyncio.sleep(10)

        recorder, _ = self._recorder(tmp_path, gamma_upcoming, stream)
        asyncio.run(recorder.run(0.2))
        foreign = [e for e in read_events(tmp_path) if e.topic == "new_market"]
        assert foreign and all(e.market_id is None for e in foreign)

    def test_rate_limited_discovery_becomes_a_gap_not_a_crash(self, tmp_path, gamma_upcoming):
        async def stream(client, assets):
            await asyncio.sleep(10)
            yield {}, 0

        recorder, transport = self._recorder(tmp_path, gamma_upcoming, stream)
        transport.fail_discovery = True
        asyncio.run(recorder.run(0.2))
        assert any(g["reason"].startswith("discovery_failed") for g in recorder.state.gaps)
        assert not recorder.state.markets_seen

    def test_recorder_writes_machine_readable_health(self, tmp_path, gamma_upcoming):
        async def stream(client, assets):
            await asyncio.sleep(10)
            yield {}, 0

        recorder, _ = self._recorder(tmp_path, gamma_upcoming, stream)
        final = asyncio.run(recorder.run(0.2))
        doc = json.loads((tmp_path / "health.json").read_text())
        assert doc["run_id"] == final["run_id"] and doc["mode"] == "read_only"
        assert doc["markets_seen"] == 1 and "config_hash" in doc

    def test_retired_market_gets_official_resolution_recorded(
        self, tmp_path, gamma_upcoming, gamma_closed, monkeypatch
    ):
        """Regression: rollover events were consumed before anything could act on them."""
        import bone_reaper.app as app_module

        monkeypatch.setattr(app_module, "DISCOVERY_INTERVAL_S", 0.01)
        monkeypatch.setattr(app_module, "RESOLUTION_POLL_S", 0.01)

        async def stream(client, assets):
            await asyncio.sleep(10)
            yield {}, 0

        recorder, transport = self._recorder(tmp_path, gamma_upcoming, stream)
        spec_event = parse(gamma_upcoming)

        async def scenario():
            run = asyncio.create_task(recorder.run(0.4))
            await asyncio.sleep(0.1)
            transport.event_by_slug = _returns(parse(gamma_closed))
            recorder.clock.set_utc_ms(int(spec_event.schedule.end_date.timestamp() * 1000) + 1)
            await run

        asyncio.run(scenario())
        events = list(read_events(tmp_path))
        kinds = [e.payload().get("kind") for e in events if e.topic == "rollover"]
        assert "activated" in kinds and "retired" in kinds
        resolutions = [e for e in events if e.topic == "market_resolution"]
        assert resolutions and resolutions[0].payload()["closed"] is True
        assert recorder.state.resolutions

    def test_outcome_is_recorded_before_the_final_price_exists(
        self, tmp_path, gamma_upcoming, gamma_closed, monkeypatch
    ):
        import bone_reaper.app as app_module

        monkeypatch.setattr(app_module, "DISCOVERY_INTERVAL_S", 0.01)
        monkeypatch.setattr(app_module, "RESOLUTION_POLL_S", 0.01)

        async def stream(client, assets):
            await asyncio.sleep(10)
            yield {}, 0

        recorder, transport = self._recorder(tmp_path, gamma_upcoming, stream)
        spec_event = parse(gamma_upcoming)
        no_final = copy.deepcopy(gamma_closed)
        no_final["eventMetadata"] = {"priceToBeat": 75000.0}

        async def scenario():
            run = asyncio.create_task(recorder.run(0.3))
            await asyncio.sleep(0.1)
            transport.event_by_slug = _returns(parse(no_final))
            recorder.clock.set_utc_ms(int(spec_event.schedule.end_date.timestamp() * 1000) + 1)
            await run

        asyncio.run(scenario())
        resolutions = [e for e in read_events(tmp_path) if e.topic == "market_resolution"]
        assert len(resolutions) == 1, "recorded once now, again only when the final price appears"
        assert resolutions[0].payload()["final_price"] is None
        assert resolutions[0].payload()["price_to_beat"] == "75000.0"

    def test_truncated_manifest_tail_is_tolerated_but_corruption_is_not(self, tmp_path):
        writer = RawEventWriter(tmp_path, run_id="r1")
        writer.append(EnvelopeFactory().make(book_payload("a", [], [], 1), 1))
        writer.flush()
        manifest = tmp_path / "manifest.jsonl"
        good = manifest.read_text()
        manifest.write_text(good + '{"path": "cut')
        assert len(load_manifest(tmp_path)) == 1
        manifest.write_text('{"path": "cut\n' + good)
        with pytest.raises(RecorderError):
            load_manifest(tmp_path)


def _returns(value):
    async def call(*args, **kwargs):
        return value

    return call
