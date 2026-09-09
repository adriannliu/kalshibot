from __future__ import annotations

import glob
import gzip
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Iterator, List, Optional, Tuple

from config import exchange
from config.exchange import SETTLEMENT_VALUE
from feed import recorder as rec
from feed.book import BookIntegrityError, BookSet, BookState, Level, OrderBook
from feed.fixed import parse_count_fp, parse_price

TYPE_SNAPSHOT = "orderbook_snapshot"
TYPE_DELTA = "orderbook_delta"
TYPE_TRADE = "trade"

BOOK_TYPES = (TYPE_SNAPSHOT, TYPE_DELTA)

SIDE_YES = "yes"
SIDE_NO = "no"

TWO = Decimal(2)



class TruncatedFinalRecord(ValueError):
    def __init__(self, path: str) -> None:
        super().__init__(
            "final record in %s is truncated, which happens when the process was "
            "killed mid-write; pass allow_truncated_tail=True to skip it" % path
        )
        self.path = path


def segment_paths(session_dir: str) -> List[str]:
    compressed = sorted(glob.glob(os.path.join(session_dir, "capture-*.jsonl.gz")))
    plain = [
        path
        for path in glob.glob(os.path.join(session_dir, "capture-*.jsonl"))
        if path + ".gz" not in compressed
    ]
    return sorted(compressed + plain, key=lambda p: p[:-3] if p.endswith(".gz") else p)


def read_records(session_dir: str) -> Iterator[Dict[str, Any]]:
    for path in segment_paths(session_dir):
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as handle:
            pending: Optional[str] = None
            for line in handle:
                if pending is not None:
                    yield json.loads(pending)
                stripped = line.strip()
                pending = stripped if stripped else None
            if pending is None:
                continue
            try:
                yield json.loads(pending)
            except ValueError:
                raise TruncatedFinalRecord(path)


class SessionIncompatible(RuntimeError):
    pass


@dataclass(frozen=True)
class MarketMeta:
    ticker: str
    series_ticker: str
    event_ticker: str
    vertical: str
    category: str
    close_time: Optional[str]
    fee_type: str
    fee_multiplier: Decimal
    maker_multiplier: Decimal
    taker_multiplier: Decimal
    fee_regime: str
    strike_type: str
    floor_strike: Optional[Decimal]
    cap_strike: Optional[Decimal]
    mutually_exclusive: Optional[bool]
    event_market_count: int

    def maker_pair_fee(self, yes_price: Decimal, no_price: Decimal) -> Decimal:
        return self.maker_multiplier * exchange.MAKER_FEE_RATE * (
            yes_price * (SETTLEMENT_VALUE - yes_price)
            + no_price * (SETTLEMENT_VALUE - no_price)
        )

    def maker_leg_fee(self, yes_price: Decimal) -> Decimal:
        return (
            self.maker_multiplier
            * exchange.MAKER_FEE_RATE
            * yes_price
            * (SETTLEMENT_VALUE - yes_price)
        )

    def taker_pair_fee(self, yes_price: Decimal, no_price: Decimal) -> Decimal:
        return self.taker_multiplier * exchange.TAKER_FEE_RATE * (
            yes_price * (SETTLEMENT_VALUE - yes_price)
            + no_price * (SETTLEMENT_VALUE - no_price)
        )


UNKNOWN_MARKET_FIELDS = {
    "series_ticker": "",
    "event_ticker": "",
    "vertical": "",
    "category": "",
    "close_time": None,
    "fee_type": "",
    "strike_type": "",
    "floor_strike": None,
    "cap_strike": None,
    "mutually_exclusive": None,
    "event_market_count": 0,
}


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _decimal_or(value: Any, fallback: Decimal) -> Decimal:
    parsed = _decimal_or_none(value)
    return fallback if parsed is None else parsed


