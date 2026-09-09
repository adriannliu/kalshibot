from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field, fields
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

import yaml

from config import exchange
from config.exchange import FeeScheduleTable, SeriesFeeSchedule, series_ticker_of
from feed.fixed import parse_count_fp
from feed.rest_client import RestClient

MULTIVARIATE_KEY = "mve_collection_ticker"


@dataclass(frozen=True)
class VerticalSpec:
    name: str
    categories: Sequence[str]
    target_markets: int
    max_markets_per_series: int
    fee_regime: str = exchange.REGIME_ANY
    max_close_days: Optional[int] = None


@dataclass(frozen=True)
class SelectionSpec:
    status: str
    min_close_minutes: int
    max_close_days: int
    exclude_multivariate: bool
    min_market_volume_fp: int
    total_target: int


@dataclass(frozen=True)
class UniverseSpec:
    selection: SelectionSpec
    verticals: Sequence[VerticalSpec]
    pinned_series: Sequence[str]
    pinned_markets: Sequence[str]


@dataclass(frozen=True)
class SelectedMarket:
    ticker: str
    series_ticker: str
    vertical: str
    category: str
    close_time: Optional[str]
    volume_fp: int
    fee_type: str
    fee_multiplier: str
    maker_multiplier: str
    taker_multiplier: str
    fee_regime: str
    event_ticker: str = ""
    strike_type: str = ""
    floor_strike: str = ""
    cap_strike: str = ""
    mutually_exclusive: Optional[bool] = None
    event_market_count: int = 0


@dataclass
class UniverseResolution:
    markets: List[SelectedMarket] = field(default_factory=list)
    fee_table: Optional[FeeScheduleTable] = None
    categories_seen: List[str] = field(default_factory=list)
    unmatched_categories: List[str] = field(default_factory=list)
    fetch_failures: List[str] = field(default_factory=list)
    per_vertical: Dict[str, int] = field(default_factory=dict)
    per_regime: Dict[str, int] = field(default_factory=dict)

    @property
    def tickers(self) -> List[str]:
        return [m.ticker for m in self.markets]


def load_spec(path: str) -> UniverseSpec:
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)

    sel = raw.get("selection", {})
    selection = SelectionSpec(
        status=sel.get("status", "open"),
        min_close_minutes=int(sel.get("min_close_minutes", 30)),
        max_close_days=int(sel.get("max_close_days", 21)),
        exclude_multivariate=bool(sel.get("exclude_multivariate", True)),
        min_market_volume_fp=parse_count_fp(str(sel.get("min_market_volume_fp", "0.00"))),
        total_target=int(sel.get("total_target", 100)),
    )

    verticals = []
    for name, body in (raw.get("verticals") or {}).items():
        verticals.append(
            VerticalSpec(
                name=name,
                categories=tuple(body.get("categories") or ()),
                target_markets=int(body.get("target_markets", 0)),
                max_markets_per_series=int(body.get("max_markets_per_series", 5)),
                fee_regime=str(body.get("fee_regime", exchange.REGIME_ANY)),
                max_close_days=(
                    int(body["max_close_days"]) if body.get("max_close_days") is not None else None
                ),
            )
        )

    return UniverseSpec(
        selection=selection,
        verticals=tuple(verticals),
        pinned_series=tuple(raw.get("pinned_series") or ()),
        pinned_markets=tuple(raw.get("pinned_markets") or ()),
    )


