from __future__ import annotations

import html
import json
import time
from typing import Any, Dict, List

GATE_GAPPED = 0.001
REQUIRED_DAYS = 14

CSS = """
:root{
  --ground:#F4F6F9; --surface:#FFFFFF; --surface-2:#EDF1F6;
  --ink:#10151F; --body:#39424F; --muted:#5A6474; --faint:#8A93A2;
  --rule:#DDE3EB; --rule-strong:#C3CCD8;
  --accent:#0B7285; --accent-soft:#E0F0F3;
  --good:#1F7A3D; --good-soft:#E3F3E8;
  --warn:#9A5B06; --warn-soft:#FBEEDB;
  --bad:#B02020;  --bad-soft:#FAE6E6;
  --shadow:0 1px 2px rgba(16,21,31,.06),0 8px 24px rgba(16,21,31,.05);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --ground:#0C1016; --surface:#141A23; --surface-2:#1B222D;
    --ink:#EEF2F7; --body:#C3CCD8; --muted:#98A3B2; --faint:#6E7988;
    --rule:#242D3A; --rule-strong:#33404F;
    --accent:#4FC3D4; --accent-soft:#12303A;
    --good:#5FD08A; --good-soft:#12301F;
    --warn:#E5A94F; --warn-soft:#332507;
    --bad:#F08B8B;  --bad-soft:#331616;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.3);
  }
}
:root[data-theme="dark"]{
  --ground:#0C1016; --surface:#141A23; --surface-2:#1B222D;
  --ink:#EEF2F7; --body:#C3CCD8; --muted:#98A3B2; --faint:#6E7988;
  --rule:#242D3A; --rule-strong:#33404F;
  --accent:#4FC3D4; --accent-soft:#12303A;
  --good:#5FD08A; --good-soft:#12301F;
  --warn:#E5A94F; --warn-soft:#332507;
  --bad:#F08B8B;  --bad-soft:#331616;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.3);
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--ground); color:var(--body);
  font-family:"IBM Plex Sans",ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;
  font-size:15px; line-height:1.55; -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1120px; margin:0 auto; padding:40px 24px 72px; display:flex; flex-direction:column; gap:32px}
h1,h2,h3{font-family:Archivo,ui-sans-serif,system-ui,sans-serif; color:var(--ink); margin:0; text-wrap:balance}
h1{font-size:31px; font-weight:700; letter-spacing:-.02em}
h2{font-size:13px; font-weight:600; text-transform:uppercase; letter-spacing:.10em; color:var(--muted)}
h3{font-size:17px; font-weight:600; letter-spacing:-.01em}
.mono{font-family:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace; font-variant-numeric:tabular-nums}
header .sub{color:var(--muted); font-size:14px; margin-top:6px}
header .sub b{color:var(--body); font-weight:600}
.verdict{
  display:flex; gap:18px; align-items:flex-start; padding:20px 22px;
  border-radius:10px; border:1px solid var(--rule); background:var(--surface);
  box-shadow:var(--shadow); border-left:4px solid var(--v);
}
.verdict.ok{--v:var(--good)} .verdict.warn{--v:var(--warn)} .verdict.bad{--v:var(--bad)}
.verdict .big{font-family:Archivo,sans-serif; font-size:20px; font-weight:700; color:var(--ink); letter-spacing:-.01em}
.verdict p{margin:6px 0 0; color:var(--muted); font-size:14px; max-width:68ch}
.grid{display:grid; gap:14px}
.g4{grid-template-columns:repeat(auto-fit,minmax(216px,1fr))}
.g2{grid-template-columns:repeat(auto-fit,minmax(400px,1fr))}
.tile{
  background:var(--surface); border:1px solid var(--rule); border-radius:10px;
  padding:16px 18px; border-left:3px solid var(--s); display:flex; flex-direction:column; gap:6px;
}
.tile.ok{--s:var(--good)} .tile.warn{--s:var(--warn)} .tile.bad{--s:var(--bad)} .tile.idle{--s:var(--rule-strong)}
.tile .label{font-size:11px; text-transform:uppercase; letter-spacing:.09em; color:var(--faint); font-weight:600}
.tile .value{font-family:"IBM Plex Mono",monospace; font-variant-numeric:tabular-nums; font-size:23px; color:var(--ink); font-weight:500; line-height:1.2}
.tile .note{font-size:12.5px; color:var(--muted)}
.pill{
  display:inline-flex; align-items:center; gap:6px; align-self:flex-start;
  font-size:11px; font-weight:700; letter-spacing:.07em; text-transform:uppercase;
  padding:3px 9px; border-radius:999px;
}
.pill.ok{background:var(--good-soft); color:var(--good)}
.pill.warn{background:var(--warn-soft); color:var(--warn)}
.pill.bad{background:var(--bad-soft); color:var(--bad)}
.pill.idle{background:var(--surface-2); color:var(--muted)}
.panel{background:var(--surface); border:1px solid var(--rule); border-radius:12px; box-shadow:var(--shadow); overflow:hidden}
.panel > .head{
  display:flex; justify-content:space-between; align-items:center; gap:12px;
  padding:15px 20px; border-bottom:1px solid var(--rule); background:var(--surface-2);
}
.panel > .head .where{font-size:12px; color:var(--muted)}
.panel .body{padding:6px 20px 16px}
.row{display:flex; justify-content:space-between; align-items:baseline; gap:16px; padding:9px 0; border-bottom:1px solid var(--rule)}
.row:last-child{border-bottom:0}
.row .k{color:var(--muted); font-size:13.5px}
.row .v{font-family:"IBM Plex Mono",monospace; font-variant-numeric:tabular-nums; color:var(--ink); font-size:14px; text-align:right}
.row .v.good{color:var(--good)} .row .v.warn{color:var(--warn)} .row .v.bad{color:var(--bad)}
.bar{height:5px; border-radius:3px; background:var(--surface-2); overflow:hidden; margin-top:9px}
.bar > i{display:block; height:100%; background:var(--f); border-radius:3px}
.bar.ok{--f:var(--good)} .bar.warn{--f:var(--warn)} .bar.bad{--f:var(--bad)}
table{width:100%; border-collapse:collapse; font-size:13.5px}
th{text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.08em; color:var(--faint); font-weight:600; padding:8px 10px; border-bottom:1px solid var(--rule-strong)}
td{padding:8px 10px; border-bottom:1px solid var(--rule); color:var(--body)}
td.n{font-family:"IBM Plex Mono",monospace; font-variant-numeric:tabular-nums; text-align:right; color:var(--ink)}
tr:last-child td{border-bottom:0}
.scroll{overflow-x:auto}
pre{
  margin:0; background:var(--surface-2); border:1px solid var(--rule); border-radius:8px;
  padding:14px 16px; overflow-x:auto; font-family:"IBM Plex Mono",monospace; font-size:13px;
  color:var(--ink); line-height:1.7;
}
pre .c{color:var(--faint)}
.note-block{font-size:13.5px; color:var(--muted); max-width:74ch}
.note-block strong{color:var(--ink); font-weight:600}
footer{border-top:1px solid var(--rule); padding-top:18px; color:var(--faint); font-size:12.5px}
a{color:var(--accent)}
:focus-visible{outline:2px solid var(--accent); outline-offset:2px; border-radius:4px}
@media (prefers-reduced-motion:reduce){*{animation:none!important; transition:none!important}}
"""


