from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import List, Optional

from config import exchange
from ops.capture_daemon import CaptureConfig, CaptureDaemon, run_capture


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 1 read-only Kalshi market data capture")
    parser.add_argument("--markets", default="config/markets.yaml")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--shard-size", type=int, default=exchange.MARKETS_PER_SUBSCRIPTION)
    parser.add_argument("--stale-feed-seconds", type=float, default=30.0)
    parser.add_argument("--digest-interval", type=float, default=300.0)
    parser.add_argument("--health-interval", type=float, default=60.0)
    parser.add_argument("--max-seconds", type=float, default=None)
    parser.add_argument("--use-yes-price", action="store_true", default=exchange.USE_YES_PRICE)
    parser.add_argument("--universe-file", default=None)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--site", default=None)
    parser.add_argument("--no-lifecycle", dest="capture_lifecycle", action="store_false", default=True)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config = CaptureConfig(
        markets_path=args.markets,
        data_root=args.data_root,
        session_id=args.session_id,
        shard_size=args.shard_size,
        use_yes_price=args.use_yes_price,
        stale_feed_seconds=args.stale_feed_seconds,
        digest_interval_seconds=args.digest_interval,
        health_interval_seconds=args.health_interval,
        max_session_seconds=args.max_seconds,
        universe_file=args.universe_file,
        shard_index=args.shard_index,
        shard_count=args.shard_count,
        site=args.site,
        capture_lifecycle=args.capture_lifecycle,
    )
    if not 0 <= config.shard_index < config.shard_count:
        raise SystemExit(
            "--shard-index must be in [0, %d)" % config.shard_count
        )
    report = asyncio.run(run_capture(config))
    json.dump(report, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
