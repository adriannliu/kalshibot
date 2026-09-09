# Phase 2 — Offline analysis: GO/NO-GO report

**Verdict: GO, conditional on Phase 3.**
**Date:** 2026-09-09
**Corpus:** usw2, 52 sessions, 2026-08-24 → 2026-09-05
**Capital at risk:** none. No order-submission code exists in this repo.

---

## 1. Gate result

| Exit criterion | Required | Measured | |
|---|---|---|---|
| A named set of markets where `net > 0` with margin | non-empty | **206 markets, 49 series** | PASS |
| Weekly contract volume in that set sufficient to matter | > $100 | **$77,970/week** | PASS |
| Result stable across two disjoint time windows | Jaccard ≥ 0.50 | **0.806 (series)** | PASS |

`markets_analyzed: 4,479` distinct tickers over `67,243` quotable market-hours.
Ten sessions from the frozen-universe period were excluded (`--since
2026-08-24T00:00:00Z`); see `phase_1.md` §5.

**This is a GO to build, not a GO to trade.** §2's gate asks whether an edge
appears in the data. It does. Whether it can be *captured* is Phase 3's question,
and the answer below is deliberately not assumed.

## 2. The adverse selection curve

The single most important output of the project. For every trade, the mid at
+1s, +10s, +60s and at settlement, conditioned on taker direction.

Maker-fee-free markets, 2,509,237 measured trades:

| Horizon | Net maker P&L | Lower 2SE bound | p5 | p95 |
|---|---|---|---|---|
| 1s | 0.790¢ | 0.786¢ | −2.00¢ | +4.00¢ |
| 10s | 0.674¢ | 0.669¢ | −4.00¢ | +5.50¢ |
| 60s | 0.610¢ | 0.602¢ | −8.50¢ | +9.50¢ |
| settlement | 1.221¢ | 1.122¢ | −26.50¢ | +31.50¢ |

The monotonic decay from 0.790¢ to 0.610¢ across the first minute **is** adverse
selection: information in the trade arrives in the mid over time and erodes the
maker's apparent capture. It is the shape theory predicts, measured directly.

By fee regime at the 10s horizon:

| Regime | Trades | Net | Lower 2SE |
|---|---|---|---|
| maker-free | 2,509,237 | **0.674¢** | 0.669¢ |
| maker-charged | 3,247,078 | **0.270¢** | 0.267¢ |
| zero-fee | 30,040 | 0.203¢ | 0.199¢ |

The 0.40¢ separation between maker-free and maker-charged is the 0.875¢ round trip
appearing exactly where §2's fee model says it should. This is the payoff from
stratifying the universe by fee regime rather than by volume: ranking by volume
would have sampled almost entirely from the middle row and understated the edge by
more than half.

**Judge on the lower bound, never the mean.** With 2.5M observations the bounds
are tight, but the dispersion is not: ±2/4¢ at one second becomes ±26/31¢ at
settlement. Sub-penny averages are being collected in front of a thirty-cent
distribution, which is §9's fat-tailed downside stated numerically.

## 3. The governing caveat

**Every number in §2 assumes we were the maker on every trade that occurred.**

We would not be. With thousands of contracts resting ahead under price-time
priority, we would fill a small fraction — and disproportionately the adverse
ones, because fills occur when size sweeps a level, which correlates with
informed flow. The rare fills are selected *for* being the bad ones.

§9 names this the number one way backtests lie. Phase 3 exists to correct it, and
the north star requires the naive and queue-aware models be reported side by side
with the queue-aware answer treated as the true one.

**No sizing, quoting, or capital decision should cite §2 without §3's correction.**

## 4. Cross-market constraint violations (§5)

Scanned continuously across the corpus. The result inverts the obvious reading.

| Kind | Occurrences | Net-positive | Median duration | Mean net/contract | Lower 2SE |
|---|---|---|---|---|---|
| complement | **0** | 0 | — | — | — |
| normalization (yes) | 6,922 | 843 | 0.03s | **−0.0140** | −0.0144 |
| normalization (no) | 6,262 | 802 | 0.17s | **−0.0173** | −0.0177 |
| implication | 249 | 116 | **1.33s** | **+0.0117** | **+0.0056** |

**Complement violations do not occur.** Zero across the entire corpus, confirming
an earlier spot check over 2.7M book updates. `yes_bid + no_bid > 100` is not a
thing that happens on this exchange. The risk-free version of this trade does not
exist.

**Normalization violations are abundant and unprofitable.** Thirteen thousand of
them, and net-*negative* on average after fees. Median lifetime 30–170ms: these
are sub-tick artifacts of asynchronous quote updates, not mispricings. Count is
not opportunity. A scanner that ranked by frequency would have chased these.

**Implication violations are the real finding.** Fewer — 249, roughly 20/day, 116
net-positive — but they persist: median 1.33s, p75 10.6s, p95 168s, p99 944s. The
largest, between two spread markets on the same NFL game
(`KXNFLSPREAD-26AUG29DETIND`), stood for **999 seconds** at 39¢/contract gross
with 598 contracts available.

Sixteen minutes requires no speed advantage. Mean net is positive with the lower
2SE bound above zero. Upper bound on extractable value is $1,316 across the
12-day window — modest, but it requires **no fair value model at all**, only
correct bookkeeping, and it is therefore available immediately.

## 5. A correction on the record

**The gate initially reported FAIL.** The stability criterion scored a Jaccard of
0.125 against a 0.50 threshold and the run was recorded as NO-GO.

That was a methodology error introduced on 2026-09-04. Interleaved windows were
adopted to control weekday seasonality, which they do — but roughly 80% of this
universe expires within days (weather, daily crypto, per-game sports), so
interleaving by calendar day gives the two windows **almost disjoint ticker
populations by construction**. The test was measuring market turnover, not edge
persistence.

Measured at the level that persists, the same two windows score **0.806**: 54 of
67 series qualify in both. `KXHIGHNY-26AUG24-B89` and `KXHIGHNY-26AUG25-B91` are
different tickers and the same economic market, and the series is the unit one
would actually decide to quote.

Both figures are now reported. Market-level remains visible as a churn
diagnostic; only series-level gates. This is recorded because a criterion that
flips from FAIL to PASS after the test is changed deserves scrutiny, and the
reasoning should be auditable rather than buried in a commit message.

## 6. What Phase 3 must establish

1. **Queue-aware fill rate**, as a fraction of naive. Expected to be small.
2. **Whether 0.674¢ survives** once fills are selected by queue exhaustion rather
   than assumed. This is the whole question.
3. **Sensitivity to ±50%** error in the adverse selection estimate.
4. Both fill models reported side by side, with the queue-aware answer taken as
   true where they disagree.

If the queue-aware model destroys the edge, the correct outcome is still to stop
and pivot — to the implication scanner in §4, which does not depend on this
answer.

## 7. Reproducing

```bash
.venv/bin/python -m research.phase2_report \
    --data-root <corpus> --jobs 3 \
    --since 2026-08-24T00:00:00Z --window-mode interleaved
```

Run on a host that is not capturing, with **at least 8GB of RAM** — the analysis
holds all session results concurrently and was OOM-killed twice on a 2GB capture
box before being moved to a 30GB instance.
