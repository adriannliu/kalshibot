from decimal import Decimal

import pytest

from config import exchange


def test_taker_fee_matches_published_formula():
    assert exchange.taker_fee(Decimal("0.5"), 100 * exchange.COUNT_SCALE) == Decimal("1.7500")


def test_maker_round_trip_at_fifty_cents_is_point_875_cents():
    cost = exchange.maker_round_trip_cost_per_contract(Decimal("0.5"), Decimal("1"))
    assert cost == Decimal("0.008750")


def schedule(ticker, fee_type, multiplier):
    return exchange.SeriesFeeSchedule(
        series_ticker=ticker, fee_type=fee_type, fee_multiplier=Decimal(multiplier)
    )


def test_fee_type_not_multiplier_decides_whether_maker_fees_are_charged():
    quadratic = schedule("KXBTCD", "quadratic", "1")
    with_maker = schedule("KXNFLGAME", "quadratic_with_maker_fees", "1")

    assert quadratic.fee_multiplier == Decimal("1")
    assert quadratic.maker_multiplier == Decimal("0")
    assert quadratic.taker_multiplier == Decimal("1")
    assert with_maker.maker_multiplier == Decimal("1")


def test_fee_multiplier_scales_both_legs():
    half = schedule("KXMLBGAME", "quadratic_with_maker_fees", "0.5")
    assert half.maker_multiplier == Decimal("0.5")
    assert half.taker_multiplier == Decimal("0.5")
    assert half.maker_round_trip_cost(Decimal("0.5")) == Decimal("0.0043750")


def test_regimes_are_disjoint_and_zero_fee_wins():
    assert schedule("A", "quadratic_with_maker_fees", "1").regime == exchange.REGIME_MAKER_CHARGED
    assert schedule("B", "quadratic", "1").regime == exchange.REGIME_MAKER_FREE
    assert schedule("C", "quadratic", "0").regime == exchange.REGIME_ZERO_FEE
    assert schedule("D", "quadratic_with_maker_fees", "0").regime == exchange.REGIME_ZERO_FEE


def test_unrecognized_fee_type_is_treated_as_charging_maker_fees():
    future = schedule("KXNEW", "quadratic_with_maker_fees_v2", "1")
    assert not future.recognized
    assert future.maker_multiplier == Decimal("1")
    assert future.regime == exchange.REGIME_MAKER_CHARGED


def test_combo_maker_fee_type_is_recognized_and_charges_maker_fees():
    combo = schedule("KXMVECROSSCATEGORY", "quadratic_with_combo_maker_fees", "1")
    assert combo.recognized
    assert combo.maker_multiplier == Decimal("1")
    assert combo.regime == exchange.REGIME_MAKER_CHARGED


def test_only_plain_quadratic_waives_maker_fees():
    assert exchange.FEE_TYPES_WITHOUT_MAKER_FEES == frozenset({"quadratic"})


def test_unknown_series_resolves_conservatively():
    table = exchange.FeeScheduleTable(schedules={}, source="test", fetched_at_ms=0)
    assert table.maker_multiplier("ANYTHING") == exchange.MAKER_MULTIPLIER_WHEN_UNKNOWN
    assert table.regime("ANYTHING") == exchange.REGIME_MAKER_CHARGED
    assert not table.is_known("ANYTHING")

    with pytest.raises(exchange.UnknownSeriesFeeSchedule):
        table.schedule_for("ANYTHING")


def test_conservative_fee_schedule_never_reports_free_maker():
    table = exchange.ConservativeFeeSchedule(reason="fee table unavailable")
    assert table.maker_multiplier("ANYTHING") == Decimal("1")
    assert not table.is_known("ANYTHING")


def test_fees_round_up_to_a_centicent():
    fee = exchange.taker_fee(Decimal("0.37"), 1 * exchange.COUNT_SCALE)
    assert fee == fee.quantize(exchange.CENTICENT)
    exact = exchange.TAKER_FEE_RATE * Decimal("0.37") * Decimal("0.63")
    assert fee >= exact


def test_zero_maker_multiplier_produces_zero_fee():
    assert exchange.maker_fee(Decimal("0.5"), 100 * exchange.COUNT_SCALE, Decimal("0")) == Decimal("0")


def test_series_ticker_extraction():
    assert exchange.series_ticker_of("KXBTCD-25AUG0517-T114999.99") == "KXBTCD"
    assert exchange.series_ticker_of("HIGHNY-22DEC23-B53.5") == "HIGHNY"


def test_no_float_leaks_from_fee_paths():
    fee = exchange.taker_fee(Decimal("0.5"), 3 * exchange.COUNT_SCALE)
    assert isinstance(fee, Decimal)
    assert isinstance(exchange.contracts_from_fp(1300), Decimal)
