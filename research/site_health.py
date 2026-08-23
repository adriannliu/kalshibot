from __future__ import annotations

import glob
import json
import os
import shutil
import time
from typing import Any, Dict, List

from research.phase1_report import evaluate, read_session_stats

GAP_MARKER = '"k":"gap"'
INTEGRITY_MARKER = '"k":"integrity"'
RECONNECT_MARKER = '"reconnect_scheduled"'
HEALTH_MARKER = '"k":"health"'


def _log_paths(root: str) -> List[str]:
    return glob.glob(os.path.join(root, "*", "capture-*.jsonl")) + glob.glob(
        os.path.join(root, "*", "capture-*.jsonl.gz")
    )


def _disk(root: str) -> Dict[str, Any]:
    usage = shutil.disk_usage(root if os.path.exists(root) else "/")
    paths = _log_paths(root)
    raw = sum(os.path.getsize(p) for p in paths if p.endswith(".jsonl"))
    compressed = sum(os.path.getsize(p) for p in paths if p.endswith(".gz"))
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_fraction": usage.used / usage.total if usage.total else 0.0,
        "capture_raw_bytes": raw,
        "capture_compressed_bytes": compressed,
        "segments_raw": sum(1 for p in paths if p.endswith(".jsonl")),
        "segments_compressed": sum(1 for p in paths if p.endswith(".gz")),
    }


def _universe(root: str) -> Dict[str, Any]:
    total = 0
    shards = set()
    sessions = 0
    for path in glob.glob(os.path.join(root, "*", "manifest.json")):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, ValueError):
            continue
        sessions += 1
        total = max(total, int(manifest.get("universe", {}).get("universe_total") or 0))
        index = manifest.get("shard_index")
        if isinstance(index, int):
            shards.add(index)
    return {"markets_total": total, "shards": len(shards), "sessions": sessions}


def _scan(root: str) -> Dict[str, Any]:
    gaps = reconnects = integrity = 0
    messages: Dict[str, int] = {}
    uptime = 0.0
    offset: Dict[str, Any] = {}
    markets = 0

    for directory in sorted(glob.glob(os.path.join(root, "*"))):
        if not os.path.isdir(directory):
            continue
        stats = read_session_stats(directory)
        if not stats:
            continue
        uptime = max(uptime, float(stats.get("uptime_seconds") or 0.0))
        markets += int(stats.get("markets_tracked") or 0)
        reconnects += int(stats.get("reconnects") or 0)
        integrity += int(stats.get("integrity_errors") or 0)
        for kind, count in (stats.get("gaps") or {}).items():
            gaps += int(count)
        for name, count in (stats.get("messages") or {}).items():
            messages[name] = messages.get(name, 0) + int(count)
        candidate = stats.get("clock_offset") or {}
        if candidate.get("count"):
            offset = candidate

    return {
        "gaps": gaps,
        "reconnects": reconnects,
        "integrity_errors": integrity,
        "messages": messages,
        "uptime_seconds": uptime,
        "markets_tracked": markets,
        "clock_offset": offset,
        "gaps_per_hour": (gaps / uptime * 3600.0) if uptime else 0.0,
    }


def collect(root: str = "data") -> Dict[str, Any]:
    report = evaluate(root, verify_replay=False)
    scan = _scan(root)
    disk = _disk(root)
    universe = _universe(root)
    if universe["markets_total"]:
        scan["markets_tracked"] = universe["markets_total"]

    raw_per_day = 0.0
    if scan["uptime_seconds"] > 0:
        raw_per_day = (
            (disk["capture_raw_bytes"] + disk["capture_compressed_bytes"])
            / scan["uptime_seconds"]
            * 86400.0
        )

    return {
        "site": os.environ.get("KALSHI_SITE", "unknown"),
        "hostname": os.uname().nodename,
        "collected_at_ms": int(time.time() * 1000),
        "shards": universe["shards"] or len(report["sessions"]),
        "sessions": universe["sessions"],
        "totals": report["totals"],
        "criteria": report["criteria"],
        "scan": scan,
        "disk": disk,
        "projection": {
            "bytes_per_day": raw_per_day,
            "bytes_14d": raw_per_day * 14,
            "days_until_full": (
                disk["free_bytes"] / raw_per_day if raw_per_day > 0 else None
            ),
        },
    }


if __name__ == "__main__":
    print(json.dumps(collect(os.environ.get("KALSHI_DATA_ROOT", "data")), default=str))
