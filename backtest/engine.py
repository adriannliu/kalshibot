from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Tuple

from config.exchange import SETTLEMENT_VALUE
from backtest.fill_model import FillModel, NaiveFillModel, QueueAwareFillModel, Side
from feed.fixed import contracts
from feed.tape import BookChange, MarketMeta, SessionStream, TopOfBook, TradeTick

TWO = Decimal(2)


NS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True)
class StrategyConfig:
    min_edge: Decimal = Decimal("0.02")
    order_size_fp: int = 1000
    max_inventory_fp: int = 10000
    horizon_seconds: int = 10
    adverse_multiplier: Decimal = Decimal("1")
    requote_on_price_move: bool = True

    @property
    def horizon_ns(self) -> int:
        return self.horizon_seconds * NS_PER_SECOND


@dataclass
class PendingMark:
    side: Side
    price: Decimal
    count: Decimal
    mid_at_fill: Decimal
    due_ns: int


@dataclass
class MarketResult:
    ticker: str
    fee_regime: str
    quotes_placed: int = 0
    fills: int = 0
    filled_contracts: Decimal = Decimal(0)
    gross: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    adverse: Decimal = Decimal(0)
    quotable_events: int = 0
    marked: int = 0
    unmarked: int = 0

    @property
    def net(self) -> Decimal:
        return self.gross - self.fees - self.adverse

    @property
    def net_per_contract(self) -> Decimal:
        if self.filled_contracts == 0:
            return Decimal(0)
        return self.net / self.filled_contracts


class MarketBacktest:
    def __init__(self, meta: MarketMeta, config: StrategyConfig, model: FillModel) -> None:
        self.meta = meta
        self.config = config
        self.model = model
        self.result = MarketResult(ticker=meta.ticker, fee_regime=meta.fee_regime)
        self._resting: Dict[Side, Tuple[int, Decimal]] = {}
        self._inventory_fp: Dict[Side, int] = {Side.YES: 0, Side.NO: 0}
        self._pending: List[PendingMark] = []
        self._last_mid: Optional[Decimal] = None

    def _cancel_side(self, side: Side) -> None:
        existing = self._resting.pop(side, None)
        if existing is not None:
            self.model.cancel(existing[0])

    def _inventory_room(self, side: Side) -> bool:
        return self._inventory_fp[side] + self.config.order_size_fp <= self.config.max_inventory_fp

    def _resolve_marks(self, now_ns: int, mid: Optional[Decimal], force: bool = False) -> None:
        if mid is None:
            return
        remaining: List[PendingMark] = []
        for mark in self._pending:
            if not force and now_ns < mark.due_ns:
                remaining.append(mark)
                continue
            if mark.side is Side.YES:
                captured = mark.mid_at_fill - mark.price
                drift = mark.mid_at_fill - mid
            else:
                captured = (SETTLEMENT_VALUE - mark.mid_at_fill) - mark.price
                drift = mid - mark.mid_at_fill
            adverse = drift * self.config.adverse_multiplier
            self.result.gross += captured * mark.count
            self.result.adverse += adverse * mark.count
            self.result.marked += 1
        self._pending = remaining

    def finalize(self) -> None:
        self._resolve_marks(0, self._last_mid, force=True)
        self.result.unmarked += len(self._pending)
        self._pending = []

    def on_book(self, change: BookChange) -> None:
        top = change.top
        if top is not None:
            self._last_mid = top.mid
            self._resolve_marks(change.recv_ns, top.mid)
        if not change.valid or top is None:
            self._cancel_side(Side.YES)
            self._cancel_side(Side.NO)
            return

        if isinstance(self.model, QueueAwareFillModel):
            self.model.observe_level(Side.YES, top.yes_bid, top.yes_size_fp)
            self.model.observe_level(Side.NO, top.no_bid, top.no_size_fp)

        if top.edge < self.config.min_edge:
            self._cancel_side(Side.YES)
            self._cancel_side(Side.NO)
            return

        self.result.quotable_events += 1
        for side, price, ahead in (
            (Side.YES, top.yes_bid, top.yes_size_fp),
            (Side.NO, top.no_bid, top.no_size_fp),
        ):
            resting = self._resting.get(side)
            if resting is not None:
                if not self.config.requote_on_price_move or resting[1] == price:
                    continue
                self._cancel_side(side)
            if not self._inventory_room(side):
                continue
            order = self.model.place(side, price, self.config.order_size_fp, ahead, change.recv_ns)
            self._resting[side] = (order.order_id, price)
            self.result.quotes_placed += 1

    def on_trade(self, tick: TradeTick) -> None:
        top = tick.top
        taker_side = Side.YES if tick.taker_bought_yes else Side.NO
        maker_side = Side.NO if tick.taker_bought_yes else Side.YES
        price = tick.yes_price if maker_side is Side.YES else tick.no_price

        if top is not None:
            self._last_mid = top.mid
            self._resolve_marks(tick.recv_ns, top.mid)

        for fill in self.model.on_trade(maker_side, price, tick.count_fp, tick.recv_ns):
            self._book_fill(fill.side, fill.price, fill.size_fp, top, tick.recv_ns)

        if isinstance(self.model, QueueAwareFillModel) and top is not None:
            self.model.observe_level(Side.YES, top.yes_bid, top.yes_size_fp)
            self.model.observe_level(Side.NO, top.no_bid, top.no_size_fp)

    def _book_fill(
        self, side: Side, price: Decimal, size_fp: int, top: Optional[TopOfBook], now_ns: int
    ) -> None:
        count = contracts(size_fp)
        mid = top.mid if top is not None else self._last_mid
        if mid is None:
            return

        self.result.fills += 1
        self.result.filled_contracts += count
        self.result.fees += self.meta.maker_leg_fee(price) * count
        self._pending.append(
            PendingMark(side, price, count, mid, now_ns + self.config.horizon_ns)
        )
        self._inventory_fp[side] += size_fp
        self._resting.pop(side, None)


