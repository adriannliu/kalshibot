from __future__ import annotations

from decimal import Decimal

import pytest

from backtest.engine import MarketBacktest, PendingMark, StrategyConfig
from backtest.fill_model import NaiveFillModel, QueueAwareFillModel, Side
from research.session import BookChange, MarketMeta, TopOfBook, TradeTick

TWO = Decimal(2)


def meta(maker_multiplier="0"):
    return MarketMeta(
        ticker="T", series_ticker="S", event_ticker="E", vertical="v", category="c",
        close_time=None, fee_type="quadratic", fee_multiplier=Decimal(1),
        maker_multiplier=Decimal(maker_multiplier), taker_multiplier=Decimal(1),
        fee_regime="maker_free", strike_type="", floor_strike=None, cap_strike=None,
        mutually_exclusive=None, event_market_count=1,
    )


def top(yes_bid="0.45", no_bid="0.50", size=100000):
    return TopOfBook(Decimal(yes_bid), size, Decimal(no_bid), size)


def book(ns, t, valid=True):
    return BookChange(recv_ns=ns, mono_ns=ns, ts_ms=None, ticker="T", valid=valid, top=t)


def trade(ns, t, count_fp=500000, taker_yes=True):
    price = t.yes_bid if t is not None else Decimal("0.45")
    other = t.no_bid if t is not None else Decimal("0.50")
    return TradeTick(
        recv_ns=ns, mono_ns=ns, ts_ms=None, ticker="T",
        yes_price=price, no_price=other, count_fp=count_fp,
        taker_outcome_side="yes" if taker_yes else "no",
        taker_book_side="bid" if taker_yes else "ask", top=t,
    )


def engine(config=None, model=None):
    return MarketBacktest(meta(), config or StrategyConfig(min_edge=Decimal("0.01")), model or NaiveFillModel())


def test_a_fill_is_not_scored_until_the_horizon_elapses():
    e = engine()
    t = top()
    e.on_book(book(0, t))
    e.on_trade(trade(1, t, taker_yes=False))

    assert e.result.fills == 1
    assert e.result.marked_at_horizon == 0
    assert e.result.gross == 0


def test_a_mid_that_does_not_move_scores_the_captured_spread_only():
    e = engine()
    t = top()
    e.on_book(book(0, t))
    e.on_trade(trade(1, t, taker_yes=False))
    e.on_book(book(11 * 10**9, t))

    assert e.result.marked_at_horizon == 1
    assert e.result.adverse == 0
    assert e.result.gross > 0


def test_a_mid_moving_against_a_yes_fill_is_charged_as_adverse():
    e = engine()
    e.on_book(book(0, top()))
    e.on_trade(trade(1, top(), taker_yes=False))
    e.on_book(book(11 * 10**9, top(yes_bid="0.35", no_bid="0.60")))

    assert e.result.marked_at_horizon == 1
    assert e.result.adverse > 0


def test_a_mid_moving_toward_a_yes_fill_is_credited_not_charged():
    e = engine()
    e.on_book(book(0, top()))
    e.on_trade(trade(1, top(), taker_yes=False))
    e.on_book(book(11 * 10**9, top(yes_bid="0.55", no_bid="0.40")))

    assert e.result.adverse < 0


def test_the_sensitivity_multiplier_scales_only_the_adverse_term():
    results = {}
    for multiplier in ("0.5", "1.0", "1.5"):
        e = engine(StrategyConfig(min_edge=Decimal("0.01"), adverse_multiplier=Decimal(multiplier)))
        e.on_book(book(0, top()))
        e.on_trade(trade(1, top(), taker_yes=False))
        e.on_book(book(11 * 10**9, top(yes_bid="0.35", no_bid="0.60")))
        results[multiplier] = (e.result.gross, e.result.adverse)

    assert results["0.5"][0] == results["1.0"][0] == results["1.5"][0]
    assert results["0.5"][1] * 2 == results["1.0"][1]
    assert results["1.5"][1] == results["1.0"][1] * Decimal("1.5")


def test_maker_fees_are_charged_only_where_the_series_charges_them():
    free = MarketBacktest(meta("0"), StrategyConfig(min_edge=Decimal("0.01")), NaiveFillModel())
    charged = MarketBacktest(meta("1"), StrategyConfig(min_edge=Decimal("0.01")), NaiveFillModel())
    for e in (free, charged):
        e.on_book(book(0, top()))
        e.on_trade(trade(1, top(), taker_yes=False))

    assert free.result.fees == 0
    assert charged.result.fees > 0


def test_a_mark_forced_early_is_counted_apart_from_one_that_reached_its_horizon():
    e = engine()
    e.on_book(book(0, top()))
    e.on_trade(trade(1, top(), taker_yes=False))
    e.finalize(now_ns=2)

    assert e.result.marked_forced == 1
    assert e.result.marked_at_horizon == 0


def test_a_mark_that_reached_its_horizon_is_not_counted_as_forced():
    e = engine()
    e.on_book(book(0, top()))
    e.on_trade(trade(1, top(), taker_yes=False))
    e.on_book(book(11 * 10**9, top()))
    e.finalize(now_ns=12 * 10**9)

    assert e.result.marked_at_horizon == 1
    assert e.result.marked_forced == 0


def test_a_fill_with_no_mid_available_is_counted_not_discarded():
    e = engine()
    e.model.place(Side.YES, Decimal("0.45"), 1000, 0, 0)
    e.on_trade(trade(1, None, taker_yes=False))

    assert e.result.fills == 0
    assert e.result.fills_unscoreable == 1


def test_an_invalid_book_cancels_resting_quotes():
    e = engine()
    e.on_book(book(0, top()))
    assert e.model.open_orders()

    e.on_book(book(1, None, valid=False))
    assert e.model.open_orders() == []


def test_no_quotes_are_placed_when_edge_is_below_threshold():
    e = engine(StrategyConfig(min_edge=Decimal("0.10")))
    e.on_book(book(0, top(yes_bid="0.49", no_bid="0.50")))

    assert e.result.quotes_placed == 0
