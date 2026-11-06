"""Property tests: fee and depth arithmetic, ledger conservation, book and quote invariants."""

from __future__ import annotations

from decimal import Decimal as D

from hypothesis import given, settings
from hypothesis import strategies as st

from bone_reaper.accounting.position import PaperAccount
from bone_reaper.market.book import BookManager
from bone_reaper.research.maker_queue import QueuePosition, QueueScenario
from bone_reaper.risk.exposure import market_exposure
from bone_reaper.strategy.fair_value import sanitize_probability
from bone_reaper.strategy.fees import buy_fill_economics, fee_usd
from bone_reaper.strategy.pair_scanner import (
    ScanThresholds,
    buy_plan_for_net_shares,
    evaluate_pair,
)
from bone_reaper.strategy.quotes import QuoteParams, build_quote_plan
from bone_reaper.types import (
    FeeCollection,
    FillRecord,
    FillStatus,
    LiquidityRole,
    OrderRecord,
    OrderState,
    Outcome,
    PriceLevel,
    Side,
)
from helpers import START, book_state, crypto_fee, make_intent, make_spec

SPEC = make_spec()
NOW = START + 60_000
prices = st.integers(1, 99).map(lambda c: D(c) / 100)
sizes = st.integers(1, 100_000).map(lambda c: D(c) / 100)
fees = st.sampled_from(
    [crypto_fee(FeeCollection.SHARES_ON_BUY), crypto_fee(FeeCollection.CASH_ON_TOP)]
)
ask_levels = st.lists(st.tuples(prices, sizes), min_size=1, max_size=8, unique_by=lambda x: x[0])


@given(sizes, prices, fees)
def test_fee_is_nonnegative_rounded_and_capped(shares, price, fee):
    value = fee_usd(shares, price, fee)
    assert value >= 0
    assert value == value.quantize(D("0.00001"))
    assert value <= shares * fee.rate * D("0.25") + D("0.00001")


@given(sizes, prices, fees)
def test_buy_conservation_of_shares_and_cash(gross, price, fee):
    net, breakdown, cash = buy_fill_economics(gross, price, fee, LiquidityRole.TAKER)
    assert net + breakdown.fee_shares == gross
    assert cash == gross * price + breakdown.fee_cash
    assert net <= gross and cash >= gross * price


@given(sizes, prices)
def test_share_collection_is_never_cheaper_than_cash_collection(gross, price):
    """Why SHARES_ON_BUY is the conservative default (ADR-0002)."""
    share_net, _, share_cash = buy_fill_economics(
        gross, price, crypto_fee(FeeCollection.SHARES_ON_BUY), LiquidityRole.TAKER
    )
    cash_net, _, cash_cash = buy_fill_economics(
        gross, price, crypto_fee(FeeCollection.CASH_ON_TOP), LiquidityRole.TAKER
    )
    assert share_cash / share_net >= cash_cash / cash_net - D("0.000001")


@given(ask_levels, st.integers(500, 20_000).map(lambda c: D(c) / 100), fees)
def test_plan_never_exceeds_depth_and_covers_target(levels, q_net, fee):
    asks = tuple(PriceLevel(p, s) for p, s in levels)
    result = buy_plan_for_net_shares(asks, q_net, Outcome.YES, "a", fee)
    available = dict(levels)
    if result.plan is None:
        assert result.available_net < q_net
        return
    plan = result.plan
    assert plan.net_shares >= q_net
    for fill in plan.levels:
        assert fill.gross_shares <= available[fill.price]
    used = [f.price for f in plan.levels]
    assert used == sorted(used), "always walks from the cheapest price"
    assert min(used) == min(available)
    assert plan.gross_shares == sum((f.gross_shares for f in plan.levels), D(0))
    assert plan.worst_price == max(used)
    assert min(used) <= plan.vwap <= max(used)


@given(ask_levels, ask_levels, st.sampled_from([D(5), D(25), D(100)]), fees)
def test_passing_candidate_really_is_positive_after_all_costs(yes_levels, no_levels, q, fee):
    """Zero false positives: anything marked passed has positive matched payout minus cash."""
    spec = make_spec(fee=fee)
    yes = book_state(
        spec.yes_asset_id, asks=tuple((str(p), str(s)) for p, s in yes_levels), now=NOW
    )
    no = book_state(spec.no_asset_id, asks=tuple((str(p), str(s)) for p, s in no_levels), now=NOW)
    thresholds = ScanThresholds(D("0.01"), D("0.001"), 1000, 20)
    candidate = evaluate_pair(spec, yes, no, q, thresholds, NOW)
    if candidate.passed:
        matched = min(candidate.yes_leg.net_shares, candidate.no_leg.net_shares)
        spent = candidate.yes_leg.cash_spent + candidate.no_leg.cash_spent
        assert matched >= q
        assert matched - spent > 0
        assert candidate.edge_after_reserve >= D("0.01")
        assert (
            candidate.yes_leg.fee.fee_usd_equivalent + candidate.no_leg.fee.fee_usd_equivalent > 0
        )


