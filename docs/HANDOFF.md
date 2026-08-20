# Handoff — Kalshi market making bot

**Written:** 2026-08-20
**Phase:** 1 (read-only capture), running. Phase 2 not started.
**Capital at risk:** none. There is no order-submission code anywhere in this repo.

Read [`KALSHI_MM_NORTH_STAR.md`](../KALSHI_MM_NORTH_STAR.md) first. It is the contract.
This document says what is true right now; the north star says what the project is for.

---

## 1. The one rule that matters

> Phase 2 analysis gates Phase 3+. Do not write an execution engine, an order state
> machine, or any code that can submit an order until the Phase 2 exit criteria are
> met with real recorded data.

If Phase 2 says there is no edge, the correct outcome is to **stop and pivot**, not to
build anyway. Discovering "no edge" now costs five weeks and zero dollars.

`tests/test_layering.py` enforces this mechanically: it fails the build if any module
under `config/ auth/ feed/ backtest/ model/ exec/ ops/` contains
`/portfolio/events/orders`, `create_order`, or `submit_order`. Do not weaken that test
to make something pass.

The user has asked for risk limits and a panic button. Both are **Phase 5** work. Right
now there is nothing to halt and no capital to protect, and shipping a panic button
would imply the system is safe to trade before Phase 2 has said whether an edge exists.
That reasoning is on the dashboard so it does not get quietly lost.

## 2. What is running

Two EC2 instances, four capture shards each, started 2026-08-20.

| Site | Region | Instance | Type |
|---|---|---|---|
| `use1` | us-east-1 | `i-0f55212c56977dfe7` | t4g.small, 200 GiB gp3 |
| `usw2` | us-west-2 | `i-046439a0331c7344c` | t4g.small, 200 GiB gp3 |

Public IPs are **not** elastic and change across a stop/start. Discover them by tag:

```bash
aws ec2 describe-instances --profile kalshibot --region us-east-1 \
  --filters Name=tag:Project,Values=kalshibot Name=instance-state-name,Values=running \
  --query "Reservations[].Instances[].{ip:PublicIpAddress,site:Tags[?Key=='Site']|[0].Value}"
```

Each site captures the **same 492 markets** across 78 series — deliberately, so gap
events can be compared between sites to separate exchange-side drops from our own
network. Shards partition by series (never round-robin) so a strike ladder stays whole
inside one process.

Systemd units, the daily universe refresh, and the hourly compressor are described in
[`deploy/README.md`](../deploy/README.md).

## 3. Access

- **AWS:** account `073158194660`, CLI profile **`kalshibot`**. The `default` profile is
  a *different* account (536697265987, root) and `kin` belongs to another project.
  Never use either. Policy: `deploy/iam-policy.json`, console steps: `deploy/IAM.md`.
- **Kalshi:** credentials in `~/.kalshi/env` locally and on each box. The user asked
  that this file not be read. Transform it without printing it if you need to.
  `KALSHI_ENV=prod` — this is live production market data, read-only.
- **SSH:** `ec2-user@<ip>` using `~/.ssh/id_ed25519`.

## 4. Where things stand against the exit criteria

Run this, do not guess:

```bash
.venv/bin/python -m ops.dashboard --out capture-dashboard.html   # both sites, one page
ssh ec2-user@<ip> 'cd ~/kalshibot && ./.venv/bin/python -m research.phase1_report --data-root data'
```

| Criterion | Status as of 2026-08-20 |
|---|---|
| 14+ consecutive days | ~0.01 days. The clock just started. |
| <0.1% of session time gapped | 0.000226 on use1 — passing, and improving as startup amortizes. |
| Books reconstructable byte-identically | Mechanism built and tested; run the full replay before declaring it. |
| Clock offset characterized | Collecting. p50 12ms (use1), 29ms (usw2). |

The 14-day span is measured as the **longest contiguous** merged interval with a 600s
tolerance, not the sum of session uptimes. Concurrent shards would otherwise report
112 days after two real days.

## 5. What will break first

**Disk.** Raw capture is ~53 GB/day per site. Gzip achieves 12.2x, which brings 14 days
to ~61 GB inside a 200 GiB volume. That only holds if `kalshi-compress.timer` actually
runs — at raw rate the volume fills in **about four days**. Check
`days_until_full` on the dashboard; if it stays near 4 after the first few hours,
compression is not running.

**Gap rate.** Around 65/hour per site at start. Each gap invalidates one subscription
(20 markets) until its snapshot returns. Currently well inside the gate. Per I2, a
*rising* gap rate is a bug, not weather — investigate rather than tolerate.

