from __future__ import annotations

from decimal import Decimal

from research.adverse_selection import (
    HORIZON_SESSION_END,
    HORIZON_SETTLEMENT,
    analyze_session,
    group_by,
    merge_markets,
    price_bucket,
    size_bucket,
)
from tests.synthetic_session import SyntheticSession


def base_session(tmp_path, name="adverse"):
    session = SyntheticSession(str(tmp_path), session_id=name)
    session.market("TEST-A", vertical="weather", fee_regime="maker_free")
    session.subscribe(["TEST-A"])
    session.snapshot("TEST-A", [["0.4000", "100.00"]], [["0.5500", "80.00"]])
    return session


def test_price_and_size_buckets_are_stable_labels():
    assert price_bucket(Decimal("0.425")) == "0.40-0.50"
    assert price_bucket(Decimal("0")) == "0.00-0.10"
    assert price_bucket(Decimal("1")) == "0.90-1.00"
    assert size_bucket(999) == "<10"
    assert size_bucket(1000) == "10-99"
    assert size_bucket(10000) == "100+"


def test_a_taker_buying_yes_before_the_mid_rises_is_adverse_to_the_maker(tmp_path):
    session = base_session(tmp_path)
    session.advance(1.0).trade("TEST-A", "0.4500", taker_outcome_side="yes")
    session.advance(1.0).delta("TEST-A", "yes", "0.5000", "10.00")
    session.advance(60.0).delta("TEST-A", "yes", "0.5000", "-1.00")
    directory = session.write()

    market = analyze_session(directory).markets["TEST-A"]
    curve = market.curve

    assert market.trades_measured == 1
    assert curve.horizon("1s").mid_move.mean() == Decimal("0.05")
    assert curve.horizon("10s").mid_move.mean() == Decimal("0.05")
    assert curve.horizon("60s").mid_move.mean() == Decimal("0.05")
    assert curve.horizon("1s").maker_pnl.mean() == Decimal("-0.025")


def test_adverse_selection_eats_into_the_spread_the_maker_captured(tmp_path):
    session = base_session(tmp_path)
    session.advance(1.0).trade("TEST-A", "0.4000", taker_outcome_side="no")
    session.advance(1.0).delta("TEST-A", "no", "0.5800", "10.00")
    session.advance(2.0).delta("TEST-A", "no", "0.5800", "-1.00")
    directory = session.write()

    horizon = analyze_session(directory).markets["TEST-A"].curve.horizon("1s")
    spread_captured = Decimal("0.425") - Decimal("0.40")

    assert horizon.mid_move.mean() == Decimal("0.015")
    assert horizon.maker_pnl.mean() == Decimal("0.01")
    assert horizon.maker_pnl.mean() == spread_captured - horizon.mid_move.mean()


def test_a_maker_keeps_the_spread_when_the_mid_does_not_move(tmp_path):
    session = base_session(tmp_path)
    session.advance(1.0).trade("TEST-A", "0.4000", taker_outcome_side="no")
    session.advance(2.0).delta("TEST-A", "yes", "0.3000", "1.00")
    directory = session.write()

    curve = analyze_session(directory).markets["TEST-A"].curve
    assert curve.horizon("1s").mid_move.mean() == Decimal("0")
    assert curve.horizon("1s").maker_pnl.mean() == Decimal("0.025")


def test_each_horizon_is_sampled_at_its_own_deadline(tmp_path):
    session = base_session(tmp_path)
    session.advance(1.0).trade("TEST-A", "0.4500", taker_outcome_side="yes")
    session.advance(1.5).delta("TEST-A", "yes", "0.4200", "10.00")
    session.advance(20.0).delta("TEST-A", "yes", "0.4400", "10.00")
    session.advance(60.0).delta("TEST-A", "yes", "0.4400", "-1.00")
    directory = session.write()

    curve = analyze_session(directory).markets["TEST-A"].curve
    assert curve.horizon("1s").mid_move.mean() == Decimal("0")
    assert curve.horizon("10s").mid_move.mean() == Decimal("0.01")
    assert curve.horizon("60s").mid_move.mean() == Decimal("0.02")


def test_a_horizon_falling_in_a_gap_is_unresolved_not_guessed(tmp_path):
    session = base_session(tmp_path)
    session.advance(1.0).trade("TEST-A", "0.4500", taker_outcome_side="yes")
    session.advance(0.5).skip_seq(3).delta("TEST-A", "yes", "0.4000", "-1.00")
    session.advance(2.0).delta("TEST-A", "yes", "0.4000", "-1.00")
    directory = session.write()

    curve = analyze_session(directory).markets["TEST-A"].curve
    assert curve.horizon("1s").mid_move.count == 0
    assert curve.horizon("1s").unresolved_book_invalid == 1


