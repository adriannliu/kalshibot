from __future__ import annotations

from decimal import Decimal

from research.microstructure import (
    NS_PER_HOUR,
    NS_PER_SECOND,
    analyze_session,
    edge_bucket,
    group_by,
    merge_markets,
    to_centicents,
)
from tests.synthetic_session import SyntheticSession


def test_edge_buckets_partition_the_range():
    assert edge_bucket(-1) == "crossed"
    assert edge_bucket(0) == "0"
    assert edge_bucket(99) == "0"
    assert edge_bucket(100) == "1"
    assert edge_bucket(600) == "6-7"
    assert edge_bucket(799) == "6-7"
    assert edge_bucket(800) == "8-9"
    assert edge_bucket(1400) == "10-14"
    assert edge_bucket(2000) == "20+"


def test_time_weighted_edge_uses_valid_intervals_only(tmp_path):
    session = SyntheticSession(str(tmp_path))
    session.market("TEST-A", maker_multiplier="0", fee_regime="maker_free")
    session.subscribe(["TEST-A"])
    session.snapshot("TEST-A", [["0.4000", "100.00"]], [["0.5500", "80.00"]])
    session.advance(10.0).delta("TEST-A", "yes", "0.4200", "50.00")
    session.advance(30.0).delta("TEST-A", "yes", "0.4200", "-50.00")
    directory = session.write()

    stats = analyze_session(directory).markets["TEST-A"]

    assert stats.quotable_ns == 40 * NS_PER_SECOND
    assert stats.invalid_ns == 0
    assert stats.edge_time.weights == {500: 10 * NS_PER_SECOND, 300: 30 * NS_PER_SECOND}
    assert stats.edge_time.quantile(0.5) == 300


def test_gapped_time_is_excluded_from_edge_and_counted_as_invalid(tmp_path):
    session = SyntheticSession(str(tmp_path))
    session.market("TEST-A")
    session.subscribe(["TEST-A"])
    session.snapshot("TEST-A", [["0.4000", "100.00"]], [["0.5500", "80.00"]])
    session.advance(10.0).skip_seq(3).delta("TEST-A", "yes", "0.4000", "-10.00")
    session.advance(50.0).snapshot("TEST-A", [["0.4000", "100.00"]], [["0.5500", "80.00"]])
    session.advance(5.0).delta("TEST-A", "yes", "0.4000", "-10.00")
    directory = session.write()

    stats = analyze_session(directory).markets["TEST-A"]

    assert stats.invalid_ns == 50 * NS_PER_SECOND
    assert stats.quotable_ns == 15 * NS_PER_SECOND
    assert stats.edge_time.total_weight == 15 * NS_PER_SECOND


def build_one_cent_book(tmp_path, name, maker_multiplier, yes="0.4950", no="0.4950"):
    session = SyntheticSession(str(tmp_path), session_id=name)
    session.market(
        "TEST-A",
        maker_multiplier=maker_multiplier,
        fee_regime="maker_free" if maker_multiplier == "0" else "maker_charged",
    )
    session.subscribe(["TEST-A"])
    session.snapshot("TEST-A", [[yes, "100.00"]], [[no, "80.00"]])
    session.advance(10.0).delta("TEST-A", "yes", yes, "-100.00")
    return analyze_session(session.write()).markets["TEST-A"]


def test_maker_fees_consume_most_of_a_one_cent_edge_at_fifty(tmp_path):
    free = build_one_cent_book(tmp_path, "free", "0")
    charged = build_one_cent_book(tmp_path, "charged", "1")
    half = build_one_cent_book(tmp_path, "half", "0.5")

    assert free.breakeven_fraction == 1.0
    assert charged.breakeven_fraction == 1.0
    assert free.net_edge_time.quantile(0.5) == 100
    assert charged.net_edge_time.quantile(0.5) == 12
    assert half.net_edge_time.quantile(0.5) == 56


def test_a_touching_book_is_never_above_breakeven(tmp_path):
    charged = build_one_cent_book(tmp_path, "touching", "1", yes="0.5000", no="0.5000")
    free = build_one_cent_book(tmp_path, "touching-free", "0", yes="0.5000", no="0.5000")

    assert charged.breakeven_fraction == 0.0
    assert free.breakeven_fraction == 0.0
    assert charged.net_edge_time.quantile(0.5) == -88


