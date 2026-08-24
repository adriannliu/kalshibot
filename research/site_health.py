from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
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


WATCHED_SERVICES = ("kalshi-universe.service", "kalshi-compress.service")
WATCHED_TIMERS = ("kalshi-universe.timer", "kalshi-compress.timer")
SHARD_UNIT = "kalshi-capture@%d.service"
UNIVERSE_MAX_AGE_HOURS = 36.0


def _systemctl(unit: str, properties: List[str]) -> Dict[str, str]:
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, "--no-pager"] + ["-p" + p for p in properties],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    values: Dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


def _units(root: str, shard_count: int) -> Dict[str, Any]:
    services = []
    for unit in WATCHED_SERVICES:
        shown = _systemctl(unit, ["Result", "ExecMainStatus", "ExecMainExitTimestamp", "ActiveState"])
        if not shown:
            continue
        services.append({
            "unit": unit,
            "result": shown.get("Result", "unknown"),
            "exit_status": shown.get("ExecMainStatus", ""),
            "last_finished": shown.get("ExecMainExitTimestamp", ""),
            "active_state": shown.get("ActiveState", ""),
            "healthy": shown.get("Result") == "success",
        })

    timers = []
    for unit in WATCHED_TIMERS:
        shown = _systemctl(unit, ["ActiveState", "LastTriggerUSec", "NextElapseUSecRealtime"])
        if not shown:
            continue
        timers.append({
            "unit": unit,
            "active_state": shown.get("ActiveState", ""),
            "last_trigger": shown.get("LastTriggerUSec", ""),
            "next_elapse": shown.get("NextElapseUSecRealtime", ""),
            "healthy": shown.get("ActiveState") == "active",
        })

    shards = []
    for index in range(max(1, shard_count)):
        unit = SHARD_UNIT % index
        shown = _systemctl(unit, ["ActiveState", "SubState", "NRestarts"])
        if not shown:
            continue
        shards.append({
            "unit": unit,
            "active_state": shown.get("ActiveState", ""),
            "sub_state": shown.get("SubState", ""),
            "restarts": int(shown.get("NRestarts") or 0),
            "healthy": shown.get("ActiveState") == "active" and shown.get("SubState") == "running",
        })

    universe_path = os.path.join(root, "universe.json")
    age_hours = None
    if os.path.exists(universe_path):
        age_hours = (time.time() - os.path.getmtime(universe_path)) / 3600.0

    return {
        "services": services,
        "timers": timers,
        "shards": shards,
        "universe_age_hours": age_hours,
        "universe_stale": age_hours is None or age_hours > UNIVERSE_MAX_AGE_HOURS,
        "all_healthy": (
            all(s["healthy"] for s in services)
            and all(t["healthy"] for t in timers)
            and all(s["healthy"] for s in shards)
            and age_hours is not None
            and age_hours <= UNIVERSE_MAX_AGE_HOURS
        ),
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
    units = _units(root, universe["shards"] or 4)
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
        "units": units,
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
