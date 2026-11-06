"""P2: fees, depth walking, classification and thresholds. Includes the mandated economic cases."""

from __future__ import annotations

import json
from decimal import Decimal as D

import pytest

from bone_reaper.research.scan_runner import ScanAccumulator
from bone_reaper.strategy.fees import buy_fill_economics, fee_usd, round_fee, sell_fill_economics
from bone_reaper.strategy.pair_scanner import (
    REJECT_BELOW_MIN,
    REJECT_EDGE,
    REJECT_EXPIRED,
    REJECT_EXPIRY,
    REJECT_LIQUIDITY,
    REJECT_NEGATIVE,
    REJECT_NOT_STARTED,
    REJECT_OFF_TICK,
    REJECT_ROI,
    REJECT_ROUTE,
    ScanThresholds,
    buy_plan_for_net_shares,
    evaluate_pair,
    scan_market,
)
from bone_reaper.types import (
    BookValidity,
    FeeCollection,
    FeeParams,
    LiquidityRole,
    Outcome,
    PriceLevel,
    to_jsonable,
)
from helpers import NO, START, YES, book_state, crypto_fee, make_spec

NOW = START + 1000
Y, N = f"{YES}0", f"{NO}0"
FEE = crypto_fee()
NO_FEE = FeeParams(False, D(0), D(1), True, D(0), "none")


def thresholds(**changes) -> ScanThresholds:
    fields = {
        "min_net_edge_usd": D("0.50"),
        "min_net_roi": D("0.01"),
        "max_book_age_ms": 1000,
        "stop_seconds_before_close": 20,
        "execution_reserve_per_share": D("0.002"),
    }
    fields.update(changes)
    return ScanThresholds(**fields)


def books(yes_asks, no_asks, **kwargs):
    return book_state(Y, asks=yes_asks, **kwargs), book_state(N, asks=no_asks, **kwargs)


class TestFees:
    @pytest.mark.parametrize(
        ("price", "expected"),
        [
            ("0.01", "0.0693"),
            ("0.05", "0.3325"),
            ("0.25", "1.3125"),
            ("0.50", "1.75"),
            ("0.75", "1.3125"),
        ],
    )
    def test_documented_crypto_curve_for_100_shares(self, price, expected):
        assert fee_usd(D(100), D(price), FEE) == D(expected)

    def test_documented_table_rounding_at_display_precision(self):
        """The docs table shows 2-decimal values: 0.05 -> $0.33, 0.50 -> $1.75."""
        assert round(fee_usd(D(100), D("0.05"), FEE), 2) == D("0.33")
        assert round(fee_usd(D(100), D("0.50"), FEE), 2) == D("1.75")

    def test_symmetric_around_half(self):
        assert fee_usd(D(100), D("0.30"), FEE) == fee_usd(D(100), D("0.70"), FEE)

    def test_rounds_to_five_decimals_and_tiny_fee_is_zero(self):
        assert round_fee(D("0.000014")) == D("0.00001")
        assert round_fee(D("0.000016")) == D("0.00002")
        assert fee_usd(D("0.01"), D("0.001"), FEE) == D(0)

    def test_fee_exponent_is_respected(self):
        squared = FeeParams(True, D("0.25"), D(2), True, D(0), "x")
        assert fee_usd(D(100), D("0.5"), squared) == D("1.5625")

    def test_disabled_and_geopolitics_style_zero_rate(self):
        assert fee_usd(D(100), D("0.5"), NO_FEE) == D(0)
        zero_rate = FeeParams(True, D(0), D(1), True, D(0), "x")
        assert fee_usd(D(100), D("0.5"), zero_rate) == D(0)

    def test_invalid_inputs(self):
        with pytest.raises(ValueError):
            fee_usd(D(1), D(1), FEE)
        with pytest.raises(ValueError):
            fee_usd(D(-1), D("0.5"), FEE)

    def test_taker_buy_fee_in_shares_reduces_net(self):
        net, fee, cash = buy_fill_economics(D(100), D("0.50"), FEE, LiquidityRole.TAKER)
        assert fee.fee_shares == D("3.5") and fee.fee_cash == 0
        assert net == D("96.5") and cash == D(50)

    def test_taker_buy_fee_on_top_keeps_gross_shares(self):
        cash_fee = crypto_fee(FeeCollection.CASH_ON_TOP)
        net, fee, cash = buy_fill_economics(D(100), D("0.50"), cash_fee, LiquidityRole.TAKER)
        assert net == D(100) and fee.fee_cash == D("1.75") and cash == D("51.75")

    def test_maker_pays_nothing(self):
        net, fee, cash = buy_fill_economics(D(100), D("0.50"), FEE, LiquidityRole.MAKER)
        assert net == D(100) and fee.fee_usd_equivalent == 0 and cash == D(50)

    def test_sell_fee_is_cash(self):
        fee, cash = sell_fill_economics(D(100), D("0.50"), FEE, LiquidityRole.TAKER)
        assert fee.fee_cash == D("1.75") and cash == D("48.25")