def market_meta_from_manifest(entry: Dict[str, Any]) -> MarketMeta:
    ticker = entry["ticker"]
    return MarketMeta(
        ticker=ticker,
        series_ticker=entry.get("series_ticker") or exchange.series_ticker_of(ticker),
        event_ticker=entry.get("event_ticker") or "",
        vertical=entry.get("vertical") or "",
        category=entry.get("category") or "",
        close_time=entry.get("close_time"),
        fee_type=entry.get("fee_type") or "",
        fee_multiplier=_decimal_or(entry.get("fee_multiplier"), exchange.TAKER_MULTIPLIER_DEFAULT),
        maker_multiplier=_decimal_or(
            entry.get("maker_multiplier"), exchange.MAKER_MULTIPLIER_WHEN_UNKNOWN
        ),
        taker_multiplier=_decimal_or(
            entry.get("taker_multiplier"), exchange.TAKER_MULTIPLIER_DEFAULT
        ),
        fee_regime=entry.get("fee_regime") or exchange.REGIME_MAKER_CHARGED,
        strike_type=entry.get("strike_type") or "",
        floor_strike=_decimal_or_none(entry.get("floor_strike")),
        cap_strike=_decimal_or_none(entry.get("cap_strike")),
        mutually_exclusive=entry.get("mutually_exclusive"),
        event_market_count=int(entry.get("event_market_count") or 0),
    )


def unknown_market_meta(ticker: str) -> MarketMeta:
    return market_meta_from_manifest({"ticker": ticker, **UNKNOWN_MARKET_FIELDS})


@dataclass(frozen=True)
class SessionMeta:
    session_id: str
    directory: str
    environment: str
    site: Optional[str]
    shard_index: int
    shard_count: int
    started_at_ms: Optional[int]
    use_yes_price: bool
    markets: Dict[str, MarketMeta]

    def market(self, ticker: str) -> MarketMeta:
        found = self.markets.get(ticker)
        return found if found is not None else unknown_market_meta(ticker)


def load_session_meta(session_dir: str) -> SessionMeta:
    path = os.path.join(session_dir, "manifest.json")
    if not os.path.exists(path):
        raise SessionIncompatible("no manifest.json in %s" % session_dir)
    with open(path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)

    config = manifest.get("config") or {}
    use_yes_price = bool(config.get("use_yes_price"))
    if use_yes_price:
        raise SessionIncompatible(
            "%s captured with use_yes_price=true; no-side prices are not complement prices "
            "and edge = 1 - (yes_bid + no_bid) does not hold" % session_dir
        )

    entries = (manifest.get("universe") or {}).get("markets") or []
    markets = {}
    for entry in entries:
        if entry.get("ticker"):
            markets[entry["ticker"]] = market_meta_from_manifest(entry)

    return SessionMeta(
        session_id=manifest.get("session_id") or os.path.basename(os.path.normpath(session_dir)),
        directory=session_dir,
        environment=manifest.get("environment") or "",
        site=manifest.get("site"),
        shard_index=int(manifest.get("shard_index") or 0),
        shard_count=int(manifest.get("shard_count") or 1),
        started_at_ms=manifest.get("started_at_ms"),
        use_yes_price=use_yes_price,
        markets=markets,
    )


@dataclass(frozen=True)
class TopOfBook:
    yes_bid: Decimal
    yes_size_fp: int
    no_bid: Decimal
    no_size_fp: int

    @property
    def edge(self) -> Decimal:
        return SETTLEMENT_VALUE - (self.yes_bid + self.no_bid)

    @property
    def yes_ask(self) -> Decimal:
        return SETTLEMENT_VALUE - self.no_bid

    @property
    def mid(self) -> Decimal:
        return (self.yes_bid + self.yes_ask) / TWO

    @property
    def two_sided_size_fp(self) -> int:
        return min(self.yes_size_fp, self.no_size_fp)


def top_of_book(book: OrderBook) -> Optional[TopOfBook]:
    if book.state is not BookState.VALID:
        return None
    yes = book.best_yes_bid()
    no = book.best_no_bid()
    if yes is None or no is None:
        return None
    return TopOfBook(yes_bid=yes[0], yes_size_fp=yes[1], no_bid=no[0], no_size_fp=no[1])


def best_levels(book: OrderBook) -> Tuple[Optional[Level], Optional[Level]]:
    if book.state is not BookState.VALID:
        return None, None
    return book.best_yes_bid(), book.best_no_bid()


