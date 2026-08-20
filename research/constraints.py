from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from config import exchange
from config.exchange import SETTLEMENT_VALUE, taker_fee
from feed.fixed import contracts
from research.session import BookChange, MarketMeta, SessionMeta, SessionStream, TopOfBook
from research.stats import DurationHistogram, SignedSummary

NS_PER_SECOND = 1_000_000_000

KIND_COMPLEMENT = "complement"
KIND_IMPLICATION = "implication"
KIND_NORMALIZATION_YES = "normalization_yes"
KIND_NORMALIZATION_NO = "normalization_no"

ALL_KINDS = (KIND_COMPLEMENT, KIND_IMPLICATION, KIND_NORMALIZATION_YES, KIND_NORMALIZATION_NO)


def _strike(text: str) -> Optional[Decimal]:
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class Chain:
    event_ticker: str
    tickers: Tuple[str, ...]


@dataclass(frozen=True)
class ExclusiveSet:
    event_ticker: str
    tickers: Tuple[str, ...]


def build_chains(markets: Dict[str, MarketMeta]) -> List[Chain]:
    by_event: Dict[str, List[MarketMeta]] = {}
    for meta in markets.values():
        if meta.event_ticker and meta.strike_type in exchange.STRIKE_TYPES_LADDERED:
            by_event.setdefault(meta.event_ticker, []).append(meta)

    chains: List[Chain] = []
    for event_ticker, members in sorted(by_event.items()):
        for strike_types, attribute, descending in (
            (exchange.STRIKE_TYPES_ABOVE, "floor_strike", False),
            (exchange.STRIKE_TYPES_BELOW, "cap_strike", True),
        ):
            ordered: List[Tuple[Decimal, str]] = []
            for meta in members:
                if meta.strike_type not in strike_types:
                    continue
                strike = getattr(meta, attribute)
                if strike is None:
                    continue
                ordered.append((strike, meta.ticker))
            strikes = {strike for strike, _ in ordered}
            if len(ordered) < 2 or len(strikes) != len(ordered):
                continue
            ordered.sort(reverse=descending)
            chains.append(Chain(event_ticker=event_ticker, tickers=tuple(t for _, t in ordered)))
    return chains


def build_exclusive_sets(markets: Dict[str, MarketMeta]) -> List[ExclusiveSet]:
    by_event: Dict[str, List[MarketMeta]] = {}
    for meta in markets.values():
        if meta.event_ticker and meta.mutually_exclusive:
            by_event.setdefault(meta.event_ticker, []).append(meta)

    sets: List[ExclusiveSet] = []
    for event_ticker, members in sorted(by_event.items()):
        expected = max(m.event_market_count for m in members)
        if len(members) < 2 or expected != len(members):
            continue
        sets.append(
            ExclusiveSet(
                event_ticker=event_ticker,
                tickers=tuple(sorted(m.ticker for m in members)),
            )
        )
    return sets


@dataclass(frozen=True)
class Opportunity:
    kind: str
    tickers: Tuple[str, ...]
    gross_per_contract: Decimal
    size_fp: int
    fees: Decimal

    @property
    def gross(self) -> Decimal:
        return self.gross_per_contract * contracts(self.size_fp)

    @property
    def net(self) -> Decimal:
        return self.gross - self.fees

    @property
    def net_per_contract(self) -> Decimal:
        count = contracts(self.size_fp)
        if count == 0:
            return Decimal(0)
        return self.net / count


@dataclass
class Occurrence:
    kind: str
    tickers: Tuple[str, ...]
    started_ns: int
    ended_ns: int
    peak: Opportunity
    truncated: bool = False

    @property
    def duration_seconds(self) -> float:
        return max(0, self.ended_ns - self.started_ns) / NS_PER_SECOND

    def report(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "tickers": list(self.tickers),
            "duration_seconds": self.duration_seconds,
            "peak_gross_per_contract": str(self.peak.gross_per_contract),
            "peak_size_contracts": str(contracts(self.peak.size_fp)),
            "peak_fees": str(self.peak.fees),
            "peak_net_dollars": str(self.peak.net),
            "peak_net_per_contract": str(self.peak.net_per_contract),
            "truncated_by_gap": self.truncated,
        }


