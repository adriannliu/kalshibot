# Handoff — Kalshi market making bot

**Written:** 2026-09-10
**Phase:** 1 complete, 2 complete (GO), 3 in progress
**Capital at risk:** none. No order-submission code exists in this repo.

Read [`KALSHI_MM_NORTH_STAR.md`](../KALSHI_MM_NORTH_STAR.md) first — it is the
contract. Phase reports in `docs/phase_reports/` carry the reasoning.

---

## 1. The rule that still governs

Phase 2 passed, so building is permitted. **Trading is not.** No execution engine,
no order state machine, no order-submission path until Phase 3's exit criteria are
met — and they are **not** met (§4 below). `tests/test_layering.py` fails the build
if such code appears; do not weaken it.

Phases 5 and 6 remain strictly serial and calendar-bound: chaos testing, 7 days
demo, 14 days live. Not compressible (`2a261f7`).

## 2. Infrastructure

| Host | Role | State |
|---|---|---|
| `usw2` us-west-2 `i-046439a0331c7344c` | capture | **running**, do not disturb |
| `use1` us-east-1 `i-0f55212c56977dfe7` | retired from capture | idle, holds a corpus copy |
| `analysis` us-east-1 `i-0f6cf8a768f2c4ca1` | r7g.xlarge, 4 vCPU / 30GB | analysis box |

**Never run analysis on a capture host.** Doing so caused a 21-minute outage that
permanently split use1's span. The analysis box exists for this.

IPs are not elastic. Discover by tag, and run `./deploy/allow-my-ip.sh` when SSH
times out — the client address changes often and the script also revokes stale
entries.

## 3. Phase 1 and 2 results

**Phase 1 (`phase_1.md`): complete.** 16.31 consecutive days, 0.018% gapped
against a 0.1% gate, **62/62 sessions replaying byte-identically** across
414,546,695 records with zero digest mismatches, clock offset characterized from
409M samples. usw2 carries the duration criterion; use1 fails it due to operator
error.

**Phase 2 (`phase_2.md`): GO, conditional.** 206 markets / 49 series clear net > 0,
$77,970/week projected, series-level stability 0.806.

Adverse selection, maker-free, 2.5M trades: **0.790¢ at 1s → 0.674¢ at 10s →
0.610¢ at 60s.** That decay is adverse selection measured directly. Maker-charged
nets 0.270¢ after fees — the 0.875¢ round trip appearing where §2 predicts.

§5 constraint violations invert the obvious reading: **complement violations do not
occur at all** (zero across the corpus); normalization violations are abundant
(13,000) and **net-negative**; **implication violations are the only tractable
finding** — 249 of them, median 1.33s, p95 168s, net-positive with the lower bound
above zero. The best stood 999 seconds at 39¢/contract.

## 4. Phase 3 — where it actually stands

Built and committed: `backtest/fill_model.py` (naive + queue-aware),
`backtest/engine.py`, `backtest/cli.py`. 179 tests pass.

A full 62-session run completed (82 min, zero failures) reporting queue-aware
**+1.195¢/contract** at the measured adverse selection, positive across ±50%.
Instrumentation was clean: 0 unscoreable fills, 1 forced mark in 123,000.

**That result is not accepted, and Phase 3 does not pass.** Two defects:

**A. The queue is not binding.** Queue-aware produced 58,125 fills against naive's
64,866 — 90%. §4 requires the queue-aware rate be "a small fraction of naive." The
likely cause is that `min_edge = 2¢` selects wide-spread markets, which are thin,
which have shallow queues. Phase 2 measured 3,764 contracts resting in tight
markets and 22 in wide ones. The strategy is avoiding the constraint rather than
surviving it.

**B. The exit is not modelled.** Each fill is marked to the mid ten seconds later
and forgotten. Nothing is ever closed. §2's real exit — matched YES+NO pairs
self-liquidating at $1.00 — is absent, so `net_dollars` is a mark, not money.

### The intended fix (designed, not implemented)

**Exit:** score against actual settlement, which we have for 4,648 of 5,116
tickers (`settlements.json`, 90.9% coverage). The formula is exact:

```
revenue = yes_contracts × S + no_contracts × (1 − S),    S ∈ {0, 1}
realized = revenue − yes_cost − no_cost − fees
```

Matched pairs give `n` regardless of S — self-liquidating, as §2 describes.
Unmatched legs ride to settlement. Markets without a settlement must be **reported
as unscored, never silently dropped**.

**Depth:** record top-of-book size at fill time, bucket markets by it, report net
per bucket for both models. If the edge exists only where queues are shallow, that
is a far narrower claim than "market making works" and is the one to test.

## 5. Access

- **AWS:** account `073158194660`, profile `kalshibot`. `default` is a *different*
  account (root); `kin` is another project. Never use either.
- **Kalshi:** `~/.kalshi/env`, `KALSHI_ENV=prod`, read-only. The user asked that
  this file not be read — transform it without printing it if needed.

## 6. Failure modes that have actually bitten, in order of cost

1. **A guard around a lazy generator guards nothing.** `paginate` yields lazily, so
   the request fired outside the `try`. Hid a broken `/events` call for three days,
   froze the universe, and cascaded into spurious reconnects. *Three symptoms, one
   cause.*
2. **Process-exists is not process-working.** `pgrep` reported a job "running" for
   15 hours after the parent was OOM-killed and only orphaned workers remained.
   Check advancing CPU time or output size.
3. **Silencing output hides the failure you need.** `2>/dev/null` on rsync hid a
   failed sync; piping pytest through `tail` masked its exit status and let a
   failing tree get committed. **Check `$?` explicitly before committing.**
4. **Partial deploys of an interdependent tree.** Syncing `ops/` without `feed/`
   crash-looped a shard. Deploy the whole tree.
5. **Metrics that improve when things break.** Several found and fixed in
   `90523e6`: a counter that could never fire, unreadable sessions dropping out of
   both sides of a ratio, unscoreable fills vanishing entirely.
6. **Long jobs with no progress output.** A ten-hour run printed nothing and was
   doing 9x redundant work. Instrument anything over a few minutes.

## 7. Conventions easy to violate

- **No new code comments.** Names and structure carry meaning; phase reports carry
  reasoning.
- `Decimal` or scaled ints for every price, size, fee, P&L. `feed/fixed.py` rejects
  bare numbers deliberately.
- Every exchange constant in `config/exchange.py`.
- **Nothing imports from `research/`.** Tape reading lives in `feed/tape.py` for
  exactly this reason; `research/session.py` is a re-export shim.
- Commit early and often; check the test exit status first.
- Secrets from environment only.

## 8. State of the tree

Branch `phase-1-capture`, not merged to `main`. 179 tests passing, clean.
Another agent works in `research/` — coordinate before editing their modules.