def test_quote_lifetime_measures_best_price_persistence(tmp_path):
    session = SyntheticSession(str(tmp_path))
    session.market("TEST-A")
    session.subscribe(["TEST-A"])
    session.snapshot("TEST-A", [["0.4000", "100.00"]], [["0.5500", "80.00"]])
    session.advance(5.0).delta("TEST-A", "yes", "0.4000", "-50.00")
    session.advance(5.0).delta("TEST-A", "yes", "0.4100", "10.00")
    session.advance(2.0).delta("TEST-A", "yes", "0.4100", "-10.00")
    directory = session.write()

    stats = analyze_session(directory).markets["TEST-A"]

    assert stats.yes_quote_life.total == 2
    assert stats.yes_quote_life.mean() == 6.0
    assert stats.no_quote_life.total == 0
    assert stats.no_quote_life.censored == 1


def test_quote_lifetime_discards_intervals_broken_by_a_gap(tmp_path):
    session = SyntheticSession(str(tmp_path))
    session.market("TEST-A")
    session.subscribe(["TEST-A"])
    session.snapshot("TEST-A", [["0.4000", "100.00"]], [["0.5500", "80.00"]])
    session.advance(5.0).skip_seq(3).delta("TEST-A", "yes", "0.4000", "-10.00")
    directory = session.write()

    stats = analyze_session(directory).markets["TEST-A"]

    assert stats.yes_quote_life.total == 0
    assert stats.yes_quote_life.censored == 1
    assert stats.no_quote_life.censored == 1


def test_trade_arrival_is_conditioned_on_the_spread_at_the_trade(tmp_path):
    session = SyntheticSession(str(tmp_path))
    session.market("TEST-A")
    session.subscribe(["TEST-A"])
    session.snapshot("TEST-A", [["0.4000", "100.00"]], [["0.5500", "80.00"]])
    session.advance(1.0).trade("TEST-A", "0.4500", count="10.00")
    session.advance(9.0).delta("TEST-A", "yes", "0.4200", "50.00")
    session.advance(1.0).trade("TEST-A", "0.4400", count="4.00")
    session.advance(1.0).trade("TEST-A", "0.4400", count="6.00")
    directory = session.write()

    stats = analyze_session(directory).markets["TEST-A"]

    assert stats.trades == 3
    assert stats.volume_fp == 2000
    assert stats.buckets["5"].trades == 1
    assert stats.buckets["5"].valid_ns == 10 * NS_PER_SECOND
    assert stats.buckets["3"].trades == 2
    assert stats.buckets["3"].volume_fp == 1000
    assert stats.buckets["5"].arrivals_per_hour() == 360.0


def test_trades_arriving_without_a_valid_book_are_counted_separately(tmp_path):
    session = SyntheticSession(str(tmp_path))
    session.market("TEST-A")
    session.subscribe(["TEST-A"])
    session.advance(1.0).trade("TEST-A", "0.4500")
    directory = session.write()

    stats = analyze_session(directory).markets["TEST-A"]
    assert stats.trades == 1
    assert stats.trades_without_book == 1
    assert stats.buckets == {}


def test_merge_and_group_combine_sessions(tmp_path):
    def build(name):
        session = SyntheticSession(str(tmp_path), session_id=name)
        session.market("TEST-A", vertical="weather", fee_regime="maker_free")
        session.market("TEST-B", vertical="weather", fee_regime="maker_free")
        session.subscribe(["TEST-A", "TEST-B"])
        session.snapshot("TEST-A", [["0.4000", "100.00"]], [["0.5500", "80.00"]])
        session.snapshot("TEST-B", [["0.1000", "100.00"]], [["0.8500", "80.00"]])
        session.advance(10.0).delta("TEST-A", "yes", "0.4000", "-10.00")
        return analyze_session(session.write())

    merged = merge_markets([build("one"), build("two")])
    assert merged["TEST-A"].quotable_ns == 20 * NS_PER_SECOND

    grouped = group_by(merged, "vertical")
    assert set(grouped) == {"weather"}
    assert grouped["weather"].quotable_ns == 40 * NS_PER_SECOND
    assert grouped["weather"].report()["group"] == "weather"


def test_report_is_json_serializable(tmp_path):
    import json

    session = SyntheticSession(str(tmp_path))
    session.market("TEST-A")
    session.subscribe(["TEST-A"])
    session.snapshot("TEST-A", [["0.4000", "100.00"]], [["0.5500", "80.00"]])
    session.advance(10.0).trade("TEST-A", "0.4500")
    directory = session.write()

    report = analyze_session(directory).markets["TEST-A"].report()
    assert json.loads(json.dumps(report))["trades"] == 1