fill_ops = st.lists(
    st.tuples(
        st.sampled_from([Outcome.YES, Outcome.NO]),
        st.integers(1, 99).map(lambda c: D(c) / 100),
        st.integers(1, 2000).map(lambda c: D(c) / 100),
        st.booleans(),
    ),
    max_size=25,
)


def _fill(index, outcome, price, gross, taker, spec=SPEC):
    role = LiquidityRole.TAKER if taker else LiquidityRole.MAKER
    net, fee, cash = buy_fill_economics(gross, price, spec.fee, role)
    return FillRecord(
        trade_id=f"t{index}",
        leg_key="l",
        intent_id=None,
        exchange_order_id=None,
        market_id=spec.market_id,
        asset_id=spec.asset_id(outcome),
        outcome=outcome,
        side=Side.BUY,
        role=role,
        price=price,
        gross_shares=gross,
        net_shares=net,
        fee_cash=fee.fee_cash,
        fee_shares=fee.fee_shares,
        cash_delta=-cash,
        status=FillStatus.MATCHED,
        simulated=True,
        ts_utc_ms=NOW,
    )


@settings(max_examples=75)
@given(fill_ops, st.booleans())
def test_ledger_conservation_through_fills_and_settlement(ops, up_wins):
    account = PaperAccount(D(100_000))
    fills = [_fill(i, *op) for i, op in enumerate(ops)]
    for fill in fills:
        assert account.apply_fill(fill)
        assert not account.apply_fill(fill), "duplicates never apply"
    inventory = account.inventory(SPEC)
    assert inventory.paired == min(inventory.yes_net, inventory.no_net)
    assert inventory.paired + inventory.unpaired_yes == inventory.yes_net
    assert inventory.paired + inventory.unpaired_no == inventory.no_net
    spent = sum((f.price * f.gross_shares + f.fee_cash for f in fills), D(0))
    assert account.cash_available == D(100_000) - spent

    winner = Outcome.YES if up_wins else Outcome.NO
    expected = inventory.yes_net if up_wins else inventory.no_net
    assert account.settle(SPEC, winner, NOW) == expected
    assert account.total_cash == D(100_000) - spent, "redeemable is not cash yet"
    account.redeem(SPEC, NOW)
    assert account.cash_available == D(100_000) - spent + expected
    assert account.flows[SPEC.market_id].realized_pnl == expected - spent
    assert account.ledger.conservation_errors() == []
    assert account.shares_net(SPEC.yes_asset_id) == 0
    assert account.shares_net(SPEC.no_asset_id) == 0


@settings(max_examples=50)
@given(fill_ops)
def test_merge_plus_settlement_equals_settlement_alone(ops):
    """Merging complete sets first must not change total value: nothing double counts."""
    fills = [_fill(i, *op) for i, op in enumerate(ops)]
    plain = PaperAccount(D(100_000))
    merged = PaperAccount(D(100_000))
    for fill in fills:
        plain.apply_fill(fill)
        merged.apply_fill(fill)
    paired = merged.inventory(SPEC).paired
    if paired > 0:
        merged.merge(SPEC, paired, NOW)
    for account in (plain, merged):
        account.settle(SPEC, Outcome.YES, NOW)
        account.redeem(SPEC, NOW)
        assert account.ledger.conservation_errors() == []
    assert plain.cash_available == merged.cash_available


book_ops = st.lists(
    st.tuples(st.sampled_from(["BUY", "SELL"]), prices, st.integers(0, 500).map(D)), max_size=40
)


@given(book_ops)
def test_book_levels_stay_positive_and_sorted(ops):
    manager = BookManager()
    book = manager.register("m", "a", D("0.01"))
    book.apply_snapshot([], [], 1)
    for side, price, size in ops:
        book.apply_level(side, price, size)
    state = book.state()
    assert all(lv.size > 0 for lv in state.bids + state.asks)
    assert [lv.price for lv in state.bids] == sorted((lv.price for lv in state.bids), reverse=True)
    assert [lv.price for lv in state.asks] == sorted(lv.price for lv in state.asks)


@given(
    book_ops,
    st.integers(1, 99).map(lambda c: D(c) / 100),
    st.integers(1, 99).map(lambda c: D(c) / 100),
)
def test_reported_top_is_never_contradicted_while_ready(ops, bid, ask):
    manager = BookManager()
    book = manager.register("m", "a", D("0.01"))
    book.apply_snapshot([], [], 1)
    for side, price, size in ops:
        book.apply_level(side, price, size)
    book.reconcile_top(bid, ask, 2)
    if book.validity.value == "ready":
        assert book.best_bid == bid and book.best_ask == ask and bid < ask