class TestMandatedEconomicCases:
    def test_case1_046_051_is_negative_after_fees(self):
        """0.46 + 0.51 shows 0.03 gross and must not signal as positive net arbitrage."""
        yes, no = books((("0.46", "1000"),), (("0.51", "1000"),))
        candidate = evaluate_pair(make_spec(), yes, no, D(100), thresholds(), NOW)
        assert candidate.gross_edge == D("3.00")
        # The PRD's 0.034881 per pair is per gross share pair.
        per_100_gross = fee_usd(D(100), D("0.46"), FEE) + fee_usd(D(100), D("0.51"), FEE)
        assert per_100_gross == D("3.4881")
        # Acquiring 100 net shares takes more gross shares, so the real fee is a little higher.
        assert candidate.fee_usd_equivalent > per_100_gross
        assert candidate.indicative_edge < 0
        assert not candidate.passed and REJECT_NEGATIVE in candidate.reasons
        assert candidate.classification == "rejected"

    def test_case1_also_negative_with_cash_collected_fees(self):
        spec = make_spec(fee=crypto_fee(FeeCollection.CASH_ON_TOP))
        yes, no = books((("0.46", "1000"),), (("0.51", "1000"),))
        candidate = evaluate_pair(spec, yes, no, D(100), thresholds(), NOW)
        assert not candidate.passed and candidate.indicative_edge < 0

    def test_case3_fee_in_shares_differs_per_leg_and_is_recorded(self):
        """Different prices burn different share fees: net holdings are not gross/gross."""
        plan_y = buy_plan_for_net_shares(
            (PriceLevel(D("0.10"), D(1000)),), D(50), Outcome.YES, Y, FEE
        ).plan
        plan_n = buy_plan_for_net_shares(
            (PriceLevel(D("0.85"), D(1000)),), D(50), Outcome.NO, N, FEE
        ).plan
        assert plan_y.gross_shares != plan_n.gross_shares
        assert plan_y.net_shares != plan_n.net_shares
        assert plan_y.fee.fee_shares > plan_n.fee.fee_shares
        for plan in (plan_y, plan_n):
            assert plan.net_shares >= D(50)
            assert plan.gross_shares == plan.net_shares + plan.fee.fee_shares


class TestDepthWalking:
    def test_multi_level_vwap_and_worst_price(self):
        asks = (PriceLevel(D("0.41"), D("10")), PriceLevel(D("0.40"), D("10")))
        plan = buy_plan_for_net_shares(asks, D(15), Outcome.YES, Y, NO_FEE).plan
        assert [lv.price for lv in plan.levels] == [D("0.40"), D("0.41")], "cheapest first"
        assert plan.gross_shares == D(15) and plan.worst_price == D("0.41")
        assert plan.notional == D("10") * D("0.40") + D(5) * D("0.41")
        assert plan.vwap == plan.notional / D(15)

    def test_partial_liquidity_rejected_with_available_amount(self):
        result = buy_plan_for_net_shares(
            (PriceLevel(D("0.40"), D(3)),), D(10), Outcome.YES, Y, NO_FEE
        )
        assert result.plan is None and result.reason == REJECT_LIQUIDITY
        assert result.available_net == D(3)

    def test_fee_can_make_visible_depth_insufficient(self):
        """Exactly 100 gross available cannot provide 100 net once fees are taken in shares."""
        asks = (PriceLevel(D("0.50"), D(100)),)
        assert buy_plan_for_net_shares(asks, D(100), Outcome.YES, Y, FEE).plan is None
        assert buy_plan_for_net_shares(asks, D(100), Outcome.YES, Y, NO_FEE).plan is not None

    def test_gross_rounded_up_to_lot(self):
        plan = buy_plan_for_net_shares(
            (PriceLevel(D("0.50"), D(1000)),), D(10), Outcome.YES, Y, FEE
        ).plan
        assert (plan.gross_shares / D("0.01")) % 1 == 0

    def test_q_must_be_positive(self):
        with pytest.raises(ValueError):
            buy_plan_for_net_shares((), D(0), Outcome.YES, Y, FEE)


