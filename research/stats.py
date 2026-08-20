from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_QUANTILES = (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)


@dataclass
class WeightedHistogram:
    weights: Dict[int, int] = field(default_factory=dict)

    def add(self, key: int, weight: int = 1) -> None:
        if weight <= 0:
            return
        self.weights[key] = self.weights.get(key, 0) + weight

    def merge(self, other: "WeightedHistogram") -> None:
        for key, weight in other.weights.items():
            self.add(key, weight)

    @property
    def total_weight(self) -> int:
        return sum(self.weights.values())

    def quantile(self, q: float) -> Optional[int]:
        total = self.total_weight
        if total <= 0:
            return None
        target = q * total
        seen = 0
        last = None
        for key in sorted(self.weights):
            seen += self.weights[key]
            last = key
            if seen >= target:
                return key
        return last

    def quantiles(self, qs: Sequence[float] = DEFAULT_QUANTILES) -> Dict[str, Optional[int]]:
        return {("p%g" % (q * 100)): self.quantile(q) for q in qs}

    def weight_at_or_above(self, key: int) -> int:
        return sum(w for k, w in self.weights.items() if k >= key)

    def weight_below(self, key: int) -> int:
        return sum(w for k, w in self.weights.items() if k < key)

    def mean(self) -> Optional[float]:
        total = self.total_weight
        if total <= 0:
            return None
        return sum(k * w for k, w in self.weights.items()) / total

    def min_key(self) -> Optional[int]:
        return min(self.weights) if self.weights else None

    def max_key(self) -> Optional[int]:
        return max(self.weights) if self.weights else None


BUCKETS_PER_DECADE = 20


def _log_bucket(value: float) -> int:
    return int(math.floor(math.log10(value) * BUCKETS_PER_DECADE))


def _bucket_value(bucket: int) -> float:
    return 10.0 ** ((bucket + 0.5) / BUCKETS_PER_DECADE)


@dataclass
class DurationHistogram:
    counts: Dict[int, int] = field(default_factory=dict)
    zero_count: int = 0
    total: int = 0
    sum_value: float = 0.0
    censored: int = 0

    def add(self, value: float) -> None:
        self.total += 1
        self.sum_value += value
        if value <= 0:
            self.zero_count += 1
            return
        bucket = _log_bucket(value)
        self.counts[bucket] = self.counts.get(bucket, 0) + 1

    def merge(self, other: "DurationHistogram") -> None:
        for bucket, count in other.counts.items():
            self.counts[bucket] = self.counts.get(bucket, 0) + count
        self.zero_count += other.zero_count
        self.total += other.total
        self.sum_value += other.sum_value
        self.censored += other.censored

    def quantile(self, q: float) -> Optional[float]:
        if self.total <= 0:
            return None
        target = q * self.total
        seen = self.zero_count
        if seen >= target:
            return 0.0
        for bucket in sorted(self.counts):
            seen += self.counts[bucket]
            if seen >= target:
                return _bucket_value(bucket)
        return _bucket_value(max(self.counts)) if self.counts else 0.0

    def mean(self) -> Optional[float]:
        if self.total <= 0:
            return None
        return self.sum_value / self.total

    def summary(self, qs: Sequence[float] = DEFAULT_QUANTILES) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "count": self.total,
            "censored": self.censored,
            "mean": self.mean(),
        }
        for q in qs:
            payload["p%g" % (q * 100)] = self.quantile(q)
        return payload


@dataclass
class SignedSummary:
    count: int = 0
    total: Decimal = Decimal(0)
    histogram: WeightedHistogram = field(default_factory=WeightedHistogram)
    scale: int = 10000

    def add(self, value: Decimal, weight: int = 1) -> None:
        self.count += weight
        self.total += value * weight
        self.histogram.add(int((value * self.scale).to_integral_value()), weight)

    def merge(self, other: "SignedSummary") -> None:
        self.count += other.count
        self.total += other.total
        self.histogram.merge(other.histogram)

    def mean(self) -> Optional[Decimal]:
        if self.count <= 0:
            return None
        return self.total / self.count

    def summary(self, qs: Sequence[float] = DEFAULT_QUANTILES) -> Dict[str, object]:
        mean = self.mean()
        payload: Dict[str, object] = {
            "count": self.count,
            "mean": None if mean is None else str(mean),
        }
        for q in qs:
            key = self.histogram.quantile(q)
            payload["p%g" % (q * 100)] = None if key is None else str(Decimal(key) / self.scale)
        return payload
