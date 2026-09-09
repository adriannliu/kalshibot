from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Tuple

from config.exchange import SETTLEMENT_VALUE


class Side(str, Enum):
    YES = "yes"
    NO = "no"


class RestingState(str, Enum):
    QUEUED = "queued"
    FILLED = "filled"
    CANCELLED = "cancelled"


@dataclass
class RestingOrder:
    order_id: int
    side: Side
    price: Decimal
    size_fp: int
    ahead_fp: int
    placed_ns: int
    state: RestingState = RestingState.QUEUED
    filled_fp: int = 0
    filled_ns: Optional[int] = None

    @property
    def remaining_fp(self) -> int:
        return self.size_fp - self.filled_fp


@dataclass
class Fill:
    order_id: int
    side: Side
    price: Decimal
    size_fp: int
    at_ns: int
    queue_ahead_at_placement_fp: int


class FillModel:
    def __init__(self) -> None:
        self._orders: Dict[int, RestingOrder] = {}
        self._next_id = 0
        self.fills: List[Fill] = []

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def place(self, side: Side, price: Decimal, size_fp: int, resting_ahead_fp: int, now_ns: int) -> RestingOrder:
        raise NotImplementedError

    def cancel(self, order_id: int) -> None:
        order = self._orders.get(order_id)
        if order is not None and order.state is RestingState.QUEUED:
            order.state = RestingState.CANCELLED

    def on_trade(self, side: Side, price: Decimal, size_fp: int, now_ns: int) -> List[Fill]:
        raise NotImplementedError

    def on_level_change(self, side: Side, price: Decimal, delta_fp: int, now_ns: int) -> None:
        return None

    def open_orders(self) -> List[RestingOrder]:
        return [o for o in self._orders.values() if o.state is RestingState.QUEUED]


class NaiveFillModel(FillModel):
    def place(self, side: Side, price: Decimal, size_fp: int, resting_ahead_fp: int, now_ns: int) -> RestingOrder:
        order = RestingOrder(self._new_id(), side, price, size_fp, resting_ahead_fp, now_ns)
        self._orders[order.order_id] = order
        return order

    def on_trade(self, side: Side, price: Decimal, size_fp: int, now_ns: int) -> List[Fill]:
        produced: List[Fill] = []
        for order in list(self._orders.values()):
            if order.state is not RestingState.QUEUED:
                continue
            if order.side is not side or order.price != price:
                continue
            filled = order.remaining_fp
            if filled <= 0:
                continue
            order.filled_fp += filled
            order.state = RestingState.FILLED
            order.filled_ns = now_ns
            fill = Fill(order.order_id, side, price, filled, now_ns, order.ahead_fp)
            self.fills.append(fill)
            produced.append(fill)
        return produced


class QueueAwareFillModel(FillModel):
    def __init__(self, cancels_ahead_are_random: bool = True) -> None:
        super().__init__()
        self._cancels_ahead_are_random = cancels_ahead_are_random
        self._level_size: Dict[Tuple[Side, Decimal], int] = {}

    def observe_level(self, side: Side, price: Decimal, size_fp: int) -> None:
        self._level_size[(side, price)] = size_fp

    def place(self, side: Side, price: Decimal, size_fp: int, resting_ahead_fp: int, now_ns: int) -> RestingOrder:
        order = RestingOrder(self._new_id(), side, price, size_fp, max(0, resting_ahead_fp), now_ns)
        self._orders[order.order_id] = order
        self._level_size.setdefault((side, price), resting_ahead_fp)
        return order

    def on_trade(self, side: Side, price: Decimal, size_fp: int, now_ns: int) -> List[Fill]:
        produced: List[Fill] = []
        for order in list(self._orders.values()):
            if order.state is not RestingState.QUEUED:
                continue
            if order.side is not side or order.price != price:
                continue

            consumed = size_fp
            if order.ahead_fp > 0:
                eaten = min(order.ahead_fp, consumed)
                order.ahead_fp -= eaten
                consumed -= eaten
            if consumed <= 0:
                continue

            filled = min(order.remaining_fp, consumed)
            if filled <= 0:
                continue
            order.filled_fp += filled
            order.filled_ns = now_ns
            if order.remaining_fp <= 0:
                order.state = RestingState.FILLED
            fill = Fill(order.order_id, side, price, filled, now_ns, order.ahead_fp)
            self.fills.append(fill)
            produced.append(fill)
        return produced

    def on_level_change(self, side: Side, price: Decimal, delta_fp: int, now_ns: int) -> None:
        key = (side, price)
        previous = self._level_size.get(key)
        self._level_size[key] = max(0, (previous or 0) + delta_fp)
        if delta_fp >= 0:
            return

        cancelled = -delta_fp
        for order in self._orders.values():
            if order.state is not RestingState.QUEUED:
                continue
            if order.side is not side or order.price != price:
                continue
            if order.ahead_fp <= 0:
                continue
            if self._cancels_ahead_are_random and previous:
                behind = order.size_fp
                total = max(1, previous)
                share = Decimal(order.ahead_fp) / Decimal(total)
                order.ahead_fp = max(0, order.ahead_fp - int(cancelled * share))
            else:
                order.ahead_fp = max(0, order.ahead_fp - cancelled)
