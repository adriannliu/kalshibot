from __future__ import annotations

import json
import os

from research.phase1_report import (
    CONTIGUITY_TOLERANCE_SECONDS,
    REQUIRED_CAPTURE_DAYS,
    evaluate,
    merge_intervals,
)

DAY = 86400.0


def test_overlapping_intervals_merge_instead_of_summing():
    merged = merge_intervals([(0.0, DAY), (0.0, DAY), (0.0, DAY)], 0.0)
    assert merged == [(0.0, DAY)]


def test_small_restart_gaps_do_not_break_contiguity():
    merged = merge_intervals([(0.0, DAY), (DAY + 60.0, 2 * DAY)], CONTIGUITY_TOLERANCE_SECONDS)
    assert len(merged) == 1
    assert merged[0][1] - merged[0][0] == 2 * DAY


def test_a_real_outage_splits_the_span():
    merged = merge_intervals([(0.0, DAY), (DAY + 4 * 3600.0, 2 * DAY)], CONTIGUITY_TOLERANCE_SECONDS)
    assert len(merged) == 2


def write_session(root, name, start_ms, uptime, valid, gapped):
    directory = os.path.join(root, name)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(
            {"session_id": name, "started_at_ms": start_ms, "site": "test", "universe": {"count": 10}},
            handle,
        )
    with open(os.path.join(directory, "capture-x-0000.jsonl"), "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "n": 1,
                    "k": "session_end",
                    "recv_ns": 0,
                    "mono_ns": 0,
                    "d": {
                        "uptime_seconds": uptime,
                        "market_valid_seconds": valid,
                        "market_gapped_seconds": gapped,
                    },
                }
            )
            + "\n"
        )


def test_concurrent_shards_do_not_inflate_capture_duration(tmp_path):
    root = str(tmp_path)
    for shard in range(4):
        write_session(root, "shard%d" % shard, 0, 3 * DAY, 3 * DAY * 10, 0.0)

    report = evaluate(root, verify_replay=False)
    duration = next(c for c in report["criteria"] if c["name"] == "capture_duration")

    assert duration["detail"]["consecutive_days"] == 3.0
    assert not duration["passed"]


def test_consecutive_days_accumulate_across_daily_restarts(tmp_path):
    root = str(tmp_path)
    for day in range(REQUIRED_CAPTURE_DAYS + 1):
        write_session(
            root,
            "day%02d" % day,
            int(day * DAY * 1000),
            DAY - 60.0,
            (DAY - 60.0) * 10,
            0.0,
        )

    report = evaluate(root, verify_replay=False)
    duration = next(c for c in report["criteria"] if c["name"] == "capture_duration")
    assert duration["passed"]
    assert duration["detail"]["contiguous_spans"] == 1


def test_gapped_fraction_is_market_weighted_across_shards(tmp_path):
    root = str(tmp_path)
    write_session(root, "a", 0, DAY, 1000.0, 1.0)
    write_session(root, "b", 0, DAY, 1000.0, 3.0)

    report = evaluate(root, verify_replay=False)
    assert abs(report["totals"]["gapped_fraction"] - 4.0 / 2004.0) < 1e-12
