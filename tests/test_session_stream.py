from __future__ import annotations

from decimal import Decimal

import pytest

from research.session import (
    BookChange,
    SessionIncompatible,
    SessionStream,
    TopOfBook,
    TradeTick,
    load_session_meta,
)
from tests.synthetic_session import SyntheticSession


def build(tmp_path, **market_kwargs):
    session = SyntheticSession(str(tmp_path))
    session.market("TEST-A", **market_kwargs)
    session.subscribe(["TEST-A"])
    return session


def test_top_of_book_edge_and_mid():
    top = TopOfBook(yes_bid=Decimal("0.40"), yes_size_fp=100, no_bid=Decimal("0.55"), no_size_fp=80)
    assert top.edge == Decimal("0.05")
    assert top.yes_ask == Decimal("0.45")
    assert top.mid == Decimal("0.425")
    assert top.two_sided_size_fp == 80


def test_stream_emits_book_changes_and_trades(tmp_path):
    session = build(tmp_path)
    session.snapshot("TEST-A", [["0.40", "100.00"]], [["0.55", "80.00"]])
    session.advance(1.0).trade("TEST-A", "0.45", taker_outcome_side="yes")
    session.advance(1.0).delta("TEST-A", "yes", "0.41", "20.00")
    directory = session.write()

    stream = SessionStream(directory)
    events = list(stream.events())

    books = [e for e in events if isinstance(e, BookChange)]
    trades = [e for e in events if isinstance(e, TradeTick)]
    assert len(books) == 2
    assert books[0].top.edge == Decimal("0.05")
    assert books[1].top.yes_bid == Decimal("0.41")
    assert len(trades) == 1
    assert trades[0].taker_bought_yes
    assert trades[0].top.yes_bid == Decimal("0.40")
    assert trades[0].count_fp == 1000


def test_sequence_gap_invalidates_every_book_in_the_subscription(tmp_path):
    session = SyntheticSession(str(tmp_path))
    session.market("TEST-A").market("TEST-B")
    session.subscribe(["TEST-A", "TEST-B"])
    session.snapshot("TEST-A", [["0.40", "100.00"]], [["0.55", "80.00"]])
    session.snapshot("TEST-B", [["0.10", "500.00"]], [["0.85", "300.00"]])
    session.advance(1.0).skip_seq(2).delta("TEST-A", "yes", "0.40", "-10.00")
    directory = session.write()

    stream = SessionStream(directory)
    events = list(stream.events())

    invalidations = [e for e in events if isinstance(e, BookChange) and not e.valid]
    assert sorted(e.ticker for e in invalidations) == ["TEST-A", "TEST-B"]
    assert stream.stats.gaps == 1
    assert all(e.valid for e in events[:2] if isinstance(e, BookChange))


def test_delta_after_gap_does_not_rebuild_the_book(tmp_path):
    session = build(tmp_path)
    session.snapshot("TEST-A", [["0.40", "100.00"]], [["0.55", "80.00"]])
    session.advance(1.0).skip_seq(3).delta("TEST-A", "yes", "0.40", "-10.00")
    session.advance(1.0).delta("TEST-A", "yes", "0.40", "-10.00")
    directory = session.write()

    events = list(SessionStream(directory).events())
    valid_after_gap = [
        e for e in events if isinstance(e, BookChange) and e.valid and e.mono_ns > 0
    ]
    assert valid_after_gap == []


def test_reconnect_invalidates_and_resets(tmp_path):
    session = build(tmp_path)
    session.snapshot("TEST-A", [["0.40", "100.00"]], [["0.55", "80.00"]])
    session.advance(1.0).reconnect()
    session.advance(1.0).subscribe(["TEST-A"])
    session.snapshot("TEST-A", [["0.42", "10.00"]], [["0.56", "10.00"]])
    directory = session.write()

    stream = SessionStream(directory)
    events = [e for e in stream.events() if isinstance(e, BookChange)]
    assert [e.valid for e in events] == [True, False, True]
    assert events[-1].top.yes_bid == Decimal("0.42")
    assert stream.stats.resets == 1


def test_use_yes_price_sessions_are_refused(tmp_path):
    session = build(tmp_path)
    session.snapshot("TEST-A", [["0.40", "100.00"]], [["0.55", "80.00"]])
    directory = session.write(use_yes_price=True)
    with pytest.raises(SessionIncompatible):
        load_session_meta(directory)


def test_market_meta_carries_fee_regime(tmp_path):
    session = build(
        tmp_path,
        fee_type="quadratic_with_maker_fees",
        fee_multiplier="0.5",
        maker_multiplier="0.5",
        fee_regime="maker_charged",
    )
    session.snapshot("TEST-A", [["0.40", "100.00"]], [["0.55", "80.00"]])
    directory = session.write()

    meta = load_session_meta(directory).market("TEST-A")
    assert meta.maker_multiplier == Decimal("0.5")
    assert meta.fee_regime == "maker_charged"
    assert meta.maker_pair_fee(Decimal("0.5"), Decimal("0.5")) == Decimal("0.5") * Decimal(
        "0.0175"
    ) * Decimal("0.5")


def test_unknown_market_defaults_to_charged_maker_fees(tmp_path):
    session = SyntheticSession(str(tmp_path))
    session.subscribe(["TEST-A"])
    session.snapshot("TEST-A", [["0.40", "100.00"]], [["0.55", "80.00"]])
    directory = session.write()

    meta = load_session_meta(directory).market("TEST-A")
    assert meta.maker_multiplier == Decimal("1")
    assert meta.fee_regime == "maker_charged"