class TestClassifier:
    def test_true_positive_passes_but_is_only_a_candidate(self):
        yes, no = books((("0.30", "1000"),), (("0.30", "1000"),))
        candidate = evaluate_pair(make_spec(), yes, no, D(100), thresholds(), NOW)
        assert candidate.passed and candidate.classification == "candidate_executable"
        assert candidate.edge_after_reserve > D("0.5") and candidate.roi > D("0.01")
        assert REJECT_ROUTE in candidate.reasons, "no route exists to execute it by default"

    def test_every_candidate_has_adverse_cases_and_funding(self):
        yes, no = books((("0.30", "1000"),), (("0.30", "1000"),))
        candidate = evaluate_pair(make_spec(), yes, no, D(100), thresholds(), NOW)
        names = {c.name for c in candidate.adverse_cases}
        assert {
            "one_tick_worse_second_leg",
            "second_leg_fails_yes_held",
            "second_leg_fails_no_held",
            "no_rebates_after_reserve",
        } == names
        failed = next(c for c in candidate.adverse_cases if c.name == "second_leg_fails_yes_held")
        assert failed.edge == -candidate.yes_leg.cash_spent, "one-sided outcome is a full-leg risk"
        assert all(c.funding_required > 0 for c in candidate.adverse_cases)
        worse = next(c for c in candidate.adverse_cases if c.name == "one_tick_worse_second_leg")
        assert worse.edge < candidate.indicative_edge

    def test_no_text_calls_requests_atomic(self):
        yes, no = books((("0.30", "1000"),), (("0.30", "1000"),))
        candidate = evaluate_pair(make_spec(), yes, no, D(100), thresholds(), NOW)
        text = json.dumps(to_jsonable(candidate)).lower()
        assert "confirmed_lock" not in text
        assert "not atomic" in text

    def test_payout_counts_matched_net_shares_only(self):
        yes, no = books((("0.10", "1000"),), (("0.85", "1000"),))
        candidate = evaluate_pair(make_spec(), yes, no, D(50), thresholds(), NOW)
        matched = min(candidate.yes_leg.net_shares, candidate.no_leg.net_shares)
        assert candidate.indicative_edge == matched - candidate.capital

    @pytest.mark.parametrize(
        ("kwargs", "reason"),
        [
            ({"min_net_edge_usd": D("100")}, REJECT_EDGE),
            ({"min_net_roi": D("0.9")}, REJECT_ROI),
        ],
    )
    def test_threshold_reasons(self, kwargs, reason):
        yes, no = books((("0.30", "1000"),), (("0.30", "1000"),))
        candidate = evaluate_pair(make_spec(), yes, no, D(100), thresholds(**kwargs), NOW)
        assert not candidate.passed and reason in candidate.reasons

    def test_stale_and_invalid_books_rejected(self):
        yes, no = books((("0.30", "1000"),), (("0.30", "1000"),))
        late = evaluate_pair(make_spec(), yes, no, D(100), thresholds(), NOW + 5000)
        assert not late.passed
        assert "yes:book_stale" in late.reasons and "no:book_stale" in late.reasons
        bad, good = books((("0.30", "1000"),), (("0.30", "1000"),), validity=BookValidity.INVALID)
        invalid = evaluate_pair(make_spec(), bad, good, D(100), thresholds(), NOW)
        assert not invalid.passed
        syncing, ok = books((("0.30", "1000"),), (("0.30", "1000"),), validity=BookValidity.SYNCING)
        assert not evaluate_pair(make_spec(), syncing, ok, D(100), thresholds(), NOW).passed

    def test_expiry_status_and_lot_rejections(self):
        spec = make_spec()
        near = spec.end_utc_ms - 10_000
        yes, no = books((("0.30", "1000"),), (("0.30", "1000"),), now=near)
        assert REJECT_EXPIRY in evaluate_pair(spec, yes, no, D(100), thresholds(), near).reasons
        after = spec.end_utc_ms + 1
        yes, no = books((("0.30", "1000"),), (("0.30", "1000"),), now=after)
        assert REJECT_EXPIRED in evaluate_pair(spec, yes, no, D(100), thresholds(), after).reasons
        before = START - 1000
        yes, no = books((("0.30", "1000"),), (("0.30", "1000"),), now=before)
        assert (
            REJECT_NOT_STARTED in evaluate_pair(spec, yes, no, D(100), thresholds(), before).reasons
        )
        yes, no = books((("0.30", "1000"),), (("0.30", "1000"),))
        assert REJECT_BELOW_MIN in evaluate_pair(spec, yes, no, D(1), thresholds(), NOW).reasons
        closed = make_spec(accepting_orders=False)
        assert not evaluate_pair(closed, yes, no, D(100), thresholds(), NOW).passed

    def test_off_tick_and_live_tick(self):
        yes, no = books((("0.305", "1000"),), (("0.30", "1000"),))
        assert (
            REJECT_OFF_TICK
            in evaluate_pair(make_spec(), yes, no, D(100), thresholds(), NOW).reasons
        )
        fine, other = books((("0.305", "1000"),), (("0.30", "1000"),), tick="0.001")
        result = evaluate_pair(make_spec(), fine, other, D(100), thresholds(), NOW)
        assert REJECT_OFF_TICK not in result.reasons, "the venue narrows the tick near extremes"

    def test_insufficient_either_leg_rejected_and_no_top_only_assumption(self):
        yes, no = books((("0.30", "10"),), (("0.30", "1000"),))
        candidate = evaluate_pair(make_spec(), yes, no, D(100), thresholds(), NOW)
        assert f"yes:{REJECT_LIQUIDITY}" in candidate.reasons and not candidate.passed

    def test_wrong_asset_books_rejected(self):
        yes = book_state("other", asks=(("0.30", "1000"),))
        no = book_state(N, asks=(("0.30", "1000"),))
        assert (
            "book_asset_mismatch"
            in evaluate_pair(make_spec(), yes, no, D(100), thresholds(), NOW).reasons
        )

    def test_reproducible_and_scan_covers_sizes(self):
        yes, no = books((("0.30", "1000"),), (("0.30", "1000"),))
        first = scan_market(make_spec(), yes, no, [D(5), D(100)], thresholds(), NOW)
        second = scan_market(make_spec(), yes, no, [D(5), D(100)], thresholds(), NOW)
        assert first == second and [c.q_net for c in first] == [D(5), D(100)]


class TestScanAccumulator:
    def test_opportunity_duration_tracking(self):
        accumulator = ScanAccumulator()
        yes, no = books((("0.30", "1000"),), (("0.30", "1000"),))
        spec = make_spec()
        accumulator.add(evaluate_pair(spec, yes, no, D(100), thresholds(), NOW))
        later = NOW + 500
        yes2, no2 = books((("0.30", "1000"),), (("0.30", "1000"),), now=later)
        accumulator.add(evaluate_pair(spec, yes2, no2, D(100), thresholds(), later))
        gone = NOW + 900
        yes3, no3 = books((("0.60", "1000"),), (("0.60", "1000"),), now=gone)
        accumulator.add(evaluate_pair(spec, yes3, no3, D(100), thresholds(), gone))
        summary = accumulator.summary()
        assert summary["opportunities"] == 1
        assert summary["opportunity_duration_ms_max"] == 500
        assert summary["positive_gross_edge"] == 2
        assert summary["by_q_net"]["100"]["evaluated"] == 3
