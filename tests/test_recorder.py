from __future__ import annotations

import json
import os

from feed.recorder import (
    KIND_HEALTH,
    KIND_WS,
    RecorderConfig,
    SessionRecorder,
    write_session_manifest,
)


def read_all(directory):
    records = []
    for name in sorted(os.listdir(directory)):
        if not name.startswith("capture-"):
            continue
        with open(os.path.join(directory, name), encoding="utf-8") as handle:
            for line in handle:
                records.append(json.loads(line))
    return records


def test_ordinals_are_contiguous_across_rotation(tmp_path):
    recorder = SessionRecorder(
        RecorderConfig(root=str(tmp_path), session_id="s", rotate_bytes=200, fsync_seconds=0.0)
    )
    for index in range(50):
        recorder.record_raw(json.dumps({"type": "trade", "n": index}), 1_000_000 * index, index)
    recorder.close()

    records = read_all(recorder.directory)
    assert [r["n"] for r in records] == list(range(1, 51))
    assert len([n for n in os.listdir(recorder.directory) if n.startswith("capture-")]) > 1


def test_raw_payload_is_preserved_verbatim(tmp_path):
    recorder = SessionRecorder(RecorderConfig(root=str(tmp_path), session_id="s"))
    raw = '{"type":"orderbook_delta","msg":{"price_dollars":"0.9600","delta_fp":"-54.00"}}'
    recorder.record_raw(raw, 1, 2)
    recorder.close()

    records = read_all(recorder.directory)
    assert records[0]["k"] == KIND_WS
    assert records[0]["raw"] == raw
    assert records[0]["recv_ns"] == 1


def test_local_and_exchange_timestamps_are_both_recoverable(tmp_path):
    recorder = SessionRecorder(RecorderConfig(root=str(tmp_path), session_id="s"))
    raw = json.dumps({"type": "trade", "msg": {"ts_ms": 1750000000000}})
    recorder.record_raw(raw, 1750000000123 * 1_000_000, 5)
    recorder.close()

    record = read_all(recorder.directory)[0]
    exchange_ts = json.loads(record["raw"])["msg"]["ts_ms"]
    assert record["recv_ns"] // 1_000_000 - exchange_ts == 123


def test_events_and_raw_share_one_ordinal_sequence(tmp_path):
    recorder = SessionRecorder(RecorderConfig(root=str(tmp_path), session_id="s"))
    recorder.record_raw("{}", 1, 1)
    recorder.record_event(KIND_HEALTH, {"ok": True})
    recorder.record_raw("{}", 2, 2)
    recorder.close()

    records = read_all(recorder.directory)
    assert [r["n"] for r in records] == [1, 2, 3]
    assert records[1]["d"] == {"ok": True}


def test_manifest_write_is_atomic(tmp_path):
    directory = str(tmp_path)
    path = write_session_manifest(directory, {"session_id": "abc"})
    assert os.path.exists(path)
    assert not os.path.exists(path + ".tmp")
    with open(path, encoding="utf-8") as handle:
        assert json.load(handle)["session_id"] == "abc"


def test_replay_prefers_compressed_segment_during_gzip_race(tmp_path):
    import gzip as gzip_module

    from research.replay import segment_paths

    directory = str(tmp_path)
    base = os.path.join(directory, "capture-20260820T100000Z-0000.jsonl")
    with open(base, "w", encoding="utf-8") as handle:
        handle.write('{"n":1}\n')
    with gzip_module.open(base + ".gz", "wt", encoding="utf-8") as handle:
        handle.write('{"n":1}\n')

    later = os.path.join(directory, "capture-20260820T110000Z-0001.jsonl")
    with open(later, "w", encoding="utf-8") as handle:
        handle.write('{"n":2}\n')

    paths = segment_paths(directory)
    assert paths == [base + ".gz", later]


def test_segments_are_ordered_by_rotation_time_not_extension(tmp_path):
    from research.replay import segment_paths

    directory = str(tmp_path)
    names = [
        "capture-20260820T090000Z-0000.jsonl.gz",
        "capture-20260820T100000Z-0001.jsonl.gz",
        "capture-20260820T110000Z-0002.jsonl",
    ]
    for name in names:
        open(os.path.join(directory, name), "w").close()

    assert [os.path.basename(p) for p in segment_paths(directory)] == names