def test_a_horizon_past_the_end_of_the_stream_is_not_resolved(tmp_path):
    session = base_session(tmp_path)
    session.advance(1.0).trade("TEST-A", "0.4500", taker_outcome_side="yes")
    session.advance(2.0).delta("TEST-A", "yes", "0.4000", "-1.00")
    directory = session.write()

    curve = analyze_session(directory).markets["TEST-A"].curve
    assert curve.horizon("1s").mid_move.count == 1
    assert curve.horizon("10s").unresolved_stream_ended == 1
    assert curve.horizon("60s").unresolved_stream_ended == 1


def test_session_end_and_settlement_horizons(tmp_path):
    session = base_session(tmp_path)
    session.advance(1.0).trade("TEST-A", "0.4500", taker_outcome_side="yes")
    session.advance(2.0).delta("TEST-A", "yes", "0.4400", "10.00")
    directory = session.write()

    curve = analyze_session(
        directory, settlements={"TEST-A": Decimal("1")}
    ).markets["TEST-A"].curve

    assert curve.horizon(HORIZON_SESSION_END).mid_move.mean() == Decimal("0.02")
    assert curve.horizon(HORIZON_SETTLEMENT).mid_move.mean() == Decimal("0.575")
    assert curve.horizon(HORIZON_SETTLEMENT).maker_pnl.mean() == Decimal("-0.55")


def test_trades_without_direction_are_never_signed(tmp_path):
    session = base_session(tmp_path)
    session.advance(1.0).trade("TEST-A", "0.4500", taker_outcome_side="")
    session.advance(2.0).delta("TEST-A", "yes", "0.4400", "10.00")
    directory = session.write()

    market = analyze_session(directory).markets["TEST-A"]
    assert market.trades_seen == 1
    assert market.trades_measured == 0
    assert market.trades_without_direction == 1
    assert market.curve.horizons == {}


def test_buckets_and_grouping(tmp_path):
    session = base_session(tmp_path)
    session.advance(1.0).trade("TEST-A", "0.4500", count="200.00", taker_outcome_side="yes")
    session.advance(2.0).delta("TEST-A", "yes", "0.4400", "10.00")
    directory = session.write()

    market = analyze_session(directory).markets["TEST-A"]
    assert set(market.by_price) == {"0.40-0.50"}
    assert set(market.by_size) == {"100+"}

    merged = merge_markets([analyze_session(directory), analyze_session(directory)])
    assert merged["TEST-A"].trades_measured == 2

    grouped = group_by(merged, "fee_regime")
    assert grouped["maker_free"].curve.horizon("1s").mid_move.count == 2


def test_report_is_json_serializable(tmp_path):
    import json

    session = base_session(tmp_path)
    session.advance(1.0).trade("TEST-A", "0.4500", taker_outcome_side="yes")
    session.advance(2.0).delta("TEST-A", "yes", "0.4400", "10.00")
    directory = session.write()

    report = analyze_session(directory).markets["TEST-A"].report()
    assert json.loads(json.dumps(report))["trades_measured"] == 1


def test_maker_fees_are_netted_at_the_fill_price(tmp_path):
    def build(name, maker_multiplier):
        session = SyntheticSession(str(tmp_path), session_id=name)
        session.market(
            "TEST-A",
            maker_multiplier=maker_multiplier,
            fee_regime="maker_free" if maker_multiplier == "0" else "maker_charged",
        )
        session.subscribe(["TEST-A"])
        session.snapshot("TEST-A", [["0.4000", "100.00"]], [["0.5500", "80.00"]])
        session.advance(1.0).trade("TEST-A", "0.4000", count="10.00", taker_outcome_side="no")
        session.advance(2.0).delta("TEST-A", "yes", "0.3000", "1.00")
        return analyze_session(session.write()).markets["TEST-A"].curve.horizon("1s")

    free = build("fee-free", "0")
    charged = build("fee-charged", "1")

    assert free.maker_pnl.mean() == Decimal("0.025")
    assert free.net_maker_pnl.mean() == Decimal("0.025")
    assert free.maker_fees == Decimal("0")

    leg_fee = Decimal("0.0175") * Decimal("0.40") * Decimal("0.60")
    assert charged.maker_pnl.mean() == Decimal("0.025")
    assert charged.net_maker_pnl.mean() == Decimal("0.025") - leg_fee
    assert charged.maker_fees == leg_fee * 10
    assert charged.contracts_fp == 1000
