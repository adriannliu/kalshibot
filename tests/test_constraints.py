from __future__ import annotations

from decimal import Decimal

from research.constraints import (
    KIND_COMPLEMENT,
    KIND_IMPLICATION,
    KIND_NORMALIZATION_NO,
    KIND_NORMALIZATION_YES,
    build_chains,
    build_exclusive_sets,
    scan_session,
)
from research.session import load_session_meta
from tests.synthetic_session import SyntheticSession


def ladder_session(tmp_path, name="ladder"):
    session = SyntheticSession(str(tmp_path), session_id=name)
    for strike in ("70", "75", "80"):
        session.market(
            "KXHIGH-26AUG20-B%s" % strike,
            series_ticker="KXHIGH",
            event_ticker="KXHIGH-26AUG20",
            strike_type="greater",
            floor_strike=strike,
            fee_regime="maker_free",
            taker_multiplier="1",
        )
    session.subscribe([m["ticker"] for m in session._markets])
    return session


def test_chains_order_above_strikes_by_decreasing_probability(tmp_path):
    session = ladder_session(tmp_path)
    session.snapshot("KXHIGH-26AUG20-B70", [["0.80", "10.00"]], [["0.19", "10.00"]])
    meta = load_session_meta(session.write())

    chains = build_chains(meta.markets)
    assert len(chains) == 1
    assert chains[0].tickers == (
        "KXHIGH-26AUG20-B70",
        "KXHIGH-26AUG20-B75",
        "KXHIGH-26AUG20-B80",
    )


def test_below_strikes_order_by_decreasing_probability(tmp_path):
    session = SyntheticSession(str(tmp_path))
    for strike in ("70", "80"):
        session.market(
            "KXLOW-B%s" % strike,
            event_ticker="KXLOW-EVT",
            strike_type="less",
            cap_strike=strike,
        )
    session.subscribe(["KXLOW-B70", "KXLOW-B80"])
    session.snapshot("KXLOW-B70", [["0.30", "10.00"]], [["0.69", "10.00"]])
    meta = load_session_meta(session.write())

    chains = build_chains(meta.markets)
    assert chains[0].tickers == ("KXLOW-B80", "KXLOW-B70")


def test_a_complement_violation_is_measured_net_of_taker_fees(tmp_path):
    session = SyntheticSession(str(tmp_path))
    session.market("TEST-A", taker_multiplier="1")
    session.subscribe(["TEST-A"])
    session.snapshot("TEST-A", [["0.6000", "50.00"]], [["0.4500", "30.00"]])
    session.advance(4.0).delta("TEST-A", "yes", "0.6000", "-50.00")
    session.advance(1.0).delta("TEST-A", "yes", "0.5000", "10.00")
    directory = session.write()

    stats = scan_session(directory).stats[KIND_COMPLEMENT]

    assert stats.occurrences == 1
    assert stats.profitable_occurrences == 1
    assert stats.best.duration_seconds == 4.0
    assert stats.best.peak.gross_per_contract == Decimal("0.05")
    assert stats.best.peak.size_fp == 3000
    assert stats.best.peak.gross == Decimal("1.50")
    assert stats.best.peak.fees == Decimal("1.0238")
    assert stats.best.peak.net == Decimal("0.4762")


def test_an_uncrossed_book_reports_no_complement_violation(tmp_path):
    session = SyntheticSession(str(tmp_path))
    session.market("TEST-A")
    session.subscribe(["TEST-A"])
    session.snapshot("TEST-A", [["0.4000", "50.00"]], [["0.5500", "30.00"]])
    session.advance(4.0).delta("TEST-A", "yes", "0.4000", "-1.00")
    directory = session.write()

    assert scan_session(directory).stats[KIND_COMPLEMENT].occurrences == 0


def test_a_gap_truncates_rather_than_extends_a_violation(tmp_path):
    session = SyntheticSession(str(tmp_path))
    session.market("TEST-A")
    session.subscribe(["TEST-A"])
    session.snapshot("TEST-A", [["0.6000", "50.00"]], [["0.4500", "30.00"]])
    session.advance(2.0).skip_seq(3).delta("TEST-A", "yes", "0.6000", "-1.00")
    session.advance(100.0).delta("TEST-A", "yes", "0.6000", "-1.00")
    directory = session.write()

    stats = scan_session(directory).stats[KIND_COMPLEMENT]
    assert stats.occurrences == 1
    assert stats.truncated == 1
    assert stats.best.duration_seconds == 2.0


def test_a_monotonicity_violation_across_a_ladder_is_detected(tmp_path):
    session = ladder_session(tmp_path)
    session.snapshot("KXHIGH-26AUG20-B70", [["0.8000", "10.00"]], [["0.1900", "10.00"]])
    session.snapshot("KXHIGH-26AUG20-B75", [["0.5000", "10.00"]], [["0.4900", "10.00"]])
    session.snapshot("KXHIGH-26AUG20-B80", [["0.8500", "20.00"]], [["0.1400", "20.00"]])
    session.advance(3.0).delta("KXHIGH-26AUG20-B80", "yes", "0.8500", "-20.00")
    directory = session.write()

    stats = scan_session(directory).stats[KIND_IMPLICATION]

    assert stats.occurrences == 2
    assert stats.best.tickers == ("KXHIGH-26AUG20-B75", "KXHIGH-26AUG20-B80")
    assert stats.best.peak.gross_per_contract == Decimal("0.34")
    assert stats.best.peak.size_fp == 1000
    assert stats.best.peak.net == Decimal("3.1357")


