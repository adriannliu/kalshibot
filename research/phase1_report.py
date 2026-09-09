from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from feed import recorder as rec
from research.replay import read_records, replay_session, segment_paths

MAX_GAPPED_FRACTION = 0.001
REQUIRED_CAPTURE_DAYS = 14
CONTIGUITY_TOLERANCE_SECONDS = 600.0


@dataclass
class Criterion:
    name: str
    passed: bool
    detail: Any


def read_session_stats(directory: str) -> Dict[str, Any]:
    for path in reversed(segment_paths(directory)):
        latest: Dict[str, Any] = {}
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    if '"session_end"' not in line and '"health"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("k") in (rec.KIND_SESSION_END, rec.KIND_HEALTH):
                        latest = record.get("d") or {}
        except OSError:
            continue
        if latest:
            return latest
    return {}


class UnreadableSession(RuntimeError):
    pass


def merge_intervals(
    intervals: List[Tuple[float, float]], tolerance: float
) -> List[Tuple[float, float]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start - merged[-1][1] <= tolerance:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def session_dirs(root: str) -> List[str]:
    return sorted(p for p in glob.glob(os.path.join(root, "*")) if os.path.isdir(p))


def evaluate(root: str, verify_replay: bool = True) -> Dict[str, Any]:
    sessions: List[Dict[str, Any]] = []
    intervals: List[Tuple[float, float]] = []
    sites: set = set()
    total_valid = 0.0
    total_gapped = 0.0
    replay_failures: List[str] = []
    unreadable: List[str] = []
    offsets: List[Dict[str, Any]] = []

    for directory in session_dirs(root):
        manifest_path = os.path.join(directory, "manifest.json")
        if not os.path.exists(manifest_path):
            continue
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)

        entry: Dict[str, Any] = {
            "session_id": manifest.get("session_id"),
            "directory": directory,
            "environment": manifest.get("environment"),
            "site": manifest.get("site"),
            "shard": "%s/%s" % (manifest.get("shard_index"), manifest.get("shard_count")),
            "markets": manifest.get("universe", {}).get("count"),
        }
        if manifest.get("site"):
            sites.add(manifest["site"])

        live = read_session_stats(directory)
        if not live:
            entry["stats_unreadable"] = True
            unreadable.append(directory)
        result = replay_session(directory, strict_ordinals=True) if verify_replay else None
        if result is not None:
            entry["replay_verified"] = result.verified
            entry["digests_checked"] = result.digests_checked
            entry["digest_mismatches"] = len(result.digest_mismatches)
            entry["gaps_live"] = result.gaps_live
            entry["clock_offset"] = result.clock_offset
            if result.clock_offset.get("count"):
                offsets.append(result.clock_offset)
            if not result.verified:
                replay_failures.append(directory)
            live = result.live_report or live

        uptime = float(live.get("uptime_seconds") or 0.0)
        started_ms = manifest.get("started_at_ms")
        if uptime > 0 and isinstance(started_ms, (int, float)):
            start_s = float(started_ms) / 1000.0
            intervals.append((start_s, start_s + uptime))
        total_valid += float(live.get("market_valid_seconds") or 0.0)
        total_gapped += float(live.get("market_gapped_seconds") or 0.0)
        entry["uptime_seconds"] = uptime
        entry["gapped_fraction"] = live.get("gapped_fraction")
        sessions.append(entry)

    weighted = total_valid + total_gapped
    gapped_fraction = (total_gapped / weighted) if weighted else 1.0

    spans = merge_intervals(intervals, CONTIGUITY_TOLERANCE_SECONDS)
    longest_span = max((end - start for start, end in spans), default=0.0)
    capture_days = longest_span / 86400.0
    covered_days = sum(end - start for start, end in spans) / 86400.0

    criteria = [
        Criterion(
            "capture_duration",
            capture_days >= REQUIRED_CAPTURE_DAYS,
            {
                "consecutive_days": capture_days,
                "total_covered_days": covered_days,
                "required": REQUIRED_CAPTURE_DAYS,
                "contiguous_spans": len(spans),
                "tolerance_seconds": CONTIGUITY_TOLERANCE_SECONDS,
            },
        ),
        Criterion(
            "gapped_state_fraction",
            gapped_fraction < MAX_GAPPED_FRACTION,
            {"fraction": gapped_fraction, "max": MAX_GAPPED_FRACTION},
        ),
        Criterion(
            "offline_reconstruction",
            verify_replay and not replay_failures and bool(sessions),
            {"failed_sessions": replay_failures, "sessions": len(sessions)},
        ),
        Criterion(
            "every_session_accounted_for",
            not unreadable,
            {"sessions_with_unreadable_stats": unreadable},
        ),
        Criterion(
            "clock_offset_characterized",
            bool(offsets),
            {"sessions_with_offset_samples": len(offsets)},
        ),
    ]

    return {
        "root": root,
        "sessions": sessions,
        "sites": sorted(sites),
        "totals": {
            "consecutive_capture_days": capture_days,
            "total_covered_days": covered_days,
            "market_valid_seconds": total_valid,
            "market_gapped_seconds": total_gapped,
            "gapped_fraction": gapped_fraction,
        },
        "criteria": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in criteria],
        "phase1_exit_criteria_met": all(c.passed for c in criteria),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate Phase 1 exit criteria across capture sessions")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--skip-replay", action="store_true")
    args = parser.parse_args(argv)

    report = evaluate(args.data_root, verify_replay=not args.skip_replay)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["phase1_exit_criteria_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