**Universe staleness.** Markets are resolved once at startup; daily weather markets
expire. `kalshi-universe.timer` re-resolves at 09:15 UTC and restarts shards staggered.
Each shard is blind ~30–60s during its own restart. That time is absent from the corpus
and is *not* counted as gapped — state it in the phase report rather than letting a
reader assume continuous coverage.

## 6. Corrections to the north star, verified against the live API

These were checked against `docs.kalshi.com/asyncapi.yaml` and the REST reference on
2026-08-19, not recalled. Full detail in `docs/phase_reports/phase_1_build.md`.

1. **WebSocket host differs from REST** — `wss://external-api-ws.kalshi.com/trade-api/ws/v2`.
2. **`seq` is per-subscription, not per-market.** A gap means *some* market in that sid
   missed an update but not which, so every book under the sid must be invalidated. I2
   should be read as plural. Shard size is therefore the blast radius of a gap.
3. **REST orderbook carries no sequence number**, so it cannot be spliced into the delta
   stream. I2's "refetch a REST snapshot" is not implementable as written; use the
   WebSocket `update_subscription` / `get_snapshot` action, which arrives in-band with
   its own `seq`.
4. **Maker fees are keyed off `fee_type`, not `fee_multiplier`.** Only `quadratic`
   waives them. `fee_multiplier` (1, 0.5, 0) scales both legs separately. Measured:
   **130 of 13,306 series charge maker fees** — about 1%, not §2's "essentially all
   liquid markets". `KXBTC15M` and `KXBTCD`, the two highest-volume series on the
   exchange, are maker-fee-**free**. 14 series charge nothing at all. This closes open
   question §10: prefer `GET /series` over `/margin/fee_tiers`, which covers only 16
   perpetual crypto series on a different linear schedule.
5. **`use_yes_price` will flip.** Today's default `false` is what makes
   `edge = 100 - (yes_bid + no_bid)` correct. Kalshi has announced the default will flip
   to `true` and the flag will later be removed. The daemon always sends it explicitly
   and records it in the manifest — a silent flip would reinterpret every recorded
   no-side price and corrupt Phase 2 invisibly. **Re-check this at the start of Phase 2.**
6. **`taker_side` is deprecated** in favour of `taker_outcome_side` / `taker_book_side`.
   Trade direction is the conditioning variable for the adverse selection curve. Raw
   frames are stored verbatim, so all three survive.

## 7. Deliberate deviation from I9

I9 says halting beats guessing. In Phase 1 that is scoped to **market granularity**: an
unparseable frame, unknown message type, or book integrity violation invalidates and
resyncs that market while capture continues. Halting the process would trade the 14-day
continuity criterion for no safety benefit while read-only, and the raw bytes are
already durably logged.

**This scoping is Phase-1-only.** When `exec/` exists, I9 applies at process
granularity as written.

## 8. Next actions, in order

1. **Do nothing for 14 days except watch.** Check the dashboard daily. The single most
   valuable thing is not interrupting the clock.
2. **Verify byte-identical replay** on a real session before trusting the corpus:
   `.venv/bin/python -m research.replay data/<session-id>`. It exits nonzero on any digest
   mismatch. Do this early — if reconstruction is broken, better to know on day 2.
3. **Confirm compression** after ~3 hours (see §5).
4. **At 14 days:** merge both sites into one tree, run `research/phase1_report.py`,
   write `docs/phase_reports/phase_1.md`, then begin Phase 2.
5. **Phase 2 is analysis only.** The adverse selection curve is the single most
   important output. Build the §5 cross-market constraint scanner as a matter of
   course — the data is already there and it needs no forecasting skill.

## 9. Conventions that are easy to violate

- **No new code comments.** Names and structure carry meaning; phase reports carry
  reasoning. Preserve existing comments.
- `Decimal` or scaled ints for every price, size, fee and P&L value. `feed/fixed.py`
  rejects bare numbers deliberately — do not "fix" it to accept floats.
- Every exchange constant lives in `config/exchange.py`.
- Nothing imports from `research/`. `research/` may import anything.
- Commit early and often; descriptive but concise messages.
- Secrets from environment variables only. Never in the repo, never in logs.

## 10. State of the tree

Branch `phase-1-capture`, not yet merged to `main`. 61 tests passing.

```
config/    exchange constants, fee model, market universe spec
auth/      RSA-PSS signing, env-only credentials
feed/      fixed-point parsing, book state, recorder, transport, universe resolution
ops/       capture daemon, CLIs, monitor, preflight, dashboard
research/  replay verification, exit-criteria report, site health
deploy/    IAM policy, provisioning, systemd units, bootstrap
docs/      phase reports and this handoff
```
