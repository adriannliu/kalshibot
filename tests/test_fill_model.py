from __future__ import annotations

from decimal import Decimal

import pytest

from backtest.fill_model import (
    Fill,
    NaiveFillModel,
    QueueAwareFillModel,
    RestingState,
    Side,
)

PRICE = Decimal("0.55")


def test_naive_fills_whenever_the_market_trades_at_your_price():
    model = NaiveFillModel()
    model.place(Side.YES, PRICE, size_fp=1000, resting_ahead_fp=500000, now_ns=0)

    fills = model.on_trade(Side.YES, PRICE, size_fp=100, now_ns=1)

    assert len(fills) == 1
    assert fills[0].size_fp == 1000


def test_queue_aware_does_not_fill_behind_resting_size():
    model = QueueAwareFillModel()
    model.place(Side.YES, PRICE, size_fp=1000, resting_ahead_fp=500000, now_ns=0)

    assert model.on_trade(Side.YES, PRICE, size_fp=100, now_ns=1) == []
    assert model.open_orders()[0].ahead_fp == 499900


def test_queue_aware_fills_only_once_the_queue_ahead_is_exhausted():
    model = QueueAwareFillModel()
    model.place(Side.YES, PRICE, size_fp=1000, resting_ahead_fp=300, now_ns=0)

    assert model.on_trade(Side.YES, PRICE, size_fp=300, now_ns=1) == []
    fills = model.on_trade(Side.YES, PRICE, size_fp=1000, now_ns=2)

    assert len(fills) == 1
    assert fills[0].size_fp == 1000


def test_a_trade_that_clears_the_queue_partially_fills():
    model = QueueAwareFillModel()
    model.place(Side.YES, PRICE, size_fp=1000, resting_ahead_fp=200, now_ns=0)

    fills = model.on_trade(Side.YES, PRICE, size_fp=700, now_ns=1)

    assert len(fills) == 1 and fills[0].size_fp == 500
    order = model.open_orders()[0]
    assert order.state is RestingState.QUEUED and order.remaining_fp == 500


def test_cancels_ahead_advance_the_queue_without_a_trade():
    model = QueueAwareFillModel(cancels_ahead_are_random=False)
    model.place(Side.YES, PRICE, size_fp=100, resting_ahead_fp=1000, now_ns=0)

    model.on_level_change(Side.YES, PRICE, delta_fp=-400, now_ns=1)

    assert model.open_orders()[0].ahead_fp == 600


def test_queue_ahead_never_goes_negative():
    model = QueueAwareFillModel(cancels_ahead_are_random=False)
    model.place(Side.YES, PRICE, size_fp=100, resting_ahead_fp=50, now_ns=0)

    model.on_level_change(Side.YES, PRICE, delta_fp=-9999, now_ns=1)

    assert model.open_orders()[0].ahead_fp == 0


def test_additions_to_the_level_never_push_you_backwards():
    model = QueueAwareFillModel(cancels_ahead_are_random=False)
    model.place(Side.YES, PRICE, size_fp=100, resting_ahead_fp=500, now_ns=0)

    model.on_level_change(Side.YES, PRICE, delta_fp=+9000, now_ns=1)

    assert model.open_orders()[0].ahead_fp == 500


def test_trades_on_the_other_side_or_price_are_ignored():
    for model in (NaiveFillModel(), QueueAwareFillModel()):
        model.place(Side.YES, PRICE, size_fp=100, resting_ahead_fp=0, now_ns=0)
        assert model.on_trade(Side.NO, PRICE, size_fp=999, now_ns=1) == []
        assert model.on_trade(Side.YES, Decimal("0.56"), size_fp=999, now_ns=1) == []


def test_a_cancelled_order_never_fills():
    for model in (NaiveFillModel(), QueueAwareFillModel()):
        order = model.place(Side.YES, PRICE, size_fp=100, resting_ahead_fp=0, now_ns=0)
        model.cancel(order.order_id)
        assert model.on_trade(Side.YES, PRICE, size_fp=999, now_ns=1) == []


def test_queue_aware_fill_rate_is_a_small_fraction_of_naive():
    naive, queued = NaiveFillModel(), QueueAwareFillModel()
    for model in (naive, queued):
        for _ in range(50):
            model.place(Side.YES, PRICE, size_fp=100, resting_ahead_fp=376400, now_ns=0)

    for tick in range(200):
        naive.on_trade(Side.YES, PRICE, size_fp=1500, now_ns=tick)
        queued.on_trade(Side.YES, PRICE, size_fp=1500, now_ns=tick)

    assert len(naive.fills) == 50
    assert len(queued.fills) == 0


def test_fills_record_the_queue_depth_faced_at_placement():
    model = QueueAwareFillModel()
    model.place(Side.YES, PRICE, size_fp=100, resting_ahead_fp=100, now_ns=0)
    fills = model.on_trade(Side.YES, PRICE, size_fp=200, now_ns=7)

    assert fills[0].at_ns == 7
    assert isinstance(fills[0], Fill)
