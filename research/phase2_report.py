from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from research import adverse_selection as adverse
from research import constraints as constraint_module
from research import microstructure as micro
from research.session import SessionIncompatible, SessionMeta, SessionStream, session_dirs

DEFAULT_HORIZON = "10s"
HOURS_PER_WEEK = Decimal(168)

MIN_QUOTABLE_HOURS = 24.0
MIN_MEASURED_TRADES = 200
MIN_NET_PER_CONTRACT = Decimal("0.001")
DEFAULT_CAPTURE_SHARE = Decimal("0.05")
DEFAULT_MIN_WEEKLY_DOLLARS = Decimal("100")
DEFAULT_WINDOWS = 2
MIN_WINDOW_OVERLAP = 0.5


def cents(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value * 100)


@dataclass
class SessionResult:
    directory: str
    site: str
    started_at_ms: Optional[int]
    markets: Dict[str, micro.MarketMicrostructure]
    adverse_markets: Dict[str, adverse.MarketAdverseSelection]
    violations: Dict[str, constraint_module.ViolationStats]
    constraint_coverage: Dict[str, Any]
    ws_messages: int
    gaps: int


def analyze_session(
    session_dir: str,
    horizons: Sequence[int] = adverse.DEFAULT_HORIZONS_S,
    settlements: Optional[Dict[str, Decimal]] = None,
    scan_constraints: bool = True,
) -> SessionResult:
    stream = SessionStream(session_dir)
    structure = micro.MicrostructureAnalysis(stream.meta)
    selection = adverse.AdverseSelectionAnalysis(
        stream.meta, horizons=horizons, settlements=settlements
    )
    scanner = constraint_module.ConstraintScanner(stream.meta)

    for event in stream.events():
        structure.consume(event)
        selection.consume(event)
        if scan_constraints:
            scanner.consume(event)

    last = stream.stats.last_mono_ns
    structure.finish(last)
    selection.finish(last)
    scanner.finish(last)

    return SessionResult(
        directory=session_dir,
        site=stream.meta.site or "unlabelled",
        started_at_ms=stream.meta.started_at_ms,
        markets=structure.markets,
        adverse_markets=selection.markets,
        violations=scanner.stats,
        constraint_coverage=scanner.coverage(),
        ws_messages=stream.stats.ws_messages,
        gaps=stream.stats.gaps,
    )


def _analyze_for_pool(session_dir: str) -> Tuple[str, Optional[SessionResult], Optional[str]]:
    try:
        return session_dir, analyze_session(session_dir), None
    except SessionIncompatible as error:
        return session_dir, None, str(error)


@dataclass
class MarketVerdict:
    ticker: str
    series_ticker: str
    vertical: str
    fee_regime: str
    quotable_hours: float
    trades_measured: int
    contracts_per_week: Decimal
    edge_median: Optional[Decimal]
    net_edge_median: Optional[Decimal]
    time_above_breakeven: Optional[float]
    adverse_selection: Optional[Decimal]
    net_per_contract: Optional[Decimal]
    net_lower_bound: Optional[Decimal]
    qualifies: bool
    blockers: List[str] = field(default_factory=list)

    def report(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "series_ticker": self.series_ticker,
            "vertical": self.vertical,
            "fee_regime": self.fee_regime,
            "quotable_hours": self.quotable_hours,
            "trades_measured": self.trades_measured,
            "contracts_per_week": str(self.contracts_per_week),
            "edge_median_cents": cents(self.edge_median),
            "net_edge_median_cents": cents(self.net_edge_median),
            "time_above_breakeven": self.time_above_breakeven,
            "adverse_selection_cents": cents(self.adverse_selection),
            "net_per_contract_cents": cents(self.net_per_contract),
            "net_lower_bound_cents": cents(self.net_lower_bound),
            "qualifies": self.qualifies,
            "blockers": self.blockers,
        }


def _median_dollars(histogram) -> Optional[Decimal]:
    key = histogram.quantile(0.5)
    return None if key is None else Decimal(key) / 10000