@dataclass(frozen=True)
class BookChange:
    recv_ns: int
    mono_ns: int
    ts_ms: Optional[int]
    ticker: str
    valid: bool
    top: Optional[TopOfBook]
    best_yes: Optional[Level] = None
    best_no: Optional[Level] = None


@dataclass(frozen=True)
class TradeTick:
    recv_ns: int
    mono_ns: int
    ts_ms: Optional[int]
    ticker: str
    yes_price: Decimal
    no_price: Decimal
    count_fp: int
    taker_outcome_side: str
    taker_book_side: str
    top: Optional[TopOfBook]

    @property
    def taker_bought_yes(self) -> bool:
        return self.taker_outcome_side == SIDE_YES


@dataclass
class StreamStats:
    records: int = 0
    ws_messages: int = 0
    book_events: int = 0
    trades: int = 0
    trades_without_direction: int = 0
    gaps: int = 0
    resets: int = 0
    integrity_errors: int = 0
    first_mono_ns: Optional[int] = None
    last_mono_ns: Optional[int] = None
    first_recv_ns: Optional[int] = None
    last_recv_ns: Optional[int] = None

    @property
    def elapsed_ns(self) -> int:
        if self.first_mono_ns is None or self.last_mono_ns is None:
            return 0
        return max(0, self.last_mono_ns - self.first_mono_ns)


def _trade_direction(body: Dict[str, Any]) -> str:
    for key in ("taker_outcome_side", "taker_side"):
        value = body.get(key)
        if value in (SIDE_YES, SIDE_NO):
            return value
    return ""


