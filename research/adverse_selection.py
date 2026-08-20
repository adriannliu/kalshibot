from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

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
from research.stats import SignedSummary

NS_PER_SECOND = 1_000_000_000

DEFAULT_HORIZONS_S: Tuple[int, ...] = (1, 10, 60)
HORIZON_SESSION_END = "session_end"
HORIZON_SETTLEMENT = "settlement"

PRICE_BUCKET_WIDTH = Decimal("0.10")
SIZE_BUCKET_EDGES_FP = (0, 1000, 10000)


def horizon_label(seconds: int) -> str:
    return "%ds" % seconds


def price_bucket(price: Decimal) -> str:
    lower = (price / PRICE_BUCKET_WIDTH).to_integral_value(rounding=ROUND_FLOOR) * PRICE_BUCKET_WIDTH
    if lower >= SETTLEMENT_VALUE:
        lower = SETTLEMENT_VALUE - PRICE_BUCKET_WIDTH
    if lower < 0:
        lower = Decimal(0)
    cents = Decimal("0.01")
    return "%s-%s" % (lower.quantize(cents), (lower + PRICE_BUCKET_WIDTH).quantize(cents))


def size_bucket(count_fp: int) -> str:
    if count_fp < SIZE_BUCKET_EDGES_FP[1]:
        return "<10"
    if count_fp < SIZE_BUCKET_EDGES_FP[2]:
        return "10-99"
    return "100+"


@dataclass
class HorizonSummary:
    mid_move: SignedSummary = field(default_factory=SignedSummary)
    maker_pnl: SignedSummary = field(default_factory=SignedSummary)
    net_maker_pnl: SignedSummary = field(default_factory=SignedSummary)
    maker_fees: Decimal = Decimal(0)
    contracts_fp: int = 0
    unresolved_book_invalid: int = 0
    unresolved_stream_ended: int = 0

    def merge(self, other: "HorizonSummary") -> None:
        self.mid_move.merge(other.mid_move)
        self.maker_pnl.merge(other.maker_pnl)
        self.net_maker_pnl.merge(other.net_maker_pnl)
        self.maker_fees += other.maker_fees
        self.contracts_fp += other.contracts_fp
        self.unresolved_book_invalid += other.unresolved_book_invalid
        self.unresolved_stream_ended += other.unresolved_stream_ended

    def mean_adverse_selection(self) -> Optional[Decimal]:
        return self.mid_move.mean()

    def report(self) -> Dict[str, Any]:
        return {
            "mid_move_dollars": self.mid_move.summary(),
            "maker_pnl_dollars": self.maker_pnl.summary(),
            "net_maker_pnl_dollars": self.net_maker_pnl.summary(),
            "maker_fees_dollars": str(self.maker_fees),
            "contracts": str(contracts(self.contracts_fp)),
            "unresolved_book_invalid": self.unresolved_book_invalid,
            "unresolved_stream_ended": self.unresolved_stream_ended,
        }


@dataclass
class AdverseSelectionCurve:
    horizons: Dict[str, HorizonSummary] = field(default_factory=dict)

    def horizon(self, label: str) -> HorizonSummary:
        found = self.horizons.get(label)
        if found is None:
            found = HorizonSummary()
            self.horizons[label] = found
        return found

    def merge(self, other: "AdverseSelectionCurve") -> None:
        for label, summary in other.horizons.items():
            self.horizon(label).merge(summary)

    def report(self) -> Dict[str, Any]:
        return {label: summary.report() for label, summary in self.horizons.items()}


