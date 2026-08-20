from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from config.exchange import SETTLEMENT_VALUE
from feed.fixed import contracts
from research.session import (
    BookChange,
    MarketMeta,
    SessionMeta,
    SessionStream,
    TopOfBook,
    TradeTick,
)
from research.stats import DurationHistogram, WeightedHistogram

CENTICENTS_PER_DOLLAR = 10000
NS_PER_SECOND = 1_000_000_000
NS_PER_HOUR = 3_600 * NS_PER_SECOND

EDGE_BUCKET_EDGES = (0, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20)
BUCKET_CROSSED = "crossed"


def to_centicents(value: Decimal) -> int:
    return int((value * CENTICENTS_PER_DOLLAR).to_integral_value(rounding=ROUND_FLOOR))


def edge_bucket(edge_cc: int) -> str:
    if edge_cc < 0:
        return BUCKET_CROSSED
    cents = edge_cc // 100
    previous = EDGE_BUCKET_EDGES[0]
    for lower in EDGE_BUCKET_EDGES[1:]:
        if cents < lower:
            return str(previous) if lower - previous == 1 else "%d-%d" % (previous, lower - 1)
        previous = lower
    return "%d+" % EDGE_BUCKET_EDGES[-1]


@dataclass
class EdgeBucket:
    valid_ns: int = 0
    trades: int = 0
    volume_fp: int = 0

    def merge(self, other: "EdgeBucket") -> None:
        self.valid_ns += other.valid_ns
        self.trades += other.trades
        self.volume_fp += other.volume_fp

    def arrivals_per_hour(self) -> Optional[float]:
        if self.valid_ns <= 0:
            return None
        return self.trades * NS_PER_HOUR / self.valid_ns

    def contracts_per_hour(self) -> Optional[float]:
        if self.valid_ns <= 0:
            return None
        return float(contracts(self.volume_fp)) * NS_PER_HOUR / self.valid_ns


@dataclass
class MarketMicrostructure:
    ticker: str
    meta: MarketMeta
    group_key: Optional[str] = None
    tracked_ns: int = 0
    quotable_ns: int = 0
    one_sided_ns: int = 0
    invalid_ns: int = 0
    above_breakeven_ns: int = 0
    edge_time: WeightedHistogram = field(default_factory=WeightedHistogram)
    net_edge_time: WeightedHistogram = field(default_factory=WeightedHistogram)
    yes_depth_time: WeightedHistogram = field(default_factory=WeightedHistogram)
    no_depth_time: WeightedHistogram = field(default_factory=WeightedHistogram)
    two_sided_depth_time: WeightedHistogram = field(default_factory=WeightedHistogram)
    yes_quote_life: DurationHistogram = field(default_factory=DurationHistogram)
    no_quote_life: DurationHistogram = field(default_factory=DurationHistogram)
    buckets: Dict[str, EdgeBucket] = field(default_factory=dict)
    trades: int = 0
    volume_fp: int = 0
    trades_without_book: int = 0
    trades_without_direction: int = 0

    def bucket(self, label: str) -> EdgeBucket:
        found = self.buckets.get(label)
        if found is None:
            found = EdgeBucket()
            self.buckets[label] = found
        return found

    def merge(self, other: "MarketMicrostructure") -> None:
        self.tracked_ns += other.tracked_ns
        self.quotable_ns += other.quotable_ns
        self.one_sided_ns += other.one_sided_ns
        self.invalid_ns += other.invalid_ns
        self.above_breakeven_ns += other.above_breakeven_ns
        self.edge_time.merge(other.edge_time)
        self.net_edge_time.merge(other.net_edge_time)
        self.yes_depth_time.merge(other.yes_depth_time)
        self.no_depth_time.merge(other.no_depth_time)
        self.two_sided_depth_time.merge(other.two_sided_depth_time)
        self.yes_quote_life.merge(other.yes_quote_life)
        self.no_quote_life.merge(other.no_quote_life)
        for label, bucket in other.buckets.items():
            self.bucket(label).merge(bucket)
        self.trades += other.trades
        self.volume_fp += other.volume_fp
        self.trades_without_book += other.trades_without_book
        self.trades_without_direction += other.trades_without_direction

    @property
    def breakeven_fraction(self) -> Optional[float]:
        if self.quotable_ns <= 0:
            return None
        return self.above_breakeven_ns / self.quotable_ns

    def report(self) -> Dict[str, Any]:
        quotable_hours = self.quotable_ns / NS_PER_HOUR
        identity: Dict[str, Any]
        if self.group_key is None:
            identity = {
                "ticker": self.ticker,
                "series_ticker": self.meta.series_ticker,
                "vertical": self.meta.vertical,
                "fee_regime": self.meta.fee_regime,
                "maker_multiplier": str(self.meta.maker_multiplier),
            }
        else:
            identity = {"group_by": self.group_key, "group": self.ticker}
        return {
            **identity,
            "tracked_hours": self.tracked_ns / NS_PER_HOUR,
            "quotable_hours": quotable_hours,
            "one_sided_hours": self.one_sided_ns / NS_PER_HOUR,
            "invalid_hours": self.invalid_ns / NS_PER_HOUR,
            "edge_cents": _cent_quantiles(self.edge_time),
            "net_edge_cents": _cent_quantiles(self.net_edge_time),
            "time_above_breakeven": self.breakeven_fraction,
            "yes_depth_contracts": _contract_quantiles(self.yes_depth_time),
            "no_depth_contracts": _contract_quantiles(self.no_depth_time),
            "two_sided_depth_contracts": _contract_quantiles(self.two_sided_depth_time),
            "yes_quote_life_seconds": self.yes_quote_life.summary(),
            "no_quote_life_seconds": self.no_quote_life.summary(),
            "trades": self.trades,
            "contracts_traded": str(contracts(self.volume_fp)),
            "trades_per_hour": (self.trades / quotable_hours) if quotable_hours else None,
            "trades_without_book": self.trades_without_book,
            "trades_without_direction": self.trades_without_direction,
            "by_edge_bucket": {
                label: {
                    "hours": bucket.valid_ns / NS_PER_HOUR,
                    "trades": bucket.trades,
                    "contracts": str(contracts(bucket.volume_fp)),
                    "trades_per_hour": bucket.arrivals_per_hour(),
                    "contracts_per_hour": bucket.contracts_per_hour(),
                }
                for label, bucket in sorted(self.buckets.items())
            },
        }


