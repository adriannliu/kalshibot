from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, TextIO

LOG_FORMAT_VERSION = 1

KIND_WS = "ws"
KIND_SESSION_START = "session_start"
KIND_SESSION_END = "session_end"
KIND_SUBSCRIBED = "subscribed"
KIND_GAP = "gap"
KIND_RESYNC = "resync"
KIND_BOOK_DIGEST = "book_digest"
KIND_HEALTH = "health"
KIND_INTEGRITY = "integrity"
KIND_CONNECTION = "connection"
KIND_COMMAND = "command"


def utc_stamp(ns: Optional[int] = None) -> str:
    seconds = (time.time_ns() if ns is None else ns) / 1e9
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(seconds))


@dataclass(frozen=True)
class RecorderConfig:
    root: str
    session_id: str
    rotate_bytes: int = 512 * 1024 * 1024
    rotate_seconds: int = 3600
    fsync_seconds: float = 5.0


class SessionRecorder:
    def __init__(self, config: RecorderConfig) -> None:
        self._config = config
        self._dir = os.path.join(config.root, config.session_id)
        os.makedirs(self._dir, exist_ok=True)
        self._handle: Optional[TextIO] = None
        self._path: Optional[str] = None
        self._bytes = 0
        self._opened_at = 0.0
        self._last_fsync = 0.0
        self._ordinal = 0
        self._rotations = 0
        self._open_segment()

    @property
    def directory(self) -> str:
        return self._dir

    @property
    def path(self) -> Optional[str]:
        return self._path

    @property
    def records_written(self) -> int:
        return self._ordinal

    def _open_segment(self) -> None:
        self._close_segment()
        name = "capture-%s-%04d.jsonl" % (utc_stamp(), self._rotations)
        self._path = os.path.join(self._dir, name)
        self._handle = open(self._path, "a", buffering=1024 * 256, encoding="utf-8")
        self._bytes = 0
        self._opened_at = time.monotonic()
        self._last_fsync = self._opened_at
        self._rotations += 1

    def _close_segment(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()
            self._handle = None

    def _maybe_rotate(self) -> None:
        if self._bytes >= self._config.rotate_bytes:
            self._open_segment()
        elif time.monotonic() - self._opened_at >= self._config.rotate_seconds:
            self._open_segment()

    def _write(self, record: Dict[str, Any]) -> None:
        assert self._handle is not None
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        self._handle.write(line)
        self._handle.write("\n")
        self._bytes += len(line) + 1
        now = time.monotonic()
        if now - self._last_fsync >= self._config.fsync_seconds:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._last_fsync = now
        self._maybe_rotate()

    def record_raw(self, raw: str, recv_ns: int, mono_ns: int) -> int:
        self._ordinal += 1
        self._write(
            {
                "n": self._ordinal,
                "k": KIND_WS,
                "recv_ns": recv_ns,
                "mono_ns": mono_ns,
                "raw": raw,
            }
        )
        return self._ordinal

    def record_event(self, kind: str, payload: Dict[str, Any]) -> int:
        self._ordinal += 1
        self._write(
            {
                "n": self._ordinal,
                "k": kind,
                "recv_ns": time.time_ns(),
                "mono_ns": time.monotonic_ns(),
                "d": payload,
            }
        )
        return self._ordinal

    def flush(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._last_fsync = time.monotonic()

    def close(self) -> None:
        self._close_segment()


def write_session_manifest(directory: str, manifest: Dict[str, Any]) -> str:
    path = os.path.join(directory, "manifest.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path