@dataclass
class MarketAdverseSelection:
    ticker: str
    meta: MarketMeta
    group_key: Optional[str] = None
    trades_seen: int = 0
    trades_measured: int = 0
    trades_without_direction: int = 0
    trades_without_book: int = 0
    curve: AdverseSelectionCurve = field(default_factory=AdverseSelectionCurve)
    by_price: Dict[str, AdverseSelectionCurve] = field(default_factory=dict)
    by_size: Dict[str, AdverseSelectionCurve] = field(default_factory=dict)

    def bucket_curve(self, table: Dict[str, AdverseSelectionCurve], key: str) -> AdverseSelectionCurve:
        found = table.get(key)
        if found is None:
            found = AdverseSelectionCurve()
            table[key] = found
        return found

    def merge(self, other: "MarketAdverseSelection") -> None:
        self.trades_seen += other.trades_seen
        self.trades_measured += other.trades_measured
        self.trades_without_direction += other.trades_without_direction
        self.trades_without_book += other.trades_without_book
        self.curve.merge(other.curve)
        for key, curve in other.by_price.items():
            self.bucket_curve(self.by_price, key).merge(curve)
        for key, curve in other.by_size.items():
            self.bucket_curve(self.by_size, key).merge(curve)

    def report(self) -> Dict[str, Any]:
        identity: Dict[str, Any]
        if self.group_key is None:
            identity = {
                "ticker": self.ticker,
                "series_ticker": self.meta.series_ticker,
                "vertical": self.meta.vertical,
                "fee_regime": self.meta.fee_regime,
            }
        else:
            identity = {"group_by": self.group_key, "group": self.ticker}
        return {
            **identity,
            "trades_seen": self.trades_seen,
            "trades_measured": self.trades_measured,
            "trades_without_direction": self.trades_without_direction,
            "trades_without_book": self.trades_without_book,
            "curve": self.curve.report(),
            "by_price_bucket": {k: c.report() for k, c in sorted(self.by_price.items())},
            "by_size_bucket": {k: c.report() for k, c in sorted(self.by_size.items())},
        }


@dataclass
class _Pending:
    ticker: str
    deadline_ns: int
    label: str
    taker_bought_yes: bool
    mid_before: Decimal
    maker_entry: Decimal
    maker_leg_fee: Decimal
    count_fp: int
    price_bucket: str
    size_bucket: str


def _signed(taker_bought_yes: bool, before: Decimal, after: Decimal) -> Decimal:
    return after - before if taker_bought_yes else before - after