def _normalize(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def build_fee_table(series_records: Sequence[Dict[str, Any]]) -> FeeScheduleTable:
    schedules: Dict[str, SeriesFeeSchedule] = {}
    unrecognized = set()
    for record in series_records:
        ticker = record.get("ticker")
        multiplier = record.get("fee_multiplier")
        fee_type = record.get("fee_type")
        if not ticker or multiplier is None or not fee_type:
            continue
        if fee_type not in exchange.KNOWN_FEE_TYPES:
            unrecognized.add(str(fee_type))
        schedules[ticker] = SeriesFeeSchedule(
            series_ticker=ticker,
            fee_type=str(fee_type),
            fee_multiplier=Decimal(str(multiplier)),
        )
    return FeeScheduleTable(
        schedules=schedules,
        source="GET /series fee_type + fee_multiplier",
        fetched_at_ms=int(time.time() * 1000),
        unrecognized_fee_types=frozenset(unrecognized),
    )


def _series_volume(record: Dict[str, Any]) -> int:
    raw = record.get("volume_fp")
    if not isinstance(raw, str):
        return 0
    try:
        return parse_count_fp(raw)
    except Exception:
        return 0


def _strike(record: Dict[str, Any], key: str) -> str:
    value = record.get(key)
    return "" if value is None else str(value)


EVENT_INDEX_FAILURES: List[str] = []
MARKET_FETCH_FAILURES: List[str] = []


def fetch_event_index(client: RestClient, series_ticker: str) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    try:
        for record in client.events(series_ticker=series_ticker, with_nested_markets="false"):
            ticker = record.get("event_ticker") or record.get("ticker")
            if ticker:
                index[str(ticker)] = record
    except Exception as error:
        EVENT_INDEX_FAILURES.append("%s: %r" % (series_ticker, error))
    return index


def _market_volume(record: Dict[str, Any]) -> int:
    for key in ("volume_24h_fp", "volume_fp"):
        raw = record.get(key)
        if isinstance(raw, str):
            try:
                return parse_count_fp(raw)
            except Exception:
                continue
    return 0


def resolve(client: RestClient, spec: UniverseSpec) -> UniverseResolution:
    series_records = list(client.paginate("/series", "series", {"include_volume": "true"}))
    fee_table = build_fee_table(series_records)
    resolution = UniverseResolution(fee_table=fee_table)

    by_category: Dict[str, List[Dict[str, Any]]] = {}
    for record in series_records:
        category = str(record.get("category") or "")
        by_category.setdefault(_normalize(category), []).append(record)
    resolution.categories_seen = sorted({str(r.get("category") or "") for r in series_records})

    now = time.time()
    min_close = now + spec.selection.min_close_minutes * 60
    max_close = now + spec.selection.max_close_days * 86400

    chosen: Dict[str, SelectedMarket] = {}

    def market_passes(record: Dict[str, Any], close_ceiling: float) -> bool:
        if spec.selection.exclude_multivariate and record.get(MULTIVARIATE_KEY):
            return False
        if _market_volume(record) < spec.selection.min_market_volume_fp:
            return False
        close_time = record.get("close_time")
        if not close_time:
            return False
        stamp = _parse_iso(close_time)
        if stamp is None:
            return False
        return min_close <= stamp <= close_ceiling

    def take_series(
        series_record: Dict[str, Any],
        vertical: str,
        limit: int,
        close_ceiling: float = max_close,
    ) -> int:
        series_ticker = series_record["ticker"]
        category = str(series_record.get("category") or "")
        schedule = fee_table.get(series_ticker)
        added = 0
        try:
            records = list(client.markets(series_ticker=series_ticker, status=spec.selection.status))
            candidates = [r for r in records if market_passes(r, close_ceiling)]
        except Exception as error:
            MARKET_FETCH_FAILURES.append("%s: %r" % (series_ticker, error))
            return 0
        events = fetch_event_index(client, series_ticker)
        markets_per_event: Dict[str, int] = {}
        for record in records:
            key = str(record.get("event_ticker") or "")
            markets_per_event[key] = markets_per_event.get(key, 0) + 1
        candidates.sort(key=_market_volume, reverse=True)
        for record in candidates:
            if added >= limit:
                break
            ticker = record.get("ticker")
            if not ticker or ticker in chosen:
                continue
            event_ticker = str(record.get("event_ticker") or "")
            event = events.get(event_ticker) or {}
            chosen[ticker] = SelectedMarket(
                ticker=ticker,
                series_ticker=series_ticker,
                vertical=vertical,
                category=category,
                close_time=record.get("close_time"),
                volume_fp=_market_volume(record),
                fee_type=schedule.fee_type if schedule else "",
                fee_multiplier=str(schedule.fee_multiplier) if schedule else "",
                maker_multiplier=str(fee_table.maker_multiplier(series_ticker)),
                taker_multiplier=str(fee_table.taker_multiplier(series_ticker)),
                fee_regime=fee_table.regime(series_ticker),
                event_ticker=event_ticker,
                strike_type=str(record.get("strike_type") or ""),
                floor_strike=_strike(record, "floor_strike"),
                cap_strike=_strike(record, "cap_strike"),
                mutually_exclusive=event.get("mutually_exclusive"),
                event_market_count=markets_per_event.get(event_ticker, 0),
            )
            added += 1
        return added

    for pinned in spec.pinned_series:
        record = next((r for r in series_records if r.get("ticker") == pinned), None)
        if record is not None:
            take_series(record, "pinned", spec.selection.total_target)

    for vertical in spec.verticals:
        matched: List[Dict[str, Any]] = []
        unmatched: List[str] = []
        for category in vertical.categories:
            bucket = by_category.get(_normalize(category))
            if bucket:
                matched.extend(bucket)
            else:
                unmatched.append(category)
        resolution.unmatched_categories.extend(unmatched)
        if vertical.fee_regime != exchange.REGIME_ANY:
            matched = [
                r for r in matched
                if fee_table.regime(str(r.get("ticker"))) == vertical.fee_regime
            ]
        matched.sort(key=_series_volume, reverse=True)

        ceiling = (
            now + vertical.max_close_days * 86400
            if vertical.max_close_days is not None
            else max_close
        )
        count = 0
        for series_record in matched:
            if count >= vertical.target_markets:
                break
            remaining = vertical.target_markets - count
            count += take_series(
                series_record,
                vertical.name,
                min(vertical.max_markets_per_series, remaining),
                ceiling,
            )
        resolution.per_vertical[vertical.name] = count

    for ticker in spec.pinned_markets:
        if ticker not in chosen:
            chosen[ticker] = SelectedMarket(
                ticker=ticker,
                series_ticker=series_ticker_of(ticker),
                vertical="pinned",
                category="",
                close_time=None,
                volume_fp=0,
                fee_type=(fee_table.get(series_ticker_of(ticker)).fee_type if fee_table.get(series_ticker_of(ticker)) else ""),
                fee_multiplier="",
                maker_multiplier=str(fee_table.maker_multiplier(series_ticker_of(ticker))),
                taker_multiplier=str(fee_table.taker_multiplier(series_ticker_of(ticker))),
                fee_regime=fee_table.regime(series_ticker_of(ticker)),
            )

    resolution.fetch_failures = list(MARKET_FETCH_FAILURES) + list(EVENT_INDEX_FAILURES)
    resolution.markets = sorted(chosen.values(), key=lambda m: m.ticker)
    for market in resolution.markets:
        resolution.per_regime[market.fee_regime] = resolution.per_regime.get(market.fee_regime, 0) + 1
    return resolution


def _parse_iso(text: str) -> Optional[float]:
    cleaned = text.replace("Z", "+0000")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return time.mktime(time.strptime(cleaned, fmt)) - time.timezone
        except ValueError:
            continue
    return None


def shard_by_series(markets: Sequence[SelectedMarket], shard_count: int) -> List[List[SelectedMarket]]:
    if shard_count < 1:
        raise ValueError("shard_count must be >= 1")
    grouped: Dict[str, List[SelectedMarket]] = {}
    for market in markets:
        grouped.setdefault(market.series_ticker, []).append(market)

    shards: List[List[SelectedMarket]] = [[] for _ in range(shard_count)]
    ordered = sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for _, group in ordered:
        target = min(range(shard_count), key=lambda i: (len(shards[i]), i))
        shards[target].extend(group)
    return [sorted(shard, key=lambda m: m.ticker) for shard in shards]


def resolution_to_dict(resolution: UniverseResolution, spec_path: str) -> Dict[str, Any]:
    table = resolution.fee_table
    schedules = {}
    if table is not None:
        for market in resolution.markets:
            schedule = table.get(market.series_ticker)
            if schedule is not None:
                schedules[market.series_ticker] = {
                    "fee_type": schedule.fee_type,
                    "fee_multiplier": str(schedule.fee_multiplier),
                }
    return {
        "resolved_at_ms": int(time.time() * 1000),
        "spec_path": spec_path,
        "per_vertical": resolution.per_vertical,
        "per_regime": resolution.per_regime,
        "unmatched_categories": resolution.unmatched_categories,
        "categories_seen": resolution.categories_seen,
        "fee_table": {
            "source": table.source if table else "",
            "fetched_at_ms": table.fetched_at_ms if table else 0,
            "unrecognized_fee_types": sorted(table.unrecognized_fee_types) if table else [],
            "schedules": schedules,
        },
        "markets": [asdict(m) for m in resolution.markets],
    }


def resolution_from_dict(data: Dict[str, Any]) -> UniverseResolution:
    raw_table = data.get("fee_table") or {}
    schedules = {
        ticker: SeriesFeeSchedule(
            series_ticker=ticker,
            fee_type=body["fee_type"],
            fee_multiplier=Decimal(body["fee_multiplier"]),
        )
        for ticker, body in (raw_table.get("schedules") or {}).items()
    }
    known = {f.name for f in fields(SelectedMarket)}
    return UniverseResolution(
        markets=[
            SelectedMarket(**{k: v for k, v in m.items() if k in known})
            for m in data.get("markets") or []
        ],
        fee_table=FeeScheduleTable(
            schedules=schedules,
            source=raw_table.get("source", ""),
            fetched_at_ms=int(raw_table.get("fetched_at_ms") or 0),
            unrecognized_fee_types=frozenset(raw_table.get("unrecognized_fee_types") or ()),
        ),
        categories_seen=list(data.get("categories_seen") or ()),
        unmatched_categories=list(data.get("unmatched_categories") or ()),
        per_vertical=dict(data.get("per_vertical") or {}),
        per_regime=dict(data.get("per_regime") or {}),
    )


def save_resolution(resolution: UniverseResolution, spec_path: str, path: str) -> str:
    payload = resolution_to_dict(resolution, spec_path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def load_resolution(path: str) -> UniverseResolution:
    with open(path, "r", encoding="utf-8") as handle:
        return resolution_from_dict(json.load(handle))
