from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

DEFAULT_PROFILE = "kalshibot"
DEFAULT_REGIONS = ("us-east-1", "us-west-2")
REMOTE = (
    "cd ~/kalshibot && set -a && . ~/.kalshi/env && set +a && "
    "./.venv/bin/python -m research.site_health"
)


def discover(profile: str, regions: List[str]) -> List[Dict[str, str]]:
    found = []
    for region in regions:
        try:
            raw = subprocess.run(
                [
                    "aws", "ec2", "describe-instances",
                    "--profile", profile, "--region", region,
                    "--filters",
                    "Name=tag:Project,Values=kalshibot",
                    "Name=instance-state-name,Values=running",
                    "--query",
                    "Reservations[].Instances[].{ip:PublicIpAddress,"
                    "site:Tags[?Key=='Site']|[0].Value,id:InstanceId}",
                    "--output", "json",
                ],
                capture_output=True, text=True, timeout=60, check=True,
            ).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            print("warn: %s discovery failed: %r" % (region, error), file=sys.stderr)
            continue
        for row in json.loads(raw or "[]"):
            if row.get("ip"):
                found.append(
                    {"site": row.get("site") or row["id"], "ip": row["ip"], "region": region}
                )
    return sorted(found, key=lambda r: r["site"])


def poll(host: Dict[str, str], timeout: int = 180) -> Dict[str, Any]:
    try:
        raw = subprocess.run(
            ["ssh", "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=20",
             "-o", "BatchMode=yes", "ec2-user@" + host["ip"], REMOTE],
            capture_output=True, text=True, timeout=timeout, check=True,
        ).stdout
        payload = json.loads(raw)
        payload["reachable"] = True
    except Exception as error:
        payload = {"site": host["site"], "reachable": False, "error": repr(error)[:300]}
    payload["region"] = host["region"]
    payload["ip"] = host["ip"]
    return payload


def human_bytes(value: Optional[float]) -> str:
    if not value:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024:
            return "%.1f %s" % (value, unit)
        value /= 1024
    return "%.1f PB" % value


def human_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "—"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return "%dd %dh" % (days, hours)
    if hours:
        return "%dh %dm" % (hours, minutes)
    return "%dm" % minutes


def build(sites: List[Dict[str, Any]], out_path: str) -> Dict[str, Any]:
    from ops.dashboard_render import render

    html = render(sites)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(html)
    return {"path": out_path, "sites": len(sites)}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Render the Phase 1 capture dashboard")
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--region", action="append", default=None)
    parser.add_argument("--host", action="append", default=None,
                        help="site=ip, skips AWS discovery")
    parser.add_argument("--out", default="capture-dashboard.html")
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)

    if args.host:
        hosts = []
        for entry in args.host:
            site, _, ip = entry.partition("=")
            hosts.append({"site": site, "ip": ip, "region": "manual"})
    else:
        hosts = discover(args.profile, args.region or list(DEFAULT_REGIONS))

    if not hosts:
        print("no running capture instances found", file=sys.stderr)
        return 1

    sites = [poll(h) for h in hosts]
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(sites, handle, indent=2, default=str)

    result = build(sites, args.out)
    reachable = sum(1 for s in sites if s.get("reachable"))
    print(json.dumps({**result, "reachable": reachable, "total": len(sites)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
