from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import List, Optional

from auth.credentials import load_from_env
from auth.signer import RequestSigner
from feed import universe as universe_module
from feed.rest_client import RestClient


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve the market universe once and snapshot it for sharded capture processes"
    )
    parser.add_argument("--markets", default="config/markets.yaml")
    parser.add_argument("--out", required=True)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--min-markets", type=int, default=100)
    args = parser.parse_args(argv)

    credentials = load_from_env()
    client = RestClient(credentials, RequestSigner(credentials))
    spec = universe_module.load_spec(args.markets)
    resolution = universe_module.resolve(client, spec)

    if len(resolution.markets) < args.min_markets:
        print(
            json.dumps(
                {
                    "error": "universe too small",
                    "markets": len(resolution.markets),
                    "required": args.min_markets,
                }
            ),
            file=sys.stderr,
        )
        return 1

    universe_module.save_resolution(resolution, args.markets, args.out)
    shards = universe_module.shard_by_series(resolution.markets, args.shard_count)

    print(
        json.dumps(
            {
                "out": args.out,
                "markets": len(resolution.markets),
                "series": len({m.series_ticker for m in resolution.markets}),
                "per_regime": resolution.per_regime,
                "per_vertical": resolution.per_vertical,
                "shard_sizes": [len(s) for s in shards],
                "shard_series": [len({m.series_ticker for m in s}) for s in shards],
                "unrecognized_fee_types": sorted(resolution.fee_table.unrecognized_fee_types),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
