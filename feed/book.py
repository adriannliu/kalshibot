from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from config.exchange import SETTLEMENT_VALUE
from feed.fixed import format_count_fp, format_price, parse_count_fp, parse_price


class BookIntegrityError(RuntimeError):
    pass


class BookState(str, Enum):
    AWAITING_SNAPSHOT = "awaiting_snapshot"
    VALID = "valid"


class GapKind(str, Enum):
    MISSED = "missed"
    REGRESSION = "regression"


@dataclass(frozen=True)
class GapEvent:
    sid: int
    kind: GapKind
    expected_seq: int
    observed_seq: int

    @property
    def missed(self) -> int:
        return max(0, self.observed_seq - self.expected_seq)


Level = Tuple[Decimal, int]


def _decode_levels(raw: Optional[Sequence[Sequence[str]]]) -> Dict[Decimal, int]:
    levels: Dict[Decimal, int] = {}
    if not raw:
        return levels
    for entry in raw:
        if len(entry) != 2:
            raise BookIntegrityError("malformed price level %r" % (entry,))
        price = parse_price(entry[0])
        size_fp = parse_count_fp(entry[1])
        if size_fp < 0:
            raise BookIntegrityError("negative size in snapshot at %s" % format_price(price))
        if size_fp == 0:
            continue
        if price in levels:
            raise BookIntegrityError("duplicate price level %s in snapshot" % format_price(price))
        levels[price] = size_fp
    return levels


class OrderBook:
    __slots__ = ("ticker", "state", "yes", "no", "last_seq", "last_ts_ms", "snapshot_count")

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self.state = BookState.AWAITING_SNAPSHOT
        self.yes: Dict[Decimal, int] = {}
        self.no: Dict[Decimal, int] = {}
        self.last_seq: Optional[int] = None
        self.last_ts_ms: Optional[int] = None
        self.snapshot_count = 0

    def invalidate(self) -> None:
        self.state = BookState.AWAITING_SNAPSHOT
        self.yes = {}
        self.no = {}

    def apply_snapshot(self, msg: dict, seq: int) -> None:
        self.yes = _decode_levels(msg.get("yes_dollars_fp"))
        self.no = _decode_levels(msg.get("no_dollars_fp"))
        self.state = BookState.VALID
        self.last_seq = seq
        self.snapshot_count += 1

    def apply_delta(self, msg: dict, seq: int) -> None:
        if self.state is not BookState.VALID:
            raise BookIntegrityError("delta applied to %s while %s" % (self.ticker, self.state.value))
        side = msg.get("side")
        if side == "yes":
            levels = self.yes
        elif side == "no":
            levels = self.no
        else:
            raise BookIntegrityError("unrecognized side %r for %s" % (side, self.ticker))

        price = parse_price(msg["price_dollars"])
        delta_fp = parse_count_fp(msg["delta_fp"])
        updated = levels.get(price, 0) + delta_fp
        if updated < 0:
            raise BookIntegrityError(
                "%s %s level %s went negative (%s + %s)"
                % (self.ticker, side, format_price(price), levels.get(price, 0), delta_fp)
            )
        if updated == 0:
            levels.pop(price, None)
        else:
            levels[price] = updated

        self.last_seq = seq
        ts_ms = msg.get("ts_ms")
        if isinstance(ts_ms, int):
            self.last_ts_ms = ts_ms

    def best_yes_bid(self) -> Optional[Level]:
        if not self.yes:
            return None
        price = max(self.yes)
        return price, self.yes[price]

    def best_no_bid(self) -> Optional[Level]:
        if not self.no:
            return None
        price = max(self.no)
        return price, self.no[price]

    def edge_dollars(self) -> Optional[Decimal]:
        yes = self.best_yes_bid()
        no = self.best_no_bid()
        if yes is None or no is None:
            return None
        return SETTLEMENT_VALUE - (yes[0] + no[0])

    def canonical(self) -> str:
        parts: List[str] = [self.ticker, self.state.value]
        for label, levels in (("yes", self.yes), ("no", self.no)):
            parts.append(label)
            for price in sorted(levels):
                parts.append("%s:%s" % (format_price(price), format_count_fp(levels[price])))
        return "|".join(parts)

    def digest(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()


class Subscription:
    __slots__ = ("sid", "channel", "tickers", "expected_seq", "books")

    def __init__(self, sid: int, channel: str, tickers: Sequence[str]) -> None:
        self.sid = sid
        self.channel = channel
        self.tickers: List[str] = list(tickers)
        self.expected_seq: Optional[int] = None
        self.books: Dict[str, OrderBook] = {t: OrderBook(t) for t in tickers}

    def observe_seq(self, seq: int) -> Optional[GapEvent]:
        if self.expected_seq is None:
            self.expected_seq = seq + 1
            return None
        if seq == self.expected_seq:
            self.expected_seq = seq + 1
            return None
        if seq < self.expected_seq:
            return GapEvent(self.sid, GapKind.REGRESSION, self.expected_seq, seq)
        event = GapEvent(self.sid, GapKind.MISSED, self.expected_seq, seq)
        self.expected_seq = seq + 1
        return event

    def invalidate_all(self) -> None:
        for book in self.books.values():
            book.invalidate()

    def book(self, ticker: str) -> OrderBook:
        existing = self.books.get(ticker)
        if existing is None:
            existing = OrderBook(ticker)
            self.books[ticker] = existing
            self.tickers.append(ticker)
        return existing

    def invalid_tickers(self) -> List[str]:
        return [t for t, b in self.books.items() if b.state is not BookState.VALID]


class BookSet:
    def __init__(self) -> None:
        self.subscriptions: Dict[int, Subscription] = {}
        self._sid_of_ticker: Dict[str, int] = {}

    def register(self, sid: int, channel: str, tickers: Sequence[str]) -> Subscription:
        sub = Subscription(sid, channel, tickers)
        self.subscriptions[sid] = sub
        for ticker in tickers:
            self._sid_of_ticker[ticker] = sid
        return sub

    def reset(self) -> None:
        self.subscriptions.clear()
        self._sid_of_ticker.clear()

    def subscription(self, sid: int) -> Optional[Subscription]:
        return self.subscriptions.get(sid)

    def book(self, ticker: str) -> Optional[OrderBook]:
        sid = self._sid_of_ticker.get(ticker)
        if sid is None:
            return None
        return self.subscriptions[sid].books.get(ticker)

    def all_books(self) -> Iterable[OrderBook]:
        for sub in self.subscriptions.values():
            for book in sub.books.values():
                yield book

    def valid_count(self) -> int:
        return sum(1 for b in self.all_books() if b.state is BookState.VALID)

    def total_count(self) -> int:
        return sum(1 for _ in self.all_books())

    def digest(self) -> str:
        hasher = hashlib.sha256()
        for book in sorted(self.all_books(), key=lambda b: b.ticker):
            hasher.update(book.canonical().encode("utf-8"))
            hasher.update(b"\x00")
        return hasher.hexdigest()

    def digest_by_ticker(self) -> Dict[str, str]:
        return {b.ticker: b.digest() for b in self.all_books()}