@dataclass
class BacktestReport:
    label: str
    config: StrategyConfig
    markets: Dict[str, MarketResult] = field(default_factory=dict)

    def add(self, result: MarketResult) -> None:
        existing = self.markets.get(result.ticker)
        if existing is None:
            self.markets[result.ticker] = result
            return
        existing.quotes_placed += result.quotes_placed
        existing.fills += result.fills
        existing.filled_contracts += result.filled_contracts
        existing.gross += result.gross
        existing.fees += result.fees
        existing.adverse += result.adverse
        existing.quotable_events += result.quotable_events
        existing.marked += result.marked
        existing.unmarked += result.unmarked

    def totals(self) -> Dict[str, object]:
        fills = sum(m.fills for m in self.markets.values())
        quotes = sum(m.quotes_placed for m in self.markets.values())
        contracts_filled = sum((m.filled_contracts for m in self.markets.values()), Decimal(0))
        gross = sum((m.gross for m in self.markets.values()), Decimal(0))
        fees = sum((m.fees for m in self.markets.values()), Decimal(0))
        adverse = sum((m.adverse for m in self.markets.values()), Decimal(0))
        net = gross - fees - adverse
        return {
            "label": self.label,
            "markets": len(self.markets),
            "quotes_placed": quotes,
            "fills": fills,
            "fill_rate_per_quote": (fills / quotes) if quotes else 0.0,
            "contracts_filled": contracts_filled,
            "gross_dollars": gross,
            "fees_dollars": fees,
            "adverse_dollars": adverse,
            "net_dollars": net,
            "net_cents_per_contract": (net / contracts_filled * 100) if contracts_filled else Decimal(0),
            "gross_cents_per_contract": (gross / contracts_filled * 100) if contracts_filled else Decimal(0),
            "adverse_cents_per_contract": (adverse / contracts_filled * 100) if contracts_filled else Decimal(0),
            "marks_resolved": sum(m.marked for m in self.markets.values()),
            "marks_forced_at_session_end": sum(m.unmarked for m in self.markets.values()),
        }


def run_session(
    session_dir: str,
    config: StrategyConfig,
    tickers: Optional[Iterable[str]] = None,
) -> Dict[str, BacktestReport]:
    stream = SessionStream(session_dir)
    allowed = set(tickers) if tickers is not None else None

    reports = {
        "naive": BacktestReport("naive", config),
        "queue_aware": BacktestReport("queue_aware", config),
    }
    engines: Dict[Tuple[str, str], MarketBacktest] = {}

    def engine_for(label: str, ticker: str) -> MarketBacktest:
        key = (label, ticker)
        found = engines.get(key)
        if found is None:
            model = NaiveFillModel() if label == "naive" else QueueAwareFillModel()
            found = MarketBacktest(stream.meta.market(ticker), config, model)
            engines[key] = found
        return found

    for event in stream.events():
        ticker = getattr(event, "ticker", None)
        if ticker is None or (allowed is not None and ticker not in allowed):
            continue
        for label in reports:
            engine = engine_for(label, ticker)
            if isinstance(event, BookChange):
                engine.on_book(event)
            elif isinstance(event, TradeTick):
                engine.on_trade(event)

    for (label, _ticker), engine in engines.items():
        engine.finalize()
        reports[label].add(engine.result)
    return reports
