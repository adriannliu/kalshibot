from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from backtest.engine import BacktestReport, MarketResult, StrategyConfig, run_session
from feed.tape import session_dirs

SENSITIVITY = (Decimal("0.5"), Decimal("1.0"), Decimal("1.5"))


def _run_one(payload: Tuple[str, StrategyConfig]) -> Tuple[str, Optional[Dict[str, List[MarketResult]]], str]:
    directory, config = payload
    try:
        reports = run_session(directory, config)
    except Exception as error:
        return os.path.basename(directory), None, repr(error)[:200]
    return (
        os.path.basename(directory),
        {label: list(report.markets.values()) for label, report in reports.items()},
        "",
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 3 backtest: naive vs queue-aware fills")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--sessions", type=int, default=0, help="0 = all")
    parser.add_argument("--min-edge", default="0.02")
    parser.add_argument("--order-size", type=int, default=1000)
    parser.add_argument("--max-inventory", type=int, default=10000)
    parser.add_argument("--horizon-seconds", type=int, default=10)
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    dirs = session_dirs(args.data_root)
    if args.sessions:
        dirs = dirs[: args.sessions]

    config = StrategyConfig(
        min_edge=Decimal(args.min_edge),
        order_size_fp=args.order_size,
        max_inventory_fp=args.max_inventory,
        horizon_seconds=args.horizon_seconds,
        adverse_multiplier=Decimal("1"),
    )

    combined: Dict[str, BacktestReport] = {
        "naive": BacktestReport("naive", config),
        "queue_aware": BacktestReport("queue_aware", config),
    }
    failures: List[str] = []
    started = time.time()

    print("running %d sessions across %d jobs" % (len(dirs), args.jobs), file=sys.stderr, flush=True)
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        done = 0
        for name, results, error in pool.map(_run_one, [(d, config) for d in dirs]):
            done += 1
            if results is None:
                failures.append("%s: %s" % (name, error))
            else:
                for label, markets in results.items():
                    for result in markets:
                        combined[label].add(result)
            print(
                "  [%3d/%d] %-32s %s  %.0fs elapsed"
                % (done, len(dirs), name, "ok" if results is not None else "FAILED", time.time() - started),
                file=sys.stderr, flush=True,
            )

    payload = {
        "data_root": args.data_root,
        "sessions": len(dirs),
        "sessions_failed": failures,
        "min_edge": args.min_edge,
        "order_size_fp": args.order_size,
        "horizon_seconds": args.horizon_seconds,
        "adverse_source": "measured per fill by marking to the mid at horizon",
        "sweep": {
            "adverse_x%s" % m: {label: report.totals(m) for label, report in combined.items()}
            for m in SENSITIVITY
        },
        "elapsed_seconds": round(time.time() - started, 1),
    }
    text = json.dumps(payload, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