queue_ops = st.lists(
    st.one_of(
        st.tuples(st.just("trade"), st.integers(1, 200).map(D)),
        st.tuples(st.just("level"), st.integers(0, 400).map(D)),
    ),
    max_size=30,
)


@given(queue_ops, st.sampled_from(list(QueueScenario)))
def test_queue_never_overfills_or_goes_negative(ops, scenario):
    position = QueuePosition.join(D("0.40"), D(25), D(100), scenario)
    filled = D(0)
    for kind, amount in ops:
        if kind == "trade":
            filled += position.on_trade_at_price(amount)
        else:
            position.on_level_size(amount)
        assert position.ahead >= 0 and position.remaining >= 0
    assert filled + position.remaining == D(25)


@given(queue_ops)
def test_pessimistic_never_fills_more_than_optimistic(ops):
    results = {}
    for scenario in (QueueScenario.PESSIMISTIC, QueueScenario.OPTIMISTIC):
        position = QueuePosition.join(D("0.40"), D(25), D(100), scenario)
        total = D(0)
        for kind, amount in ops:
            if kind == "trade":
                total += position.on_trade_at_price(amount)
            else:
                position.on_level_size(amount)
        results[scenario] = total
    assert results[QueueScenario.PESSIMISTIC] <= results[QueueScenario.OPTIMISTIC]


PARAMS = QuoteParams(
    size_shares=D(10),
    margin=D("0.01"),
    pair_buffer=D("0.02"),
    min_price=D("0.05"),
    max_price=D("0.95"),
    max_order_notional=D(10),
    max_unpaired_notional=D(20),
    max_total_gross_notional=D(100),
    stop_seconds_before_close=20,
)


@settings(max_examples=100)
@given(
    st.integers(1, 97),
    st.integers(1, 3),
    st.integers(1, 97),
    st.integers(1, 3),
    st.integers(1, 30_000).map(lambda c: D(c) / 100),
    st.integers(1, 99).map(lambda c: D(c) / 100),
)
def test_quotes_always_within_bounds(y_bid, y_spread, n_bid, n_spread, cash, fair_yes):
    yes = book_state(
        SPEC.yes_asset_id,
        ((str(D(y_bid) / 100), "10"),),
        ((str(D(y_bid + y_spread) / 100), "10"),),
        now=NOW,
    )
    no = book_state(
        SPEC.no_asset_id,
        ((str(D(n_bid) / 100), "10"),),
        ((str(D(n_bid + n_spread) / 100), "10"),),
        now=NOW,
    )
    from bone_reaper.strategy.fair_value import FairValue

    account = PaperAccount(cash)
    fair = FairValue(fair_yes, D(1) - fair_yes, "t")
    plan = build_quote_plan(
        SPEC,
        yes,
        no,
        fair,
        PARAMS,
        account.inventory(SPEC),
        market_exposure(account, SPEC, ()),
        account.cash_available,
        NOW,
    )
    for quote in plan.quotes:
        own = yes if quote.outcome is Outcome.YES else no
        assert quote.price < own.best_ask, "post-only quote never crosses"
        assert quote.price % SPEC.tick_size == 0
        assert PARAMS.min_price <= quote.price <= PARAMS.max_price
        assert quote.size >= SPEC.min_order_shares
        assert quote.price * quote.size <= PARAMS.max_order_notional
    assert sum((q.price * q.size for q in plan.quotes), D(0)) <= cash
    if len(plan.quotes) == 2:
        assert sum(q.price for q in plan.quotes) <= D(1) - PARAMS.pair_buffer


@given(st.floats(allow_nan=True, allow_infinity=True))
def test_any_model_output_is_bounded_or_rejected(value):
    result = sanitize_probability(value)
    assert result is None or D("0.01") <= result <= D("0.99")


@given(
    st.lists(st.tuples(st.sampled_from([Outcome.YES, Outcome.NO]), prices, sizes), max_size=6),
)
def test_gross_exposure_counts_every_open_order(orders):
    records = [
        OrderRecord(make_intent(SPEC, f"i{n}", outcome, str(price), str(size)), OrderState.OPEN)
        for n, (outcome, price, size) in enumerate(orders)
    ]
    exposure = market_exposure(PaperAccount(D(1)), SPEC, records)
    assert exposure.open_buy_notional == sum((p * s for _, p, s in orders), D(0))
    # The average cost is a ratio, so allow for its last digit. Risk limits have far wider margins.
    assert exposure.worst_unpaired_notional <= exposure.open_buy_notional + D("1e-20")
