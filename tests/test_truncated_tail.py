from __future__ import annotations

import json
import os

import pytest

from research.replay import TruncatedFinalRecord, read_records, replay_session


def write_log(directory, lines, truncate_last=False):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "capture-20260101T000000Z-0000.jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        for index, body in enumerate(lines, start=1):
            handle.write(json.dumps({"n": index, "k": "health", "recv_ns": index, "mono_ns": index, "d": body}) + "\n")
        if truncate_last:
            handle.write('{"n":99,"k":"health","recv_ns":1,"mono_n')
    return path


def test_a_complete_log_reads_every_record(tmp_path):
    write_log(str(tmp_path), [{"a": 1}, {"a": 2}, {"a": 3}])
    assert len(list(read_records(str(tmp_path)))) == 3


def test_a_truncated_final_line_raises_by_default(tmp_path):
    write_log(str(tmp_path), [{"a": 1}, {"a": 2}], truncate_last=True)
    with pytest.raises(TruncatedFinalRecord):
        list(read_records(str(tmp_path)))


def test_complete_records_before_the_truncation_are_still_yielded(tmp_path):
    write_log(str(tmp_path), [{"a": 1}, {"a": 2}], truncate_last=True)
    seen = []
    with pytest.raises(TruncatedFinalRecord):
        for record in read_records(str(tmp_path)):
            seen.append(record)
    assert [r["n"] for r in seen] == [1, 2]


def test_replay_can_be_told_to_tolerate_a_truncated_tail(tmp_path):
    session = os.path.join(str(tmp_path), "s")
    write_log(session, [{"uptime_seconds": 1.0}], truncate_last=True)

    with pytest.raises(TruncatedFinalRecord):
        replay_session(session, strict_ordinals=False)

    result = replay_session(session, strict_ordinals=False, allow_truncated_tail=True)
    assert result.truncated_segments == 1
    assert result.records == 1


def test_corruption_that_is_not_the_final_line_still_raises(tmp_path):
    session = os.path.join(str(tmp_path), "s")
    os.makedirs(session, exist_ok=True)
    path = os.path.join(session, "capture-20260101T000000Z-0000.jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"n": 1, "k": "health", "d": {}}) + "\n")
        handle.write('{"n":2,"k":"heal\n')
        handle.write(json.dumps({"n": 3, "k": "health", "d": {}}) + "\n")

    with pytest.raises(ValueError):
        replay_session(session, strict_ordinals=False, allow_truncated_tail=True)
