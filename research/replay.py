from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from feed import recorder as rec
from feed.book import BookIntegrityError, BookSet, BookState
from ops.monitor import CaptureMonitor

BOOK_TYPES = ("orderbook_snapshot", "orderbook_delta")


@dataclass
class ReplayResult:
    session_id: str
    records: int = 0
    ws_messages: int = 0
    digests_checked: int = 0
    digest_mismatches: List[Dict[str, Any]] = field(default_factory=list)
    aggregate_mismatches: int = 0
    gaps_live: int = 0
    gaps_replayed: int = 0
    integrity_errors: int = 0
    message_types: Dict[str, int] = field(default_factory=dict)
    clock_offset: Dict[str, Any] = field(default_factory=dict)
    live_report: Optional[Dict[str, Any]] = None

    @property
    def verified(self) -> bool:
        return (
            self.digests_checked > 0
            and not self.digest_mismatches
            and self.aggregate_mismatches == 0
        )


def segment_paths(session_dir: str) -> List[str]:
    compressed = sorted(glob.glob(os.path.join(session_dir, "capture-*.jsonl.gz")))
    plain = [
        path
        for path in glob.glob(os.path.join(session_dir, "capture-*.jsonl"))
        if path + ".gz" not in compressed
    ]
    return sorted(compressed + plain, key=lambda p: p[:-3] if p.endswith(".gz") else p)


def read_records(session_dir: str) -> Iterator[Dict[str, Any]]:
    for path in segment_paths(session_dir):
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)


def replay_session(session_dir: str, strict_ordinals: bool = True) -> ReplayResult:
    manifest_path = os.path.join(session_dir, "manifest.json")
    session_id = os.path.basename(os.path.normpath(session_dir))
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as handle:
            session_id = json.load(handle).get("session_id", session_id)

    result = ReplayResult(session_id=session_id)
    books = BookSet()
    monitor = CaptureMonitor(started_mono=0.0)
    expected_ordinal = 0
    counts: Dict[str, int] = {}

    for record in read_records(session_dir):
        result.records += 1
        ordinal = record.get("n")
        expected_ordinal += 1
        if strict_ordinals and ordinal != expected_ordinal:
            raise ValueError(
                "log ordinal discontinuity in %s: expected %d, saw %r"
                % (session_dir, expected_ordinal, ordinal)
            )

        kind = record.get("k")
        if kind == rec.KIND_WS:
            _apply_ws_record(record, books, monitor, result, counts)
        elif kind == rec.KIND_SUBSCRIBED:
            payload = record.get("d") or {}
            sid = payload.get("sid")
            if payload.get("channel") == "orderbook_delta" and isinstance(sid, int):
                books.register(sid, "orderbook_delta", payload.get("tickers") or [])
        elif kind == rec.KIND_GAP:
            result.gaps_live += 1
        elif kind == rec.KIND_CONNECTION:
            if (record.get("d") or {}).get("event") == "reconnect_scheduled":
                books.reset()
        elif kind == rec.KIND_BOOK_DIGEST:
            _check_digest(record, books, result)
        elif kind == rec.KIND_SESSION_END:
            result.live_report = record.get("d")

    result.message_types = counts
    result.clock_offset = monitor.offsets.summary()
    return result


def _apply_ws_record(
    record: Dict[str, Any],
    books: BookSet,
    monitor: CaptureMonitor,
    result: ReplayResult,
    counts: Dict[str, int],
) -> None:
    result.ws_messages += 1
    payload = json.loads(record["raw"])
    message_type = payload.get("type")
    counts[str(message_type)] = counts.get(str(message_type), 0) + 1

    body = payload.get("msg") or {}
    ts_ms = body.get("ts_ms") if isinstance(body, dict) else None
    if isinstance(ts_ms, int) and ts_ms > 0:
        monitor.offsets.add(record["recv_ns"] // 1_000_000 - ts_ms)

    if message_type not in BOOK_TYPES:
        return

    sid = payload.get("sid")
    seq = payload.get("seq")
    ticker = body.get("market_ticker")
    if not isinstance(sid, int) or not isinstance(seq, int) or not isinstance(ticker, str):
        result.integrity_errors += 1
        return

    sub = books.subscription(sid)
    if sub is None:
        sub = books.register(sid, "orderbook_delta", [ticker])

    gap = sub.observe_seq(seq)
    if gap is not None:
        result.gaps_replayed += 1
        sub.invalidate_all()

    book = sub.book(ticker)
    try:
        if message_type == "orderbook_snapshot":
            book.apply_snapshot(body, seq)
        else:
            if book.state is not BookState.VALID:
                return
            book.apply_delta(body, seq)
    except BookIntegrityError:
        result.integrity_errors += 1
        book.invalidate()


def _check_digest(record: Dict[str, Any], books: BookSet, result: ReplayResult) -> None:
    payload = record.get("d") or {}
    live_digests = payload.get("digests") or {}
    result.digests_checked += 1

    replay_digests = {
        b.ticker: b.digest() for b in books.all_books() if b.state is BookState.VALID
    }

    for ticker, live in live_digests.items():
        replayed = replay_digests.get(ticker)
        if replayed != live:
            result.digest_mismatches.append(
                {
                    "record": record.get("n"),
                    "ticker": ticker,
                    "live": live,
                    "replay": replayed,
                }
            )
    for ticker in replay_digests:
        if ticker not in live_digests:
            result.digest_mismatches.append(
                {
                    "record": record.get("n"),
                    "ticker": ticker,
                    "live": None,
                    "replay": replay_digests[ticker],
                }
            )

    if payload.get("aggregate") != books.digest():
        result.aggregate_mismatches += 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild Phase 1 books offline and verify against live digests")
    parser.add_argument("session_dir")
    parser.add_argument("--allow-ordinal-gaps", action="store_true")
    parser.add_argument("--max-mismatches", type=int, default=20)
    args = parser.parse_args(argv)

    result = replay_session(args.session_dir, strict_ordinals=not args.allow_ordinal_gaps)
    print(
        json.dumps(
            {
                "session_id": result.session_id,
                "verified": result.verified,
                "records": result.records,
                "ws_messages": result.ws_messages,
                "message_types": result.message_types,
                "digests_checked": result.digests_checked,
                "digest_mismatches": result.digest_mismatches[: args.max_mismatches],
                "digest_mismatch_total": len(result.digest_mismatches),
                "aggregate_mismatches": result.aggregate_mismatches,
                "gaps_live": result.gaps_live,
                "gaps_replayed": result.gaps_replayed,
                "integrity_errors": result.integrity_errors,
                "clock_offset": result.clock_offset,
            },
            indent=2,
        )
    )
    return 0 if result.verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
