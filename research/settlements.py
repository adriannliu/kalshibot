from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Set

from auth.credentials import load_from_env
from auth.signer import RequestSigner
from config import exchange
from feed.rest_client import RestClient

SOURCE_LIFECYCLE = "lifecycle"
SOURCE_REST = "rest"
REST_BATCH = 50


def tickers_from_manifests(root: str) -> Set[str]:
    tickers: Set[str] = set()
    for path in glob.glob(os.path.join(root, "*", "manifest.json")):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, ValueError):
            continue
        for market in manifest.get("universe", {}).get("markets", []):
            ticker = market.get("ticker")
            if ticker:
                tickers.add(ticker)
    return tickers


def settlements_from_capture(root: str) -> Dict[str, Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    paths = glob.glob(os.path.join(root, "*", "capture-*.jsonl")) + glob.glob(
        os.path.join(root, "*", "capture-*.jsonl.gz")
    )
    for path in sorted(paths):
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    if exchange.LIFECYCLE_CHANNEL not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("k") != "ws":
                        continue
                    try:
                        payload = json.loads(record["raw"])
                    except (KeyError, ValueError):
                        continue
                    if payload.get("type") != exchange.LIFECYCLE_CHANNEL:
                        continue
                    body = payload.get("msg") or {}
                    value = body.get("settlement_value")
                    ticker = body.get("market_ticker")
                    if not ticker or value is None:
                        continue
                    found[ticker] = {
                        "settlement_value_dollars": str(value),
                        "result": body.get("result"),
                        "determination_ts": body.get("determination_ts"),
                        "settled_ts": body.get("settled_ts"),
                        "source": SOURCE_LIFECYCLE,
                        "observed_at_ns": record.get("recv_ns"),
                    }
        except OSError:
            continue
    return found


def settlements_from_rest(client: RestClient, tickers: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    batch: List[str] = []

    def flush() -> None:
        if not batch:
            return
        try:
            records = client.paginate(
                exchange.ENDPOINT_MARKETS, "markets", {"tickers": ",".join(batch)}
            )
        except Exception as error:
            print("warn: batch failed: %r" % (error,), file=sys.stderr)
            return
        for record in records:
            ticker = record.get("ticker")
            value = record.get("settlement_value_dollars")
            if not ticker or value is None:
                continue
            if str(record.get("status")) not in ("settled", "finalized", "determined"):
                continue
            found[ticker] = {
                "settlement_value_dollars": str(value),
                "result": record.get("result"),
                "status": record.get("status"),
                "close_time": record.get("close_time"),
                "source": SOURCE_REST,
            }

    for ticker in tickers:
        batch.append(ticker)
        if len(batch) >= REST_BATCH:
            flush()
            batch = []
    flush()
    return found


def build(root: str, use_rest: bool = True) -> Dict[str, Any]:
    tickers = tickers_from_manifests(root)
    captured = settlements_from_capture(root)

    merged: Dict[str, Dict[str, Any]] = dict(captured)
    rest_only = 0
    if use_rest:
        missing = sorted(tickers - set(captured))
        if missing:
            credentials = load_from_env()
            client = RestClient(credentials, RequestSigner(credentials))
            recovered = settlements_from_rest(client, missing)
            rest_only = len(recovered)
            merged.update(recovered)

    return {
        "root": root,
        "universe_tickers": len(tickers),
        "from_lifecycle": len(captured),
        "from_rest": rest_only,
        "settled_total": len(merged),
        "unsettled": len(tickers) - len(merged),
        "settlements": merged,
    }


def load_map(path: str) -> Dict[str, Decimal]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return {
        ticker: Decimal(body["settlement_value_dollars"])
        for ticker, body in (payload.get("settlements") or {}).items()
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect settled outcomes for captured markets, from lifecycle records and REST"
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--out", default="data/settlements.json")
    parser.add_argument("--no-rest", dest="use_rest", action="store_false", default=True)
    args = parser.parse_args(argv)

    report = build(args.data_root, use_rest=args.use_rest)
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, args.out)

    summary = {k: v for k, v in report.items() if k != "settlements"}
    summary["out"] = args.out
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
