from decimal import Decimal

import pytest

from feed.book import (
    BookIntegrityError,
    BookSet,
    BookState,
    GapKind,
    OrderBook,
    Subscription,
)

SNAPSHOT = {
    "market_ticker": "T",
    "yes_dollars_fp": [["0.0800", "300.00"], ["0.2200", "333.00"]],
    "no_dollars_fp": [["0.5400", "20.00"], ["0.5600", "146.00"]],
}


def make_book():
    book = OrderBook("T")
    book.apply_snapshot(SNAPSHOT, 2)
    return book


def test_snapshot_sets_state_and_levels():
    book = make_book()
    assert book.state is BookState.VALID
    assert book.best_yes_bid() == (Decimal("0.22"), 33300)
    assert book.best_no_bid() == (Decimal("0.56"), 14600)


def test_edge_is_one_minus_the_two_bids():
    book = make_book()
    assert book.edge_dollars() == Decimal("1") - (Decimal("0.22") + Decimal("0.56"))


def test_delta_removes_exhausted_level():
    book = make_book()
    book.apply_delta({"side": "yes", "price_dollars": "0.2200", "delta_fp": "-333.00"}, 3)
    assert Decimal("0.22") not in book.yes
    assert book.best_yes_bid() == (Decimal("0.08"), 30000)


def test_negative_level_raises_rather_than_clamping():
    book = make_book()
    with pytest.raises(BookIntegrityError):
        book.apply_delta({"side": "no", "price_dollars": "0.5400", "delta_fp": "-21.00"}, 3)


def test_delta_before_snapshot_is_refused():
    book = OrderBook("T")
    with pytest.raises(BookIntegrityError):
        book.apply_delta({"side": "yes", "price_dollars": "0.5", "delta_fp": "1.00"}, 1)


def test_unknown_side_is_refused():
    book = make_book()
    with pytest.raises(BookIntegrityError):
        book.apply_delta({"side": "maybe", "price_dollars": "0.5", "delta_fp": "1.00"}, 3)


def test_invalidate_clears_levels_and_state():
    book = make_book()
    book.invalidate()
    assert book.state is BookState.AWAITING_SNAPSHOT
    assert book.yes == {} and book.no == {}


def test_digest_is_order_independent_and_value_sensitive():
    left = OrderBook("T")
    left.apply_snapshot(SNAPSHOT, 1)
    right = OrderBook("T")
    right.apply_snapshot(
        {
            "market_ticker": "T",
            "yes_dollars_fp": [["0.2200", "333.00"], ["0.0800", "300.00"]],
            "no_dollars_fp": [["0.5600", "146.00"], ["0.5400", "20.00"]],
        },
        1,
    )
    assert left.digest() == right.digest()

    right.apply_delta({"side": "yes", "price_dollars": "0.0800", "delta_fp": "-1.00"}, 2)
    assert left.digest() != right.digest()


def test_sequence_tracking_detects_missed_and_regressed():
    sub = Subscription(1, "orderbook_delta", ["T"])
    assert sub.observe_seq(5) is None
    assert sub.observe_seq(6) is None

    gap = sub.observe_seq(9)
    assert gap is not None and gap.kind is GapKind.MISSED and gap.missed == 2

    regression = sub.observe_seq(8)
    assert regression is not None and regression.kind is GapKind.REGRESSION


def test_gap_invalidates_every_market_in_the_subscription():
    sub = Subscription(1, "orderbook_delta", ["A", "B"])
    for ticker in ("A", "B"):
        sub.books[ticker].apply_snapshot({"market_ticker": ticker}, 1)
    sub.invalidate_all()
    assert sorted(sub.invalid_tickers()) == ["A", "B"]


def test_snapshot_duplicate_price_level_is_refused():
    book = OrderBook("T")
    with pytest.raises(BookIntegrityError):
        book.apply_snapshot(
            {"market_ticker": "T", "yes_dollars_fp": [["0.08", "1.00"], ["0.0800", "2.00"]]}, 1
        )


def test_bookset_maps_tickers_and_resets():
    books = BookSet()
    books.register(7, "orderbook_delta", ["A", "B"])
    assert books.book("A") is not None
    assert books.total_count() == 2
    books.reset()
    assert books.book("A") is None