class AdverseSelectionAnalysis:
    def __init__(
        self,
        meta: SessionMeta,
        horizons: Sequence[int] = DEFAULT_HORIZONS_S,
        settlements: Optional[Dict[str, Decimal]] = None,
    ) -> None:
        self.meta = meta
        self.horizons = tuple(horizons)
        self.settlements = settlements or {}
        self.markets: Dict[str, MarketAdverseSelection] = {}
        self._last_top: Dict[str, Optional[TopOfBook]] = {}
        self._pending: Dict[str, Deque[_Pending]] = {
            horizon_label(seconds): deque() for seconds in self.horizons
        }
        self._open: List[_Pending] = []
        self._now_ns = 0

    def _market(self, ticker: str) -> MarketAdverseSelection:
        found = self.markets.get(ticker)
        if found is None:
            found = MarketAdverseSelection(ticker=ticker, meta=self.meta.market(ticker))
            self.markets[ticker] = found
        return found

    def _record(self, pending: _Pending, after: Optional[Decimal]) -> None:
        market = self._market(pending.ticker)
        targets = (
            market.curve,
            market.bucket_curve(market.by_price, pending.price_bucket),
            market.bucket_curve(market.by_size, pending.size_bucket),
        )
        if after is None:
            for curve in targets:
                curve.horizon(pending.label).unresolved_book_invalid += 1
            return
        mid_move = _signed(pending.taker_bought_yes, pending.mid_before, after)
        maker_pnl = _signed(not pending.taker_bought_yes, pending.maker_entry, after)
        for curve in targets:
            summary = curve.horizon(pending.label)
            summary.mid_move.add(mid_move)
            summary.maker_pnl.add(maker_pnl)
            summary.net_maker_pnl.add(maker_pnl - pending.maker_leg_fee)
            summary.maker_fees += pending.maker_leg_fee * contracts(pending.count_fp)
            summary.contracts_fp += pending.count_fp

    def _drain(self, now_ns: int, inclusive: bool) -> None:
        for queue in self._pending.values():
            while queue and (
                queue[0].deadline_ns <= now_ns if inclusive else queue[0].deadline_ns < now_ns
            ):
                pending = queue.popleft()
                top = self._last_top.get(pending.ticker)
                self._record(pending, None if top is None else top.mid)

    def consume(self, event: Any) -> None:
        now = getattr(event, "mono_ns", self._now_ns)
        if now > self._now_ns:
            self._now_ns = now
        self._drain(self._now_ns, inclusive=False)

        if isinstance(event, BookChange):
            self._last_top[event.ticker] = event.top if event.valid else None
        elif isinstance(event, TradeTick):
            self._on_trade(event)

        self._drain(self._now_ns, inclusive=True)

    def _on_trade(self, event: TradeTick) -> None:
        market = self._market(event.ticker)
        market.trades_seen += 1
        if not event.taker_outcome_side:
            market.trades_without_direction += 1
            return
        top = self._last_top.get(event.ticker)
        if top is None:
            market.trades_without_book += 1
            return

        market.trades_measured += 1
        taker_bought_yes = event.taker_bought_yes
        maker_entry = event.yes_price
        maker_leg_fee = market.meta.maker_leg_fee(event.yes_price)
        bucket_price = price_bucket(top.mid)
        bucket_size = size_bucket(event.count_fp)

        for seconds in self.horizons:
            label = horizon_label(seconds)
            self._pending[label].append(
                _Pending(
                    ticker=event.ticker,
                    deadline_ns=event.mono_ns + seconds * NS_PER_SECOND,
                    label=label,
                    taker_bought_yes=taker_bought_yes,
                    mid_before=top.mid,
                    maker_entry=maker_entry,
                    maker_leg_fee=maker_leg_fee,
                    count_fp=event.count_fp,
                    price_bucket=bucket_price,
                    size_bucket=bucket_size,
                )
            )

        self._open.append(
            _Pending(
                ticker=event.ticker,
                deadline_ns=event.mono_ns,
                label=HORIZON_SESSION_END,
                taker_bought_yes=taker_bought_yes,
                mid_before=top.mid,
                maker_entry=maker_entry,
                maker_leg_fee=maker_leg_fee,
                count_fp=event.count_fp,
                price_bucket=bucket_price,
                size_bucket=bucket_size,
            )
        )

    def finish(self, last_mono_ns: Optional[int]) -> None:
        if last_mono_ns is not None and last_mono_ns > self._now_ns:
            self._now_ns = last_mono_ns
        self._drain(self._now_ns, inclusive=True)

        for queue in self._pending.values():
            while queue:
                self._mark_unresolved(queue.popleft())

        for pending in self._open:
            settlement = self.settlements.get(pending.ticker)
            if settlement is not None:
                terminal = _Pending(**{**pending.__dict__, "label": HORIZON_SETTLEMENT})
                self._record(terminal, settlement)
            top = self._last_top.get(pending.ticker)
            self._record(pending, None if top is None else top.mid)
        self._open = []

    def _mark_unresolved(self, pending: _Pending) -> None:
        market = self._market(pending.ticker)
        for curve in (
            market.curve,
            market.bucket_curve(market.by_price, pending.price_bucket),
            market.bucket_curve(market.by_size, pending.size_bucket),
        ):
            curve.horizon(pending.label).unresolved_stream_ended += 1


def analyze_session(
    session_dir: str,
    horizons: Sequence[int] = DEFAULT_HORIZONS_S,
    settlements: Optional[Dict[str, Decimal]] = None,
) -> AdverseSelectionAnalysis:
    stream = SessionStream(session_dir)
    analysis = AdverseSelectionAnalysis(stream.meta, horizons=horizons, settlements=settlements)
    for event in stream.events():
        analysis.consume(event)
    analysis.finish(stream.stats.last_mono_ns)
    return analysis


def merge_markets(
    analyses: Iterable[AdverseSelectionAnalysis],
) -> Dict[str, MarketAdverseSelection]:
    return merge_market_stats(analysis.markets for analysis in analyses)


def merge_market_stats(
    tables: Iterable[Dict[str, MarketAdverseSelection]],
) -> Dict[str, MarketAdverseSelection]:
    merged: Dict[str, MarketAdverseSelection] = {}
    for table in tables:
        for ticker, stats in table.items():
            existing = merged.get(ticker)
            if existing is None:
                existing = MarketAdverseSelection(ticker=ticker, meta=stats.meta)
                merged[ticker] = existing
            existing.merge(stats)
    return merged


def group_by(
    markets: Dict[str, MarketAdverseSelection], key: str
) -> Dict[str, MarketAdverseSelection]:
    grouped: Dict[str, MarketAdverseSelection] = {}
    for stats in markets.values():
        label = str(getattr(stats.meta, key))
        existing = grouped.get(label)
        if existing is None:
            existing = MarketAdverseSelection(ticker=label, meta=stats.meta, group_key=key)
            grouped[label] = existing
        existing.merge(stats)
    return grouped