def _cent_quantiles(histogram: WeightedHistogram) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"weight_hours": histogram.total_weight / NS_PER_HOUR}
    mean = histogram.mean()
    payload["mean"] = None if mean is None else mean / 100.0
    for name, key in histogram.quantiles().items():
        payload[name] = None if key is None else key / 100.0
    return payload


def _contract_quantiles(histogram: WeightedHistogram) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    mean = histogram.mean()
    payload["mean"] = None if mean is None else mean / 100.0
    for name, key in histogram.quantiles().items():
        payload[name] = None if key is None else str(contracts(key))
    return payload


class _MarketTracker:
    __slots__ = (
        "stats",
        "since_ns",
        "top",
        "valid",
        "yes_price",
        "yes_since_ns",
        "no_price",
        "no_since_ns",
    )

    def __init__(self, stats: MarketMicrostructure) -> None:
        self.stats = stats
        self.since_ns: Optional[int] = None
        self.top: Optional[TopOfBook] = None
        self.valid = False
        self.yes_price: Optional[Decimal] = None
        self.yes_since_ns: Optional[int] = None
        self.no_price: Optional[Decimal] = None
        self.no_since_ns: Optional[int] = None

    def _accrue(self, now_ns: int) -> None:
        if self.since_ns is None:
            return
        elapsed = now_ns - self.since_ns
        if elapsed <= 0:
            return
        stats = self.stats
        stats.tracked_ns += elapsed
        if not self.valid:
            stats.invalid_ns += elapsed
            return
        top = self.top
        if top is None:
            stats.one_sided_ns += elapsed
            return
        stats.quotable_ns += elapsed
        edge_cc = to_centicents(top.edge)
        stats.edge_time.add(edge_cc, elapsed)
        fee = stats.meta.maker_pair_fee(top.yes_bid, top.no_bid)
        stats.net_edge_time.add(to_centicents(top.edge - fee), elapsed)
        if top.edge > fee:
            stats.above_breakeven_ns += elapsed
        stats.yes_depth_time.add(top.yes_size_fp, elapsed)
        stats.no_depth_time.add(top.no_size_fp, elapsed)
        stats.two_sided_depth_time.add(top.two_sided_size_fp, elapsed)
        stats.bucket(edge_bucket(edge_cc)).valid_ns += elapsed

    def _close_quote_lives(self, now_ns: int, record: bool) -> None:
        if record and self.yes_since_ns is not None and self.yes_price is not None:
            self.stats.yes_quote_life.add((now_ns - self.yes_since_ns) / NS_PER_SECOND)
        elif self.yes_since_ns is not None:
            self.stats.yes_quote_life.censored += 1
        if record and self.no_since_ns is not None and self.no_price is not None:
            self.stats.no_quote_life.add((now_ns - self.no_since_ns) / NS_PER_SECOND)
        elif self.no_since_ns is not None:
            self.stats.no_quote_life.censored += 1
        self.yes_price = None
        self.yes_since_ns = None
        self.no_price = None
        self.no_since_ns = None

    def on_change(self, event: BookChange) -> None:
        now = event.mono_ns
        self._accrue(now)
        self.since_ns = now

        if not event.valid:
            self._close_quote_lives(now, record=False)
            self.valid = False
            self.top = None
            return

        self.valid = True
        self.top = event.top

        yes_price = event.best_yes[0] if event.best_yes else None
        if yes_price != self.yes_price:
            if self.yes_since_ns is not None and self.yes_price is not None:
                self.stats.yes_quote_life.add((now - self.yes_since_ns) / NS_PER_SECOND)
            self.yes_price = yes_price
            self.yes_since_ns = now if yes_price is not None else None

        no_price = event.best_no[0] if event.best_no else None
        if no_price != self.no_price:
            if self.no_since_ns is not None and self.no_price is not None:
                self.stats.no_quote_life.add((now - self.no_since_ns) / NS_PER_SECOND)
            self.no_price = no_price
            self.no_since_ns = now if no_price is not None else None

    def on_trade(self, event: TradeTick) -> None:
        self._accrue(event.mono_ns)
        self.since_ns = event.mono_ns
        stats = self.stats
        stats.trades += 1
        stats.volume_fp += event.count_fp
        if not event.taker_outcome_side:
            stats.trades_without_direction += 1
        if self.top is None or not self.valid:
            stats.trades_without_book += 1
            return
        bucket = stats.bucket(edge_bucket(to_centicents(self.top.edge)))
        bucket.trades += 1
        bucket.volume_fp += event.count_fp

    def finish(self, now_ns: int) -> None:
        self._accrue(now_ns)
        self._close_quote_lives(now_ns, record=False)
        self.since_ns = None


