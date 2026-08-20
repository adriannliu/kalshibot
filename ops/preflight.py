from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Any, Dict, List, Optional

from auth.credentials import MissingCredentials, load_from_env
from auth.signer import RequestSigner
from config import exchange
from feed import universe as universe_module
from feed.rest_client import RestClient


def run(markets_path: str, sample: int) -> Dict[str, Any]:
    credentials = load_from_env()
    signer = RequestSigner(credentials)
    client = RestClient(credentials, signer)

    checks: Dict[str, Any] = {
        "environment": credentials.environment,
        "rest_base": credentials.rest_base,
        "ws_url": credentials.ws_url,
        "key_id_prefix": credentials.key_id[:6],
    }

    for name, fetch in (
        ("account_limits", client.account_limits),
        ("endpoint_costs", client.endpoint_costs),
    ):
        try:
            checks[name] = fetch()
        except Exception as error:
            checks[name] = {"error": repr(error)}

    spec = universe_module.load_spec(markets_path)
    resolution = universe_module.resolve(client, spec)

    multipliers = Counter(m.maker_multiplier for m in resolution.markets)
    regimes = Counter(m.fee_regime for m in resolution.markets)
    verticals = Counter(m.vertical for m in resolution.markets)
    series = Counter(m.series_ticker for m in resolution.markets)

    checks["universe"] = {
        "markets": len(resolution.markets),
        "target": spec.selection.total_target,
        "per_vertical": dict(verticals),
        "distinct_series": len(series),
        "maker_multiplier_counts": dict(multipliers),
        "fee_regime_counts": dict(regimes),
        "fee_table_source": resolution.fee_table.source,
        "unrecognized_fee_types": sorted(resolution.fee_table.unrecognized_fee_types),
        "unmatched_categories": resolution.unmatched_categories,
        "categories_available": resolution.categories_seen,
        "sample": [
            {
                "ticker": m.ticker,
                "vertical": m.vertical,
                "category": m.category,
                "maker_multiplier": m.maker_multiplier,
                "fee_regime": m.fee_regime,
                "close_time": m.close_time,
            }
            for m in resolution.markets[:sample]
        ],
    }

    shards = -(-len(resolution.markets) // max(1, exchange.MARKETS_PER_SUBSCRIPTION))
    checks["subscription_plan"] = {
        "shard_size": exchange.MARKETS_PER_SUBSCRIPTION,
        "orderbook_subscriptions": shards,
        "total_subscriptions": shards * len(exchange.MARKET_DATA_CHANNELS),
        "markets_invalidated_per_gap": exchange.MARKETS_PER_SUBSCRIPTION,
        "use_yes_price": exchange.USE_YES_PRICE,
    }

    problems: List[str] = []
    if len(resolution.markets) < 100:
        problems.append("universe has %d markets; Phase 1 requires 100+" % len(resolution.markets))
    if resolution.unmatched_categories:
        problems.append(
            "categories not found on the exchange: %s" % ", ".join(sorted(set(resolution.unmatched_categories)))
        )
    for vertical in spec.verticals:
        if verticals.get(vertical.name, 0) == 0:
            problems.append("stratum %s resolved to zero markets" % vertical.name)
    if resolution.fee_table.unrecognized_fee_types:
        problems.append(
            "unrecognized fee_type values, fees may be understated: %s"
            % ", ".join(sorted(resolution.fee_table.unrecognized_fee_types))
        )
    if regimes.get(exchange.REGIME_MAKER_FREE, 0) == 0:
        problems.append("no maker-fee-free markets selected; Phase 2 cannot test the long tail")
    checks["problems"] = problems
    checks["ready"] = not problems
    return checks


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Verify credentials, limits, and market universe before capture")
    parser.add_argument("--markets", default="config/markets.yaml")
    parser.add_argument("--sample", type=int, default=10)
    args = parser.parse_args(argv)

    try:
        report = run(args.markets, args.sample)
    except MissingCredentials as error:
        print(json.dumps({"ready": False, "error": str(error)}, indent=2))
        return 2

    print(json.dumps(report, indent=2, default=str))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
