# Phase 1 — Read-only capture: exit report

**Status:** **COMPLETE.** All four exit criteria met.
**Capture window:** 2026-08-20 19:34 UTC → 2026-09-05 (continuing)
**Capital at risk:** none. No order-submission code exists in this repo.
**Date:** 2026-09-07

---

## 1. Verdict

| Exit criterion | Required | Measured | |
|---|---|---|---|
| Consecutive days of capture | ≥ 14 | **16.31** | MET |
| Session time in gapped state | < 0.1% | **0.018%** | PASS |
| Books reconstructable byte-identically | all | **62 / 62 sessions** | MET |
| Clock offset distribution characterized | — | **409,922,354 samples** | MET |

Phase 2 is now open as a GO/NO-GO gate.

## 2. What was captured

| | |
|---|---|
| Markets | 499, across 78 series |
| Fee regimes | 400 maker-free / 72 maker-charged / 20 zero-fee |
| Sites | `use1` (us-east-1) and `usw2` (us-west-2), independently capturing the same universe |
| Shards | 4 per site, partitioned by series |
| Records | 414,546,695 (usw2, verified corpus) |
| Distinct tickers over the run | 5,116 — the universe re-resolves daily as markets expire |
| Settled outcomes collected | 4,648 of 5,116 (**90.9%**); 468 still open |
| Compressed size | ~16 GB |

Fee-regime stratification was deliberate. Ranking by volume alone selects almost
entirely maker-charged series, which §2 predicts cannot work; that would have
measured only the half of the exchange that fails and concluded no edge exists.

## 3. Byte-identical reconstruction

Every WebSocket frame is stored verbatim, so the log is lossless independent of
parser correctness. Every `digest_interval` the daemon writes a SHA-256 over each
book's canonical state plus an aggregate over all books. `research/replay.py`
re-runs the identical book code offline and compares at every digest.

```
sessions verified      : 62 / 62
records replayed       : 414,546,695
digests checked        : 18,842
digest mismatches      : 0
aggregate mismatches   : 0
integrity errors       : 0
truncated segments     : 0
sequence gaps live     : 2,739
sequence gaps replayed : 2,739
```

The gap counts agreeing exactly is an independent confirmation: replay detected
every sequence discontinuity the live daemon saw, from the same bytes, without
being told where they were.

## 4. Clock offset

From 409,922,354 `recv_ms - ts_ms` samples, per-session ranges:

| | usw2 | use1 |
|---|---|---|
| p50 | 28–35 ms | ~11 ms |
| p95 | 32–258 ms | ~14 ms |
| p99 | 573–925 ms | ~580 ms |
| max | 40,668 ms | 1,397 ms |

The p50 difference between sites tracks geography and is the expected result.
The **40.7 second maximum** is not: it is a single stall, and it matters because
any Phase 4 model that treats `ts_ms` as trustworthy without bounds will be wrong
about when it knew something. Use the p99, never the max, and treat samples above
~1s as unusable rather than merely late.

Both hosts run chrony against the Amazon Time Sync Service; without disciplined
clocks these figures would measure our own drift and be worthless.

## 5. Incidents

Three, all operator-caused, all recorded here rather than smoothed over.

**Frozen universe, 2026-08-21 09:16 → 08-24 17:26 (3.3 days).** `GET /events` caps
`limit` at 200 while `GET /markets` allows 1000; the shared paginator hardcoded
1000, so every event lookup returned 400. The daily refresh aborted before its
shard-restart step, leaving the market list frozen while its markets settled.
Trade activity fell to 30–46% of normal. The failure was invisible because
`fetch_event_index` wrapped a *generator* in `try/except` — `paginate` yields
lazily, so the request fired outside the guarded block.

**Consequence for Phase 2:** this window is excluded from analysis via
`--since 2026-08-24T00:00:00Z`. The data is valid and replays byte-identically;
it is simply not representative of a market we would quote into.

**Shard-0 crash loop, 08-22 (~15 min).** A partial deploy: `ops/` was synced
without `feed/`, and the daemon read a field that only existed in the newer
`feed/universe.py`. Partial deploys of an interdependent tree are more dangerous
than full ones, not safer.

**use1 reboot, 09-01 (21.2 min).** Rebooting to clear a runaway analysis job I had
started on a capture host. This exceeded the 600s contiguity tolerance and split
use1's span permanently:

```
use1   08-20 19:30 → 09-01 02:34   11.29 days
       09-01 02:55 → present        (restarted)
usw2   08-20 19:34 → present       16.31 days   ← unbroken
```

**use1 therefore fails the duration criterion and usw2 carries it.** That is the
two-region design working as intended — the redundancy insured against operator
error rather than against AWS. use1's data remains valid and replay-verified.

The reboot also exposed a latent fault: `kalshi-capture.target` was never enabled
on either host, so the shard units — `WantedBy` that target — did not start at
boot. Capture came back down with every unit still reporting "enabled". It would
have failed identically on any kernel-update reboot, on both hosts.

## 6. Corrections to the north star

Recorded in full in `phase_1_build.md` §3; revised in the north star itself with
dated notes. The consequential ones:

1. **`seq` is per-subscription, not per-market.** A gap means some market in that
   `sid` missed an update but not which, so every book under it must be
   invalidated. Shard size is the blast radius of a gap.
2. **The REST orderbook carries no sequence number**, so it cannot be spliced into
   the delta stream. I2's "refetch a REST snapshot" is not implementable as
   written; the WebSocket `get_snapshot` action arrives in-band with its own `seq`.
3. **Maker fees key off `fee_type`, not `fee_multiplier`.** 130 of 13,306 series
   charge maker fees — about 1%, not §2's "essentially all liquid markets". The
   two highest-volume series on the exchange, `KXBTC15M` and `KXBTCD`, are
   maker-fee-**free**.
4. **Incentive programs run to 2027-01-01 and 2027-09-01**, not 2026-09-01.

## 7. Deliberate deviation from I9

I9 says halting beats guessing. In Phase 1 that was scoped to **market
granularity**: an unparseable frame, unknown message type, or book integrity
violation invalidates and resyncs that market while capture continues. Halting the
process would have traded the 14-day continuity criterion for no safety benefit
while read-only, and the raw bytes are durably logged regardless.

**This scoping ends with Phase 1.** When `exec/` exists, I9 applies at process
granularity as written.

## 8. What Phase 2 inherits

- 62 verified sessions, filtered to post-2026-08-24 for edge estimation
- 4,648 settled outcomes, populating the settlement horizon of the adverse
  selection curve — the one §2 calls out specifically
- Fee regime, `fee_type`, and both multipliers recorded per market per session, so
  the fee model never has to be reconstructed
- Stability windows **interleaved by calendar day**, not split chronologically: two
  contiguous halves of a nine-day corpus differ by weekday composition, and Phase 2
  would read that seasonality as an unstable edge

## 9. Reproducing this

```bash
.venv/bin/python -m research.replay <session-dir>
.venv/bin/python -m research.phase1_report --data-root data
.venv/bin/python -m research.settlements --data-root data --out data/settlements.json
.venv/bin/python -m ops.dashboard --out capture-dashboard.html
```

Run analysis on a host that is not capturing. Two of the three incidents above
trace to ignoring that.