def _esc(value: Any) -> str:
    return html.escape(str(value))


def _bytes(value: Any) -> str:
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024:
            return "%.1f %s" % (value, unit)
        value /= 1024
    return "%.1f PB" % value


def _dur(seconds: Any) -> str:
    try:
        seconds = int(float(seconds or 0))
    except (TypeError, ValueError):
        return "—"
    if seconds <= 0:
        return "—"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return "%dd %dh" % (days, hours)
    if hours:
        return "%dh %dm" % (hours, minutes)
    return "%dm" % minutes


def _tile(label: str, value: str, note: str, state: str) -> str:
    return (
        '<div class="tile %s"><span class="label">%s</span>'
        '<span class="value">%s</span><span class="note">%s</span></div>'
        % (state, _esc(label), _esc(value), _esc(note))
    )


def _row(key: str, value: str, state: str = "") -> str:
    cls = (" " + state) if state else ""
    return '<div class="row"><span class="k">%s</span><span class="v%s">%s</span></div>' % (
        _esc(key), cls, _esc(value),
    )


def render(sites: List[Dict[str, Any]]) -> str:
    live = [s for s in sites if s.get("reachable")]

    consecutive = max(
        (s["totals"]["consecutive_capture_days"] for s in live if s.get("totals")), default=0.0
    )
    worst_gapped = max((s["totals"]["gapped_fraction"] for s in live if s.get("totals")), default=1.0)
    integrity = sum(s["scan"]["integrity_errors"] for s in live if s.get("scan"))
    soonest_full = min(
        (s["projection"]["days_until_full"] for s in live
         if s.get("projection") and s["projection"].get("days_until_full")),
        default=None,
    )
    markets = max((s["scan"]["markets_tracked"] for s in live if s.get("scan")), default=0)
    unreachable = [s for s in sites if not s.get("reachable")]

    compression_seen = any(s["disk"]["segments_compressed"] > 0 for s in live if s.get("disk"))
    youngest_uptime = min(
        (s["scan"]["uptime_seconds"] for s in live if s.get("scan")), default=0.0
    )
    compression_due = youngest_uptime > 3 * 3600

    gap_state = "ok" if worst_gapped < GATE_GAPPED else "bad"
    disk_ample = (soonest_full or 999) > REQUIRED_DAYS
    if disk_ample:
        disk_state = "ok"
    elif compression_seen or compression_due:
        disk_state = "bad"
    else:
        disk_state = "warn"
    integ_state = "ok" if integrity == 0 else "bad"
    days_state = "ok" if consecutive >= REQUIRED_DAYS else "idle"

    blocking = []
    if unreachable:
        blocking.append("%d site(s) unreachable" % len(unreachable))
    if worst_gapped >= GATE_GAPPED:
        blocking.append("gapped fraction above the 0.1%% gate")
    if integrity:
        blocking.append("%d integrity error(s)" % integrity)
    if soonest_full is not None and soonest_full <= REQUIRED_DAYS and disk_state == "bad":
        blocking.append("disk fills in %.1f days, before the 14-day mark" % soonest_full)

    pending = []
    if not compression_seen and not compression_due:
        pending.append(
            "compression has not run yet, so the disk projection below is raw-only and will "
            "improve roughly twelvefold once the first segments pass two hours old"
        )

    if blocking:
        verdict_cls = "bad" if (unreachable or integrity or worst_gapped >= GATE_GAPPED) else "warn"
        headline = "Capture is at risk"
        detail = "Blocking the 14-day clock: " + "; ".join(blocking) + "."
    else:
        verdict_cls = "ok"
        headline = "Capture is healthy — %.2f of 14 days banked" % consecutive
        detail = (
            "All four Phase 1 exit criteria are on track. The clock resets only if capture "
            "stops for more than 10 minutes, so the thing to protect is continuity, not throughput."
        )
    if pending:
        detail += " Note: " + "; ".join(pending) + "."

    tiles = "".join([
        _tile("Consecutive days", "%.2f" % consecutive, "need %d — longest unbroken span" % REQUIRED_DAYS, days_state),
        _tile("Gapped fraction", "%.5f" % worst_gapped, "gate is < %.3f — worst site" % GATE_GAPPED, gap_state),
        _tile("Integrity errors", str(integrity), "any value above zero is a bug", integ_state),
        _tile(
            "Disk headroom",
            ("%.1f d" % soonest_full) if soonest_full else "—",
            "until the fuller volume is full"
            + ("" if compression_seen else " — before compression"),
            disk_state,
        ),
    ])

    panels = []
    for site in sites:
        name = _esc(site.get("site", "?"))
        if not site.get("reachable"):
            panels.append(
                '<div class="panel"><div class="head"><h3>%s</h3>'
                '<span class="pill bad">unreachable</span></div>'
                '<div class="body"><p class="note-block">%s</p></div></div>'
                % (name, _esc(site.get("error", "no response")))
            )
            continue

        scan, totals, disk, proj = site["scan"], site["totals"], site["disk"], site["projection"]
        offset = scan.get("clock_offset") or {}
        gapped = totals["gapped_fraction"]
        used = disk["used_fraction"]
        days_full = proj.get("days_until_full")

        g_state = "good" if gapped < GATE_GAPPED else "bad"
        d_state = "good" if (days_full or 999) > REQUIRED_DAYS else "bad"
        compressed_share = 0.0
        total_capture = disk["capture_raw_bytes"] + disk["capture_compressed_bytes"]
        if total_capture:
            compressed_share = disk["capture_compressed_bytes"] / total_capture

        bar_cls = "ok" if used < 0.6 else ("warn" if used < 0.85 else "bad")
        panels.append(
            '<div class="panel"><div class="head">'
            '<div><h3>%s</h3><div class="where mono">%s &middot; %s</div></div>'
            '<span class="pill %s">%s</span></div><div class="body">%s'
            '<div class="bar %s"><i style="width:%.1f%%"></i></div>'
            '</div></div>' % (
                name,
                _esc(site.get("region", "")),
                _esc(site.get("ip", "")),
                "ok" if gapped < GATE_GAPPED and scan["integrity_errors"] == 0 else "warn",
                "capturing",
                "".join([
                    _row("Uptime (longest shard)", _dur(scan["uptime_seconds"])),
                    _row("Shards", str(site.get("shards", 0))),
                    _row("Markets tracked", "{:,}".format(scan["markets_tracked"])),
                    _row("Gapped fraction", "%.5f" % gapped, g_state),
                    _row("Sequence gaps / hour", "%.1f" % scan["gaps_per_hour"]),
                    _row("Reconnects", str(scan["reconnects"]), "" if scan["reconnects"] == 0 else "warn"),
                    _row("Integrity errors", str(scan["integrity_errors"]), "good" if scan["integrity_errors"] == 0 else "bad"),
                    _row("Clock offset p50 / p95 / p99",
                         "%s / %s / %s ms" % (offset.get("p50_ms", "—"), offset.get("p95_ms", "—"), offset.get("p99_ms", "—"))),
                    _row("Orderbook deltas", "{:,}".format(scan["messages"].get("orderbook_delta", 0))),
                    _row("Trades", "{:,}".format(scan["messages"].get("trade", 0))),
                    _row("Capture on disk", "%s raw + %s gz (%.0f%% packed)" % (
                        _bytes(disk["capture_raw_bytes"]), _bytes(disk["capture_compressed_bytes"]), compressed_share * 100)),
                    _row("Growth", "%s / day" % _bytes(proj.get("bytes_per_day"))),
                    _row("Volume used", "%s of %s (%.1f%%)" % (
                        _bytes(disk["used_bytes"]), _bytes(disk["total_bytes"]), used * 100)),
                    _row("Days until full", ("%.1f" % days_full) if days_full else "—", d_state),
                ]),
                bar_cls, min(100.0, used * 100),
            )
        )

    criteria_rows = []
    for site in live:
        for crit in site.get("criteria", []):
            criteria_rows.append((site["site"], crit))
    seen = set()
    crit_html = []
    for name, crit in criteria_rows:
        key = crit["name"]
        if key in seen:
            continue
        seen.add(key)
        state = "ok" if crit["passed"] else "idle"
        crit_html.append(
            "<tr><td>%s</td><td><span class='pill %s'>%s</span></td><td class='n'>%s</td></tr>"
            % (_esc(key.replace("_", " ")), state, "pass" if crit["passed"] else "pending",
               _esc(json.dumps(crit["detail"])[:120]))
        )

    stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    return """<title>Kalshi Capture Watch</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;600&display=swap">
<style>%s</style>
<div class="wrap">
  <header>
    <h1>Kalshi Capture Watch</h1>
    <div class="sub">Phase 1 read-only capture &middot; <b>%d markets</b> captured independently at <b>%d sites</b> &middot; generated %s</div>
  </header>

  <div class="verdict %s">
    <div><div class="big">%s</div><p>%s</p></div>
  </div>

  <section>
    <h2>Phase 1 exit criteria</h2>
    <div class="grid g4" style="margin-top:12px">%s</div>
  </section>

  <section>
    <h2>Sites</h2>
    <div class="grid g2" style="margin-top:12px">%s</div>
  </section>

  <section>
    <h2>Criterion detail</h2>
    <div class="panel" style="margin-top:12px"><div class="scroll"><table>
      <thead><tr><th>Criterion</th><th>State</th><th style="text-align:right">Detail</th></tr></thead>
      <tbody>%s</tbody>
    </table></div></div>
  </section>

  <section>
    <h2>There is no trading kill switch, and that is deliberate</h2>
    <p class="note-block" style="margin-top:12px">This system is <strong>read-only</strong>. It has no execution engine, no order
    state machine, and no position — so there is nothing to halt and no capital to protect. The safeguard is structural:
    <code>tests/test_layering.py</code> fails the build if any module in the trading path gains an order-submission call site.
    The kill switches in &sect;7 of the north star — stale feed, position mismatch, inventory breach, daily P&amp;L floor,
    sustained 429s — get built in <strong>Phase 5</strong>, against a demo account, behind chaos testing. Building them now would
    imply the system is safe to trade before Phase 2 has said whether an edge exists at all.</p>
  </section>

  <section>
    <h2>Controls</h2>
    <pre style="margin-top:12px"><span class="c"># stop capture at one site (data is preserved)</span>
ssh ec2-user@HOST 'sudo systemctl stop kalshi-capture@{0,1,2,3}'

<span class="c"># start it again — books rebuild from fresh snapshots</span>
ssh ec2-user@HOST 'sudo systemctl start kalshi-capture@{0,1,2,3}'

<span class="c"># refresh this dashboard</span>
python -m ops.dashboard --out capture-dashboard.html

<span class="c"># full verification: replay every book and compare to live digests</span>
ssh ec2-user@HOST 'cd ~/kalshibot &amp;&amp; ./.venv/bin/python -m research.phase1_report --data-root data'</pre>
  </section>

  <footer>Snapshot, not live &mdash; re-run <span class="mono">python -m ops.dashboard</span> to refresh. Phase 2 is a GO/NO-GO gate; do not build the bot until it passes.</footer>
</div>
""" % (CSS, markets, len(sites), _esc(stamp), verdict_cls, _esc(headline), _esc(detail),
       tiles, "".join(panels), "".join(crit_html))
