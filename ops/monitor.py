from __future__ import annotations

import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

RESERVOIR_LIMIT = 200000


class OffsetReservoir:
    def __init__(self, limit: int = RESERVOIR_LIMIT) -> None:
        self._limit = limit
        self._samples: List[int] = []
        self._seen = 0
        self._sum = 0
        self._min: Optional[int] = None
        self._max: Optional[int] = None
        self._random = random.Random(0)

    def add(self, value: int) -> None:
        self._seen += 1
        self._sum += value
        if self._min is None or value < self._min:
            self._min = value
        if self._max is None or value > self._max:
            self._max = value
        if len(self._samples) < self._limit:
            self._samples.append(value)
            return
        index = self._random.randrange(self._seen)
        if index < self._limit:
            self._samples[index] = value

    def summary(self) -> Dict[str, Optional[float]]:
        if not self._seen:
            return {"count": 0}
        ordered = sorted(self._samples)

        def quantile(q: float) -> int:
            position = int(q * (len(ordered) - 1))
            return ordered[position]

        return {
            "count": self._seen,
            "mean_ms": self._sum / self._seen,
            "min_ms": self._min,
            "p01_ms": quantile(0.01),
            "p50_ms": quantile(0.50),
            "p95_ms": quantile(0.95),
            "p99_ms": quantile(0.99),
            "max_ms": self._max,
            "sampled": len(ordered),
        }


@dataclass
class MarketAvailability:
    tracked_since: float
    is_valid: bool = False
    last_change: float = 0.0
    valid_seconds: float = 0.0
    invalid_seconds: float = 0.0
    invalid_episodes: int = 0


class CaptureMonitor:
    def __init__(self, started_mono: float) -> None:
        self.started_mono = started_mono
        self.message_counts: Counter = Counter()
        self.gap_counts: Counter = Counter()
        self.missed_messages = 0
        self.reconnects = 0
        self.integrity_errors = 0
        self.resync_requests = 0
        self.bytes_received = 0
        self.offsets = OffsetReservoir()
        self.offsets_by_type: Dict[str, OffsetReservoir] = defaultdict(OffsetReservoir)
        self.markets: Dict[str, MarketAvailability] = {}
        self.last_message_mono: Optional[float] = None

    def track_market(self, ticker: str, mono: float) -> None:
        if ticker not in self.markets:
            self.markets[ticker] = MarketAvailability(tracked_since=mono, last_change=mono)

    def mark_valid(self, ticker: str, mono: float) -> None:
        state = self.markets.get(ticker)
        if state is None:
            self.track_market(ticker, mono)
            state = self.markets[ticker]
        if state.is_valid:
            return
        state.invalid_seconds += mono - state.last_change
        state.is_valid = True
        state.last_change = mono

    def mark_invalid(self, ticker: str, mono: float) -> None:
        state = self.markets.get(ticker)
        if state is None:
            self.track_market(ticker, mono)
            return
        if not state.is_valid:
            return
        state.valid_seconds += mono - state.last_change
        state.is_valid = False
        state.last_change = mono
        state.invalid_episodes += 1

    def on_message(self, message_type: str, recv_ms: int, ts_ms: Optional[int], size: int) -> None:
        self.message_counts[message_type] += 1
        self.bytes_received += size
        if isinstance(ts_ms, int) and ts_ms > 0:
            offset = recv_ms - ts_ms
            self.offsets.add(offset)
            self.offsets_by_type[message_type].add(offset)

    def on_gap(self, kind: str, missed: int) -> None:
        self.gap_counts[kind] += 1
        self.missed_messages += missed

    def availability(self, mono: float) -> Dict[str, float]:
        valid = 0.0
        invalid = 0.0
        for state in self.markets.values():
            elapsed = mono - state.last_change
            valid += state.valid_seconds + (elapsed if state.is_valid else 0.0)
            invalid += state.invalid_seconds + (0.0 if state.is_valid else elapsed)
        total = valid + invalid
        return {
            "market_valid_seconds": valid,
            "market_gapped_seconds": invalid,
            "gapped_fraction": (invalid / total) if total else 0.0,
            "markets_tracked": len(self.markets),
        }

    def worst_markets(self, mono: float, limit: int = 10) -> List[Dict[str, object]]:
        rows = []
        for ticker, state in self.markets.items():
            elapsed = mono - state.last_change
            invalid = state.invalid_seconds + (0.0 if state.is_valid else elapsed)
            valid = state.valid_seconds + (elapsed if state.is_valid else 0.0)
            total = valid + invalid
            rows.append(
                {
                    "ticker": ticker,
                    "gapped_fraction": (invalid / total) if total else 1.0,
                    "gapped_seconds": invalid,
                    "episodes": state.invalid_episodes,
                }
            )
        rows.sort(key=lambda r: r["gapped_seconds"], reverse=True)
        return rows[:limit]

    def snapshot(self, mono: float) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "uptime_seconds": mono - self.started_mono,
            "messages": dict(self.message_counts),
            "bytes_received": self.bytes_received,
            "gaps": dict(self.gap_counts),
            "missed_messages": self.missed_messages,
            "reconnects": self.reconnects,
            "integrity_errors": self.integrity_errors,
            "resync_requests": self.resync_requests,
            "clock_offset": self.offsets.summary(),
            "seconds_since_last_message": (
                None if self.last_message_mono is None else mono - self.last_message_mono
            ),
        }
        payload.update(self.availability(mono))
        return payload

    def final_report(self, mono: float) -> Dict[str, object]:
        report = self.snapshot(mono)
        report["clock_offset_by_type"] = {
            name: reservoir.summary() for name, reservoir in self.offsets_by_type.items()
        }
        report["worst_markets"] = self.worst_markets(mono, limit=20)
        return report