@dataclass
class ViolationStats:
    kind: str
    occurrences: int = 0
    profitable_occurrences: int = 0
    truncated: int = 0
    total_ns: int = 0
    durations: DurationHistogram = field(default_factory=DurationHistogram)
    net_per_contract: SignedSummary = field(default_factory=SignedSummary)
    net_dollars_upper_bound: Decimal = Decimal(0)
    best: Optional[Occurrence] = None

    def add(self, occurrence: Occurrence) -> None:
        self.occurrences += 1
        self.total_ns += max(0, occurrence.ended_ns - occurrence.started_ns)
        self.durations.add(occurrence.duration_seconds)
        if occurrence.truncated:
            self.truncated += 1
        net = occurrence.peak.net
        self.net_per_contract.add(occurrence.peak.net_per_contract)
        if net > 0:
            self.profitable_occurrences += 1
            self.net_dollars_upper_bound += net
        if self.best is None or net > self.best.peak.net:
            self.best = occurrence

    def merge(self, other: "ViolationStats") -> None:
        self.occurrences += other.occurrences
        self.profitable_occurrences += other.profitable_occurrences
        self.truncated += other.truncated
        self.total_ns += other.total_ns
        self.durations.merge(other.durations)
        self.net_per_contract.merge(other.net_per_contract)
        self.net_dollars_upper_bound += other.net_dollars_upper_bound
        if other.best is not None and (self.best is None or other.best.peak.net > self.best.peak.net):
            self.best = other.best

    def report(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "occurrences": self.occurrences,
            "occurrences_net_positive": self.profitable_occurrences,
            "truncated_by_gap": self.truncated,
            "total_seconds": self.total_ns / NS_PER_SECOND,
            "duration_seconds": self.durations.summary(),
            "net_per_contract_dollars": self.net_per_contract.summary(),
            "net_dollars_upper_bound": str(self.net_dollars_upper_bound),
            "best": None if self.best is None else self.best.report(),
        }


@dataclass
class _Open:
    started_ns: int
    peak: Opportunity


