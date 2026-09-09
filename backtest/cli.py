from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal
from typing import Dict, List, Optional

from backtest.engine import BacktestReport, StrategyConfig, run_session
from research.session import session_dirs

SENSITIVITY = (Decimal("0.5"), Decimal("1.0"), Decimal("1.5"))


def merge(into: Dict[str, BacktestReport], new: Dict[str, BacktestReport]) -> None:
    for label, report in new.items():
        target = into.setdefault(label, BacktestReport(label, report.config))
        for result in report.markets.values():
            target.add(result)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 3 backtest: naive vs queue-aware fills")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--sessions", type=int, default=0, help="0 = all")
    parser.add_argument("--min-edge", default="0.02")
    parser.add_argument("--order-size", type=int, default=1000)
    parser.add_argument("--max-inventory", type=int, default=10000)
    parser.add_argument("--horizon-seconds", type=int, default=10)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    dirs = session_dirs(args.data_root)
    if args.sessions:
        dirs = dirs[: args.sessions]

    sweep: Dict[str, Dict[str, object]] = {}

    for multiplier in SENSITIVITY:
        config = StrategyConfig(
            min_edge=Decimal(args.min_edge),
            order_size_fp=args.order_size,
            max_inventory_fp=args.max_inventory,
            horizon_seconds=args.horizon_seconds,
            adverse_multiplier=multiplier,
        )
        combined: Dict[str, BacktestReport] = {}
        for directory in dirs:
            try:
                merge(combined, run_session(directory, config))
            except Exception as error:
                print("skip %s: %r" % (os.path.basename(directory), error))
        sweep["adverse_x%s" % multiplier] = {
            label: report.totals() for label, report in combined.items()
        }

    payload = {
        "data_root": args.data_root,
        "sessions": len(dirs),
        "min_edge": args.min_edge,
        "order_size_fp": args.order_size,
        "horizon_seconds": args.horizon_seconds,
        "adverse_source": "measured per fill by marking to the mid at horizon",
        "sweep": sweep,
    }
    text = json.dumps(payload, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