class MicrostructureAnalysis:
    def __init__(self, meta: SessionMeta) -> None:
        self.meta = meta
        self.markets: Dict[str, MarketMicrostructure] = {}
        self._trackers: Dict[str, _MarketTracker] = {}

    def _tracker(self, ticker: str) -> _MarketTracker:
        tracker = self._trackers.get(ticker)
        if tracker is None:
            stats = MarketMicrostructure(ticker=ticker, meta=self.meta.market(ticker))
            self.markets[ticker] = stats
            tracker = _MarketTracker(stats)
            self._trackers[ticker] = tracker
        return tracker

    def consume(self, event: Any) -> None:
        if isinstance(event, BookChange):
            self._tracker(event.ticker).on_change(event)
        elif isinstance(event, TradeTick):
            self._tracker(event.ticker).on_trade(event)

    def finish(self, last_mono_ns: Optional[int]) -> None:
        if last_mono_ns is None:
            return
        for tracker in self._trackers.values():
            tracker.finish(last_mono_ns)


def analyze_session(session_dir: str) -> MicrostructureAnalysis:
    stream = SessionStream(session_dir)
    analysis = MicrostructureAnalysis(stream.meta)
    for event in stream.events():
        analysis.consume(event)
    analysis.finish(stream.stats.last_mono_ns)
    return analysis


def merge_markets(
    analyses: Iterable[MicrostructureAnalysis],
) -> Dict[str, MarketMicrostructure]:
    merged: Dict[str, MarketMicrostructure] = {}
    for analysis in analyses:
        for ticker, stats in analysis.markets.items():
            existing = merged.get(ticker)
            if existing is None:
                merged[ticker] = MarketMicrostructure(ticker=ticker, meta=stats.meta)
                existing = merged[ticker]
            existing.merge(stats)
    return merged


def group_by(
    markets: Dict[str, MarketMicrostructure], key: str
) -> Dict[str, MarketMicrostructure]:
    grouped: Dict[str, MarketMicrostructure] = {}
    for stats in markets.values():
        label = str(getattr(stats.meta, key))
        existing = grouped.get(label)
        if existing is None:
            existing = MarketMicrostructure(ticker=label, meta=stats.meta, group_key=key)
            grouped[label] = existing
        existing.merge(stats)
    return grouped