def build_verdicts(
    structure: Dict[str, micro.MarketMicrostructure],
    selection: Dict[str, adverse.MarketAdverseSelection],
    horizon: str = DEFAULT_HORIZON,
    min_quotable_hours: float = MIN_QUOTABLE_HOURS,
    min_trades: int = MIN_MEASURED_TRADES,
    min_net: Decimal = MIN_NET_PER_CONTRACT,
) -> List[MarketVerdict]:
    verdicts: List[MarketVerdict] = []
    for ticker in sorted(set(structure) | set(selection)):
        stats = structure.get(ticker)
        curve = selection.get(ticker)
        quotable_hours = (stats.quotable_ns / micro.NS_PER_HOUR) if stats else 0.0

        contracts_per_week = Decimal(0)
        if stats is not None and quotable_hours > 0:
            traded = Decimal(stats.volume_fp) / 100
            contracts_per_week = traded / Decimal(str(quotable_hours)) * HOURS_PER_WEEK

        summary = curve.curve.horizons.get(horizon) if curve else None
        trades_measured = summary.net_maker_pnl.count if summary else 0
        net = summary.net_maker_pnl.mean() if summary else None
        lower = summary.net_maker_pnl.mean_lower_bound() if summary else None
        adverse_cost = summary.mid_move.mean() if summary else None

        blockers: List[str] = []
        if quotable_hours < min_quotable_hours:
            blockers.append("quotable_hours<%g" % min_quotable_hours)
        if trades_measured < min_trades:
            blockers.append("trades_measured<%d" % min_trades)
        if net is None or net <= min_net:
            blockers.append("net_per_contract<=%s" % min_net)
        if lower is None or lower <= 0:
            blockers.append("net_lower_bound<=0")

        verdicts.append(
            MarketVerdict(
                ticker=ticker,
                series_ticker=stats.meta.series_ticker if stats else "",
                vertical=stats.meta.vertical if stats else "",
                fee_regime=stats.meta.fee_regime if stats else "",
                quotable_hours=quotable_hours,
                trades_measured=trades_measured,
                contracts_per_week=contracts_per_week,
                edge_median=_median_dollars(stats.edge_time) if stats else None,
                net_edge_median=_median_dollars(stats.net_edge_time) if stats else None,
                time_above_breakeven=stats.breakeven_fraction if stats else None,
                adverse_selection=adverse_cost,
                net_per_contract=net,
                net_lower_bound=lower,
                qualifies=not blockers,
                blockers=blockers,
            )
        )
    return verdicts


def projected_weekly_dollars(
    verdicts: Iterable[MarketVerdict], capture_share: Decimal
) -> Decimal:
    total = Decimal(0)
    for verdict in verdicts:
        if not verdict.qualifies or verdict.net_lower_bound is None:
            continue
        total += verdict.net_lower_bound * verdict.contracts_per_week * capture_share
    return total