def test_a_consistent_ladder_reports_nothing(tmp_path):
    session = ladder_session(tmp_path)
    session.snapshot("KXHIGH-26AUG20-B70", [["0.8000", "10.00"]], [["0.1900", "10.00"]])
    session.snapshot("KXHIGH-26AUG20-B75", [["0.5000", "10.00"]], [["0.4900", "10.00"]])
    session.snapshot("KXHIGH-26AUG20-B80", [["0.2000", "10.00"]], [["0.7900", "10.00"]])
    session.advance(3.0).delta("KXHIGH-26AUG20-B80", "yes", "0.2000", "-1.00")
    directory = session.write()

    assert scan_session(directory).stats[KIND_IMPLICATION].occurrences == 0


def exclusive_session(tmp_path, count=3, captured=None, name="exclusive"):
    session = SyntheticSession(str(tmp_path), session_id=name)
    for index in range(captured if captured is not None else count):
        session.market(
            "KXWIN-O%d" % index,
            event_ticker="KXWIN-EVT",
            mutually_exclusive=True,
            event_market_count=count,
        )
    session.subscribe([m["ticker"] for m in session._markets])
    return session


def test_normalization_requires_the_whole_exclusive_set_to_be_captured(tmp_path):
    partial = exclusive_session(tmp_path, count=5, captured=3, name="partial")
    partial.snapshot("KXWIN-O0", [["0.3000", "10.00"]], [["0.6900", "10.00"]])
    assert build_exclusive_sets(load_session_meta(partial.write()).markets) == []

    whole = exclusive_session(tmp_path, count=3, captured=3, name="whole")
    whole.snapshot("KXWIN-O0", [["0.3000", "10.00"]], [["0.6900", "10.00"]])
    assert len(build_exclusive_sets(load_session_meta(whole.write()).markets)) == 1


def test_yes_legs_summing_below_one_dollar_is_a_normalization_violation(tmp_path):
    session = exclusive_session(tmp_path)
    session.snapshot("KXWIN-O0", [["0.2000", "10.00"]], [["0.7000", "10.00"]])
    session.snapshot("KXWIN-O1", [["0.2000", "10.00"]], [["0.7000", "10.00"]])
    session.snapshot("KXWIN-O2", [["0.2000", "10.00"]], [["0.7000", "10.00"]])
    session.advance(5.0).delta("KXWIN-O2", "no", "0.7000", "-10.00")
    directory = session.write()

    stats = scan_session(directory).stats[KIND_NORMALIZATION_YES]
    assert stats.occurrences == 1
    assert stats.best.peak.gross_per_contract == Decimal("0.10")
    assert stats.best.duration_seconds == 5.0


def test_yes_bids_summing_above_one_dollar_is_a_normalization_violation(tmp_path):
    session = exclusive_session(tmp_path)
    session.snapshot("KXWIN-O0", [["0.4000", "10.00"]], [["0.5000", "10.00"]])
    session.snapshot("KXWIN-O1", [["0.4000", "10.00"]], [["0.5000", "10.00"]])
    session.snapshot("KXWIN-O2", [["0.4000", "10.00"]], [["0.5000", "10.00"]])
    session.advance(5.0).delta("KXWIN-O2", "yes", "0.4000", "-10.00")
    directory = session.write()

    scanner = scan_session(directory)
    stats = scanner.stats[KIND_NORMALIZATION_NO]
    assert stats.occurrences == 1
    assert stats.best.peak.gross_per_contract == Decimal("0.20")
    assert scanner.stats[KIND_NORMALIZATION_YES].occurrences == 0


def test_a_consistent_exclusive_set_reports_nothing(tmp_path):
    session = exclusive_session(tmp_path)
    session.snapshot("KXWIN-O0", [["0.3000", "10.00"]], [["0.6600", "10.00"]])
    session.snapshot("KXWIN-O1", [["0.3000", "10.00"]], [["0.6600", "10.00"]])
    session.snapshot("KXWIN-O2", [["0.3000", "10.00"]], [["0.6600", "10.00"]])
    session.advance(5.0).delta("KXWIN-O2", "yes", "0.3000", "-1.00")
    directory = session.write()

    scanner = scan_session(directory)
    assert scanner.stats[KIND_NORMALIZATION_YES].occurrences == 0
    assert scanner.stats[KIND_NORMALIZATION_NO].occurrences == 0


def test_report_is_json_serializable(tmp_path):
    import json

    session = exclusive_session(tmp_path)
    session.snapshot("KXWIN-O0", [["0.2000", "10.00"]], [["0.7000", "10.00"]])
    session.snapshot("KXWIN-O1", [["0.2000", "10.00"]], [["0.7000", "10.00"]])
    session.snapshot("KXWIN-O2", [["0.2000", "10.00"]], [["0.7000", "10.00"]])
    session.advance(5.0).delta("KXWIN-O2", "no", "0.7000", "-1.00")
    directory = session.write()

    report = scan_session(directory).report()
    assert json.loads(json.dumps(report))["coverage"]["exclusive_sets"] == 1


def test_a_gross_violation_that_taker_fees_erase_is_recorded_but_not_profitable(tmp_path):
    session = exclusive_session(tmp_path, name="fee-eaten")
    session.snapshot("KXWIN-O0", [["0.3000", "10.00"]], [["0.6800", "10.00"]])
    session.snapshot("KXWIN-O1", [["0.3000", "10.00"]], [["0.6800", "10.00"]])
    session.snapshot("KXWIN-O2", [["0.3000", "10.00"]], [["0.6800", "10.00"]])
    session.advance(5.0).delta("KXWIN-O2", "no", "0.6800", "-10.00")
    directory = session.write()

    stats = scan_session(directory).stats[KIND_NORMALIZATION_YES]
    assert stats.occurrences == 1
    assert stats.profitable_occurrences == 0
    assert stats.best.peak.gross_per_contract == Decimal("0.04")
    assert stats.best.peak.net < 0
    assert stats.net_dollars_upper_bound == Decimal(0)