class ConstraintScanner:
    def __init__(self, meta: SessionMeta) -> None:
        self.meta = meta
        self.chains = build_chains(meta.markets)
        self.exclusive_sets = build_exclusive_sets(meta.markets)
        self.stats: Dict[str, ViolationStats] = {k: ViolationStats(kind=k) for k in ALL_KINDS}
        self.by_market: Dict[str, ViolationStats] = {}
        self.occurrences: List[Occurrence] = []
        self._tops: Dict[str, Optional[TopOfBook]] = {}
        self._open: Dict[Tuple[Any, ...], _Open] = {}
        self._now_ns = 0
        self._groups: Dict[str, List[Tuple[str, int]]] = {}
        for index, chain in enumerate(self.chains):
            for ticker in chain.tickers:
                self._groups.setdefault(ticker, []).append(("chain", index))
        for index, exclusive in enumerate(self.exclusive_sets):
            for ticker in exclusive.tickers:
                self._groups.setdefault(ticker, []).append(("exclusive", index))

    def _market_stats(self, ticker: str, kind: str) -> ViolationStats:
        key = "%s|%s" % (ticker, kind)
        found = self.by_market.get(key)
        if found is None:
            found = ViolationStats(kind=kind)
            self.by_market[key] = found
        return found

    def _taker_multiplier(self, ticker: str) -> Decimal:
        return self.meta.market(ticker).taker_multiplier

    def consume(self, event: Any) -> None:
        if not isinstance(event, BookChange):
            return
        if event.mono_ns > self._now_ns:
            self._now_ns = event.mono_ns
        self._tops[event.ticker] = event.top if event.valid else None
        self._evaluate_complement(event.ticker)
        for kind, index in self._groups.get(event.ticker, ()):
            if kind == "chain":
                self._evaluate_chain(self.chains[index])
            else:
                self._evaluate_exclusive(self.exclusive_sets[index])

    def _observe(self, key: Tuple[Any, ...], opportunity: Optional[Opportunity], truncated: bool) -> None:
        existing = self._open.get(key)
        if opportunity is None:
            if existing is not None:
                self._close(key, existing, truncated)
            return
        if existing is None:
            self._open[key] = _Open(started_ns=self._now_ns, peak=opportunity)
            return
        if opportunity.net > existing.peak.net:
            existing.peak = opportunity

    def _close(self, key: Tuple[Any, ...], state: _Open, truncated: bool) -> None:
        self._open.pop(key, None)
        occurrence = Occurrence(
            kind=state.peak.kind,
            tickers=state.peak.tickers,
            started_ns=state.started_ns,
            ended_ns=self._now_ns,
            peak=state.peak,
            truncated=truncated,
        )
        self.occurrences.append(occurrence)
        self.stats[occurrence.kind].add(occurrence)
        for ticker in occurrence.tickers:
            self._market_stats(ticker, occurrence.kind).add(occurrence)

    def _evaluate_complement(self, ticker: str) -> None:
        key = (KIND_COMPLEMENT, ticker)
        top = self._tops.get(ticker)
        if top is None:
            self._observe(key, None, truncated=True)
            return
        gross = top.yes_bid + top.no_bid - SETTLEMENT_VALUE
        if gross <= 0:
            self._observe(key, None, truncated=False)
            return
        size_fp = top.two_sided_size_fp
        multiplier = self._taker_multiplier(ticker)
        fees = taker_fee(SETTLEMENT_VALUE - top.no_bid, size_fp, multiplier) + taker_fee(
            SETTLEMENT_VALUE - top.yes_bid, size_fp, multiplier
        )
        self._observe(
            key,
            Opportunity(
                kind=KIND_COMPLEMENT,
                tickers=(ticker,),
                gross_per_contract=gross,
                size_fp=size_fp,
                fees=fees,
            ),
            truncated=False,
        )

    def _evaluate_chain(self, chain: Chain) -> None:
        tops = [self._tops.get(ticker) for ticker in chain.tickers]
        if any(top is None for top in tops):
            for ticker in chain.tickers:
                self._observe((KIND_IMPLICATION, ticker), None, truncated=True)
            return

        count = len(chain.tickers)
        best_yes: List[Optional[Decimal]] = [None] * count
        best_index = [-1] * count
        for index in range(count - 2, -1, -1):
            follower = index + 1
            forward = best_yes[follower]
            if forward is None or tops[follower].yes_bid >= forward:
                best_yes[index] = tops[follower].yes_bid
                best_index[index] = follower
            else:
                best_yes[index] = forward
                best_index[index] = best_index[follower]

        for index in range(count - 1):
            lower = chain.tickers[index]
            key = (KIND_IMPLICATION, lower)
            gross = best_yes[index] + tops[index].no_bid - SETTLEMENT_VALUE
            if gross <= 0:
                self._observe(key, None, truncated=False)
                continue
            higher = chain.tickers[best_index[index]]
            higher_top = tops[best_index[index]]
            size_fp = min(tops[index].no_size_fp, higher_top.yes_size_fp)
            fees = taker_fee(
                SETTLEMENT_VALUE - tops[index].no_bid, size_fp, self._taker_multiplier(lower)
            ) + taker_fee(
                SETTLEMENT_VALUE - higher_top.yes_bid, size_fp, self._taker_multiplier(higher)
            )
            self._observe(
                key,
                Opportunity(
                    kind=KIND_IMPLICATION,
                    tickers=(lower, higher),
                    gross_per_contract=gross,
                    size_fp=size_fp,
                    fees=fees,
                ),
                truncated=False,
            )

    def _evaluate_exclusive(self, exclusive: ExclusiveSet) -> None:
        tops = [self._tops.get(ticker) for ticker in exclusive.tickers]
        keys = (
            (KIND_NORMALIZATION_YES, exclusive.event_ticker),
            (KIND_NORMALIZATION_NO, exclusive.event_ticker),
        )
        if any(top is None for top in tops):
            for key in keys:
                self._observe(key, None, truncated=True)
            return

        count = len(exclusive.tickers)
        no_sum = sum((top.no_bid for top in tops), Decimal(0))
        yes_sum = sum((top.yes_bid for top in tops), Decimal(0))

        gross_yes = no_sum - (count - 1)
        if gross_yes > 0:
            size_fp = min(top.no_size_fp for top in tops)
            fees = sum(
                (
                    taker_fee(
                        SETTLEMENT_VALUE - top.no_bid, size_fp, self._taker_multiplier(ticker)
                    )
                    for ticker, top in zip(exclusive.tickers, tops)
                ),
                Decimal(0),
            )
            self._observe(
                keys[0],
                Opportunity(
                    kind=KIND_NORMALIZATION_YES,
                    tickers=exclusive.tickers,
                    gross_per_contract=gross_yes,
                    size_fp=size_fp,
                    fees=fees,
                ),
                truncated=False,
            )
        else:
            self._observe(keys[0], None, truncated=False)

        gross_no = yes_sum - SETTLEMENT_VALUE
        if gross_no > 0:
            size_fp = min(top.yes_size_fp for top in tops)
            fees = sum(
                (
                    taker_fee(
                        SETTLEMENT_VALUE - top.yes_bid, size_fp, self._taker_multiplier(ticker)
                    )
                    for ticker, top in zip(exclusive.tickers, tops)
                ),
                Decimal(0),
            )
            self._observe(
                keys[1],
                Opportunity(
                    kind=KIND_NORMALIZATION_NO,
                    tickers=exclusive.tickers,
                    gross_per_contract=gross_no,
                    size_fp=size_fp,
                    fees=fees,
                ),
                truncated=False,
            )
        else:
            self._observe(keys[1], None, truncated=False)

    def finish(self, last_mono_ns: Optional[int]) -> None:
        if last_mono_ns is not None and last_mono_ns > self._now_ns:
            self._now_ns = last_mono_ns
        for key, state in list(self._open.items()):
            self._close(key, state, truncated=True)

    def coverage(self) -> Dict[str, Any]:
        laddered = sum(len(chain.tickers) for chain in self.chains)
        exclusive = sum(len(group.tickers) for group in self.exclusive_sets)
        return {
            "markets": len(self.meta.markets),
            "chains": len(self.chains),
            "markets_in_chains": laddered,
            "exclusive_sets": len(self.exclusive_sets),
            "markets_in_exclusive_sets": exclusive,
        }

    def report(self, top_markets: int = 20) -> Dict[str, Any]:
        ranked = sorted(
            self.by_market.items(),
            key=lambda item: item[1].net_dollars_upper_bound,
            reverse=True,
        )
        return {
            "coverage": self.coverage(),
            "by_kind": {kind: stats.report() for kind, stats in self.stats.items()},
            "by_market": {
                key: stats.report() for key, stats in ranked[:top_markets]
            },
        }


def scan_session(session_dir: str) -> ConstraintScanner:
    stream = SessionStream(session_dir)
    scanner = ConstraintScanner(stream.meta)
    for event in stream.events():
        scanner.consume(event)
    scanner.finish(stream.stats.last_mono_ns)
    return scanner


def merge_scanners(scanners: Iterable[ConstraintScanner]) -> Dict[str, ViolationStats]:
    merged: Dict[str, ViolationStats] = {k: ViolationStats(kind=k) for k in ALL_KINDS}
    for scanner in scanners:
        for kind, stats in scanner.stats.items():
            merged[kind].merge(stats)
    return merged
