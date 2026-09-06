from __future__ import annotations

import json
import os

from feed import recorder as rec
from feed.book import BookSet
from research.replay import replay_session


def build_session(root, include_ok):
    directory = os.path.join(root, "s")
    os.makedirs(directory, exist_ok=True)
    ordinal = 0
    lines = []

    def add(kind, body=None, raw=None):
        nonlocal ordinal
        ordinal += 1
        record = {"n": ordinal, "k": kind, "recv_ns": ordinal, "mono_ns": ordinal}
        if raw is not None:
            record["raw"] = raw
        else:
            record["d"] = body
        lines.append(json.dumps(record))

    add(rec.KIND_SUBSCRIBED, {"sid": 1, "channel": "orderbook_delta", "tickers": ["A"]})
    add(rec.KIND_WS, raw=json.dumps({
        "type": "orderbook_snapshot", "sid": 1, "seq": 1,
        "msg": {"market_ticker": "A", "yes_dollars_fp": [["0.5000", "10.00"]]},
    }))
    if include_ok:
        add(rec.KIND_WS, raw=json.dumps({
            "type": "ok", "sid": 1, "id": 7, "msg": {"market_tickers": ["A", "B"]},
        }))

    books = BookSet()
    sub = books.register(1, "orderbook_delta", ["A"])
    sub.book("A").apply_snapshot({"yes_dollars_fp": [["0.5000", "10.00"]]}, 1)
    if include_ok:
        sub.book("B")

    add(rec.KIND_BOOK_DIGEST, {
        "aggregate": books.digest(),
        "valid_books": 1,
        "total_books": books.total_count(),
        "digests": {"A": sub.book("A").digest()},
    })

    with open(os.path.join(directory, "capture-20260101T000000Z-0000.jsonl"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return directory


def test_replay_matches_live_without_an_ok_response(tmp_path):
    result = replay_session(build_session(str(tmp_path), include_ok=False))
    assert result.verified
    assert result.aggregate_mismatches == 0


def test_an_ok_response_registers_the_same_books_live_and_on_replay(tmp_path):
    result = replay_session(build_session(str(tmp_path), include_ok=True))
    assert result.digest_mismatches == []
    assert result.aggregate_mismatches == 0
    assert result.verified
