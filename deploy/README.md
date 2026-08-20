# EC2 deployment — Phase 1 capture

Two instances in two regions, four capture shards each, one shared universe
snapshot per instance refreshed daily.

## Instances

| | |
|---|---|
| Type | `t4g.small` (2 vCPU, 2 GiB, Graviton) — ~$12/mo each |
| Regions | `us-east-1` (site `use1`) and `us-west-2` (site `usw2`) |
| AMI | Amazon Linux 2023 (arm64) |
| Storage | 200 GiB `gp3` |
| Security group | Outbound 443 only; inbound SSH from your IP |

**A bigger instance buys nothing here.** The workload is four WebSocket connections
and some dict updates. §8 is explicit that latency is not the binding constraint on
long-tail markets, and §9 lists optimising latency before establishing edge as an
anti-pattern. Credits are better spent on the second region and on disk.

200 GiB is deliberately generous: 492 markets is ~3.6x the original universe, so the
10–30 GiB estimate scales to roughly 40–120 GiB for 14 days. That estimate is still
unmeasured. Check `du -sh data/` after day one and resize if it is wildly off.

## Why two regions

14 *consecutive* days is the longest pole in Phase 1. A single instance failing on
day 12 restarts the clock. The second region also does something insurance cannot:
when `use1` logs a sequence gap and `usw2` does not, that separates an exchange-side
drop from our own network. Against a 0.1% gate, that diagnostic decides whether a
rising gap rate is a bug or weather.

Each instance sets `KALSHI_SITE` and stamps it into every session manifest, so the
merged corpus stays attributable.

## Clock sync is not optional

Exit criterion 3 is *characterising the clock offset distribution*. If the instance
clock drifts we measure our own drift and call it exchange latency — and it fails
silently, poisoning every `recv_ms - ts_ms` figure in the phase report.

```
chronyc tracking      # System time within a few ms, Leap status Normal
chronyc sources -v    # expect 169.254.169.123
```

AL2023 ships this configured. Verify before capture starts and again at the end.

## Setup (repeat per instance)

```bash
sudo dnf install -y git python3.11 python3.11-pip
git clone <your-repo> ~/kalshibot && cd ~/kalshibot
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p ~/kalshibot/data ~/.kalshi
```

Copy the private key up — never commit it, never bake it into an AMI:

```bash
scp -i <your.pem> ~/.kalshi/kalshi_key.pem ec2-user@<host>:~/.kalshi/kalshi_key.pem
ssh ec2-user@<host> 'chmod 600 ~/.kalshi/kalshi_key.pem'
```

Create `/home/ec2-user/.kalshi/env`. **systemd's `EnvironmentFile` does not accept
`export`** — plain `KEY=value`, no quotes:

```
KALSHI_API_KEY_ID=...
KALSHI_PRIVATE_KEY_PATH=/home/ec2-user/.kalshi/kalshi_key.pem
KALSHI_ENV=prod
KALSHI_SITE=use1
KALSHI_SHARD_COUNT=4
```

```bash
chmod 600 ~/.kalshi/env
```

Verify, then take the first universe snapshot:

```bash
cd ~/kalshibot
set -a && . ~/.kalshi/env && set +a
.venv/bin/python -m ops.preflight
.venv/bin/python -m ops.resolve_universe --out data/universe.json --shard-count 4
```

## Install

```bash
sudo cp deploy/kalshi-capture@.service deploy/kalshi-capture.target \
        deploy/kalshi-universe.{service,timer} \
        deploy/kalshi-compress.{service,timer} /etc/systemd/system/
sudo install -m 440 -o root -g root deploy/kalshi-sudoers /etc/sudoers.d/kalshi
sudo visudo -c
sudo systemctl daemon-reload
sudo systemctl enable --now kalshi-capture@{0,1,2,3}.service
sudo systemctl enable --now kalshi-universe.timer kalshi-compress.timer
```

## How the daily cycle works

`kalshi-universe.timer` fires at 09:15 UTC. `refresh-universe.sh` resolves the
universe into a staging file, and **only replaces `universe.json` if resolution
succeeded** — a failed refresh leaves yesterday's universe in place and capture keeps
running on it rather than dying. It then restarts shards one at a time with 15s
between them, so capture never stops globally; at any instant at most one quarter of
one site is re-subscribing.

The daily blind spot is therefore per shard, roughly 30–60s while that shard
re-resolves, not a global outage. That time is absent from the corpus and is not
counted as gapped. `research/phase1_report.py` tolerates gaps up to 600s when
measuring contiguity, so the daily cycle does not break the 14-day span.

Shards are partitioned **by series**, never round-robin. §5 wants a scanner for
monotonicity violations across a strike ladder, and splitting an event's strikes
across processes would put them on different connections with different latencies and
no shared sequence stream — you could not distinguish a real violation from a timing
artifact. Every series stays whole inside one process; shards are balanced by greedy
bin-packing.

## Watching it

```bash
systemctl status 'kalshi-capture@*'
journalctl -u 'kalshi-capture@*' -f
du -sh ~/kalshibot/data/

cd ~/kalshibot && .venv/bin/python -m research.phase1_report --data-root data
```

Run the report daily; it exits nonzero until all four criteria pass. Watch
`gapped_fraction` against the 0.001 gate. `--skip-replay` gives a fast answer from
the health records without rebuilding books.

To merge both sites for the final report, rsync both `data/` trees into one directory
— session ids carry a uuid suffix and manifests carry `site`, so they will not
collide.