class SessionStream:
    def __init__(self, session_dir: str, meta: Optional[SessionMeta] = None) -> None:
        self.directory = session_dir
        self.meta = meta if meta is not None else load_session_meta(session_dir)
        self.stats = StreamStats()
        self._books = BookSet()

    def book(self, ticker: str) -> Optional[OrderBook]:
        return self._books.book(ticker)

    def events(self) -> Iterator[Any]:
        for record in read_records(self.directory):
            self.stats.records += 1
            kind = record.get("k")
            if kind == rec.KIND_WS:
                for event in self._on_ws(record):
                    yield event
            elif kind == rec.KIND_SUBSCRIBED:
                self._on_subscribed(record)
            elif kind == rec.KIND_CONNECTION:
                for event in self._on_connection(record):
                    yield event

    def _mark_clock(self, record: Dict[str, Any]) -> None:
        mono_ns = record.get("mono_ns")
        recv_ns = record.get("recv_ns")
        if isinstance(mono_ns, int):
            if self.stats.first_mono_ns is None:
                self.stats.first_mono_ns = mono_ns
            if self.stats.last_mono_ns is None or mono_ns > self.stats.last_mono_ns:
                self.stats.last_mono_ns = mono_ns
        if isinstance(recv_ns, int):
            if self.stats.first_recv_ns is None:
                self.stats.first_recv_ns = recv_ns
            self.stats.last_recv_ns = recv_ns

    def _on_subscribed(self, record: Dict[str, Any]) -> None:
        payload = record.get("d") or {}
        sid = payload.get("sid")
        if payload.get("channel") == exchange.ORDERBOOK_CHANNEL and isinstance(sid, int):
            self._books.register(sid, exchange.ORDERBOOK_CHANNEL, payload.get("tickers") or [])

    def _on_connection(self, record: Dict[str, Any]) -> Iterator[BookChange]:
        payload = record.get("d") or {}
        if payload.get("event") != "reconnect_scheduled":
            return
        self._mark_clock(record)
        self.stats.resets += 1
        for change in self._invalidate_all(record):
            yield change
        self._books.reset()

    def _invalidate_all(self, record: Dict[str, Any]) -> Iterator[BookChange]:
        for book in list(self._books.all_books()):
            if book.state is BookState.VALID:
                yield self._invalidation(record, book.ticker)

    def _invalidation(self, record: Dict[str, Any], ticker: str) -> BookChange:
        return BookChange(
            recv_ns=int(record.get("recv_ns") or 0),
            mono_ns=int(record.get("mono_ns") or 0),
            ts_ms=None,
            ticker=ticker,
            valid=False,
            top=None,
        )

    def _on_ws(self, record: Dict[str, Any]) -> Iterator[Any]:
        self.stats.ws_messages += 1
        self._mark_clock(record)
        try:
            payload = json.loads(record["raw"])
        except ValueError:
            self.stats.integrity_errors += 1
            return

        message_type = payload.get("type")
        body = payload.get("msg") or {}
        if not isinstance(body, dict):
            self.stats.integrity_errors += 1
            return

        if message_type in BOOK_TYPES:
            for event in self._on_book(record, payload, body, message_type):
                yield event
        elif message_type == TYPE_TRADE:
            event = self._on_trade(record, body)
            if event is not None:
                yield event

    def _on_book(
        self,
        record: Dict[str, Any],
        payload: Dict[str, Any],
        body: Dict[str, Any],
        message_type: str,
    ) -> Iterator[BookChange]:
        sid = payload.get("sid")
        seq = payload.get("seq")
        ticker = body.get("market_ticker")
        if not isinstance(sid, int) or not isinstance(seq, int) or not isinstance(ticker, str):
            self.stats.integrity_errors += 1
            return

        sub = self._books.subscription(sid)
        if sub is None:
            sub = self._books.register(sid, exchange.ORDERBOOK_CHANNEL, [ticker])

        gap = sub.observe_seq(seq)
        if gap is not None:
            self.stats.gaps += 1
            for book in list(sub.books.values()):
                if book.state is BookState.VALID:
                    yield self._invalidation(record, book.ticker)
            sub.invalidate_all()

        book = sub.book(ticker)
        try:
            if message_type == TYPE_SNAPSHOT:
                book.apply_snapshot(body, seq)
            else:
                if book.state is not BookState.VALID:
                    return
                book.apply_delta(body, seq)
        except BookIntegrityError:
            self.stats.integrity_errors += 1
            was_valid = book.state is BookState.VALID
            book.invalidate()
            if was_valid:
                yield self._invalidation(record, ticker)
            return

        self.stats.book_events += 1
        ts_ms = body.get("ts_ms")
        best_yes, best_no = best_levels(book)
        yield BookChange(
            recv_ns=int(record.get("recv_ns") or 0),
            mono_ns=int(record.get("mono_ns") or 0),
            ts_ms=ts_ms if isinstance(ts_ms, int) else None,
            ticker=ticker,
            valid=True,
            top=top_of_book(book),
            best_yes=best_yes,
            best_no=best_no,
        )

    def _on_trade(self, record: Dict[str, Any], body: Dict[str, Any]) -> Optional[TradeTick]:
        ticker = body.get("market_ticker")
        if not isinstance(ticker, str):
            self.stats.integrity_errors += 1
            return None
        try:
            yes_price = parse_price(body["yes_price_dollars"])
            no_price = parse_price(body["no_price_dollars"])
            count_fp = parse_count_fp(body["count_fp"])
        except (KeyError, TypeError, ValueError):
            self.stats.integrity_errors += 1
            return None

        direction = _trade_direction(body)
        if not direction:
            self.stats.trades_without_direction += 1

        self.stats.trades += 1
        book = self._books.book(ticker)
        ts_ms = body.get("ts_ms")
        return TradeTick(
            recv_ns=int(record.get("recv_ns") or 0),
            mono_ns=int(record.get("mono_ns") or 0),
            ts_ms=ts_ms if isinstance(ts_ms, int) else None,
            ticker=ticker,
            yes_price=yes_price,
            no_price=no_price,
            count_fp=count_fp,
            taker_outcome_side=direction,
            taker_book_side=str(body.get("taker_book_side") or ""),
            top=top_of_book(book) if book is not None else None,
        )


def session_dirs(root: str) -> List[str]:
    if not os.path.isdir(root):
        return []
    found = []
    for name in sorted(os.listdir(root)):
        directory = os.path.join(root, name)
        if os.path.isdir(directory) and os.path.exists(os.path.join(directory, "manifest.json")):
            found.append(directory)
    return found