def assign_windows(
    analyses: Sequence[SessionResult], windows: int
) -> List[List[SessionResult]]:
    stamped = [a for a in analyses if a.started_at_ms is not None]
    if windows < 2 or len(stamped) < 2:
        return [list(analyses)]
    first = min(a.started_at_ms for a in stamped)
    last = max(a.started_at_ms for a in stamped)
    if last <= first:
        return [list(analyses)]
    span = last - first
    buckets: List[List[SessionAnalyses]] = [[] for _ in range(windows)]
    for analysis in analyses:
        stamp = analysis.started_at_ms
        if stamp is None:
            buckets[0].append(analysis)
            continue
        index = min(windows - 1, int((stamp - first) * windows // span))
        buckets[index].append(analysis)
    return buckets


def _qualifying(verdicts: Iterable[MarketVerdict]) -> List[str]:
    return sorted(v.ticker for v in verdicts if v.qualifies)


def _overlap(left: Sequence[str], right: Sequence[str]) -> float:
    if not left or not right:
        return 0.0
    a, b = set(left), set(right)
    return len(a & b) / len(a | b)


def evaluate_site(
    analyses: Sequence[SessionResult],
    horizon: str = DEFAULT_HORIZON,
    capture_share: Decimal = DEFAULT_CAPTURE_SHARE,
    min_weekly_dollars: Decimal = DEFAULT_MIN_WEEKLY_DOLLARS,
    windows: int = DEFAULT_WINDOWS,
) -> Dict[str, Any]:
    structure = micro.merge_market_stats(a.markets for a in analyses)
    selection = adverse.merge_market_stats(a.adverse_markets for a in analyses)
    verdicts = build_verdicts(structure, selection, horizon=horizon)
    qualifying = _qualifying(verdicts)
    weekly = projected_weekly_dollars(verdicts, capture_share)

    window_sets: List[Dict[str, Any]] = []
    for index, bucket in enumerate(assign_windows(analyses, windows)):
        if not bucket:
            window_sets.append({"window": index, "sessions": 0, "qualifying": []})
            continue
        window_structure = micro.merge_market_stats(a.markets for a in bucket)
        window_selection = adverse.merge_market_stats(a.adverse_markets for a in bucket)
        window_verdicts = build_verdicts(
            window_structure,
            window_selection,
            horizon=horizon,
            min_quotable_hours=MIN_QUOTABLE_HOURS / max(1, windows),
            min_trades=max(1, MIN_MEASURED_TRADES // max(1, windows)),
        )
        window_sets.append(
            {
                "window": index,
                "sessions": len(bucket),
                "qualifying": _qualifying(window_verdicts),
            }
        )

    populated = [w for w in window_sets if w["qualifying"]]
    overlap = (
        _overlap(window_sets[0]["qualifying"], window_sets[-1]["qualifying"])
        if len(window_sets) >= 2
        else 0.0
    )
    stable = len(populated) >= 2 and overlap >= MIN_WINDOW_OVERLAP

    violations = constraint_module.merge_violation_stats(a.violations for a in analyses)

    criteria = [
        {
            "name": "profitable_market_set",
            "passed": bool(qualifying),
            "detail": {"markets": qualifying, "count": len(qualifying)},
        },
        {
            "name": "volume_sufficient",
            "passed": weekly >= min_weekly_dollars,
            "detail": {
                "projected_weekly_dollars": str(weekly),
                "required": str(min_weekly_dollars),
                "capture_share_assumed": str(capture_share),
            },
        },
        {
            "name": "stable_across_disjoint_windows",
            "passed": stable,
            "detail": {
                "windows": window_sets,
                "jaccard_first_last": overlap,
                "required_overlap": MIN_WINDOW_OVERLAP,
            },
        },
    ]

    return {
        "sessions": len(analyses),
        "markets_analyzed": len(structure),
        "horizon": horizon,
        "quotable_hours": sum(s.quotable_ns for s in structure.values()) / micro.NS_PER_HOUR,
        "criteria": criteria,
        "gate_met": all(c["passed"] for c in criteria),
        "qualifying_markets": [
            v.report() for v in verdicts if v.qualifies
        ],
        "blocked_markets_sample": [
            v.report() for v in sorted(
                (v for v in verdicts if not v.qualifies),
                key=lambda v: v.net_per_contract or Decimal(-1),
                reverse=True,
            )[:20]
        ],
        "by_fee_regime": {
            label: stats.report()
            for label, stats in micro.group_by(structure, "fee_regime").items()
        },
        "adverse_selection_by_fee_regime": {
            label: stats.report()
            for label, stats in adverse.group_by(selection, "fee_regime").items()
        },
        "constraint_violations": {
            kind: stats.report() for kind, stats in violations.items()
        },
    }


def _collect(root: str, jobs: int) -> Tuple[List[SessionResult], List[Dict[str, str]]]:
    directories = session_dirs(root)
    results: List[SessionResult] = []
    skipped: List[Dict[str, str]] = []

    if jobs > 1 and len(directories) > 1:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            outcomes = pool.map(_analyze_for_pool, directories)
    else:
        outcomes = (_analyze_for_pool(d) for d in directories)

    for directory, result, reason in outcomes:
        if result is None:
            skipped.append({"directory": directory, "reason": reason or "unknown"})
        else:
            results.append(result)
    return results, skipped


def evaluate(
    root: str,
    horizon: str = DEFAULT_HORIZON,
    capture_share: Decimal = DEFAULT_CAPTURE_SHARE,
    min_weekly_dollars: Decimal = DEFAULT_MIN_WEEKLY_DOLLARS,
    windows: int = DEFAULT_WINDOWS,
    site: Optional[str] = None,
    jobs: int = 1,
) -> Dict[str, Any]:
    results, skipped = _collect(root, jobs)
    by_site: Dict[str, List[SessionResult]] = {}
    for result in results:
        if site is not None and result.site != site:
            continue
        by_site.setdefault(result.site, []).append(result)

    sites = {
        label: evaluate_site(
            analyses,
            horizon=horizon,
            capture_share=capture_share,
            min_weekly_dollars=min_weekly_dollars,
            windows=windows,
        )
        for label, analyses in sorted(by_site.items())
    }

    primary = max(
        sites.items(), key=lambda item: item[1]["quotable_hours"], default=(None, None)
    )[0]

    agreement = None
    if len(sites) >= 2:
        sets = [
            [m["ticker"] for m in report["qualifying_markets"]] for report in sites.values()
        ]
        agreement = _overlap(sets[0], sets[1])

    return {
        "root": root,
        "sites": sites,
        "primary_site": primary,
        "cross_site_agreement": agreement,
        "skipped_sessions": skipped,
        "phase2_gate_met": bool(primary) and sites[primary]["gate_met"],
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate the Phase 2 go/no-go gate from captured sessions"
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--horizon", default=DEFAULT_HORIZON)
    parser.add_argument("--capture-share", default=str(DEFAULT_CAPTURE_SHARE))
    parser.add_argument("--min-weekly-dollars", default=str(DEFAULT_MIN_WEEKLY_DOLLARS))
    parser.add_argument("--windows", type=int, default=DEFAULT_WINDOWS)
    parser.add_argument("--site")
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    args = parser.parse_args(argv)

    report = evaluate(
        args.data_root,
        horizon=args.horizon,
        capture_share=Decimal(args.capture_share),
        min_weekly_dollars=Decimal(args.min_weekly_dollars),
        windows=args.windows,
        site=args.site,
        jobs=args.jobs,
    )
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["phase2_gate_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
