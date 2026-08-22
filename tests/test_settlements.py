from __future__ import annotations

import json
import os
from decimal import Decimal

from config import exchange
from research.settlements import (
    build,
    load_map,
    settlements_from_capture,
    tickers_from_manifests,
)


def write_session(root, name, tickers, lifecycle_rows=()):
    directory = os.path.join(root, name)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "session_id": name,
                "universe": {"markets": [{"ticker": t} for t in tickers], "universe_total": len(tickers)},
            },
            handle,
        )
    with open(os.path.join(directory, "capture-x-0000.jsonl"), "w", encoding="utf-8") as handle:
        for index, body in enumerate(lifecycle_rows, start=1):
            raw = json.dumps({"type": exchange.LIFECYCLE_CHANNEL, "sid": 9, "msg": body})
            handle.write(json.dumps({"n": index, "k": "ws", "recv_ns": index, "mono_ns": index, "raw": raw}) + "\n")


def test_tickers_are_read_from_every_manifest(tmp_path):
    write_session(str(tmp_path), "a", ["T-1", "T-2"])
    write_session(str(tmp_path), "b", ["T-2", "T-3"])
    assert tickers_from_manifests(str(tmp_path)) == {"T-1", "T-2", "T-3"}


def test_settled_value_is_extracted_from_lifecycle_records(tmp_path):
    write_session(
        str(tmp_path),
        "a",
        ["T-1"],
        [
            {"market_ticker": "T-1", "event_type": "determined",
             "result": "yes", "settlement_value": "1.0000", "determination_ts": 111},
            {"market_ticker": "T-1", "event_type": "settled", "settled_ts": 222},
        ],
    )
    found = settlements_from_capture(str(tmp_path))
    assert found["T-1"]["settlement_value_dollars"] == "1.0000"
    assert found["T-1"]["result"] == "yes"
    assert found["T-1"]["source"] == "lifecycle"


def test_lifecycle_events_without_a_settlement_value_are_ignored(tmp_path):
    write_session(
        str(tmp_path),
        "a",
        ["T-1"],
        [{"market_ticker": "T-1", "event_type": "created", "open_ts": 1}],
    )
    assert settlements_from_capture(str(tmp_path)) == {}


def test_build_without_rest_reports_what_is_still_unsettled(tmp_path):
    write_session(
        str(tmp_path),
        "a",
        ["T-1", "T-2"],
        [{"market_ticker": "T-1", "event_type": "determined", "settlement_value": "0.0000"}],
    )
    report = build(str(tmp_path), use_rest=False)
    assert report["universe_tickers"] == 2
    assert report["from_lifecycle"] == 1
    assert report["settled_total"] == 1
    assert report["unsettled"] == 1


def test_load_map_returns_decimals_for_the_analysis(tmp_path):
    path = os.path.join(str(tmp_path), "s.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"settlements": {"T-1": {"settlement_value_dollars": "0.5000"}}}, handle)
    mapping = load_map(path)
    assert mapping == {"T-1": Decimal("0.5000")}
    assert isinstance(mapping["T-1"], Decimal)
