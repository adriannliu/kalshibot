# Phase 2 — Offline analysis: build report

**Status:** analysis pipeline built and tested. **Zero real data analysed.** The
Phase 2 gate cannot be evaluated until Phase 1 delivers 14 days of capture.

**Date:** 2026-08-20
**Trading code written:** none. `tests/test_layering.py` still fails the build if any
module in the trading path gains an order-submission call site.

This report describes the machinery and the measurement decisions inside it. It does
**not** contain a go/no-go answer, and nothing here should be read as one.

---

## 1. Why build this now

Phase 1 is running and its binding exit criterion is 14 consecutive days of capture.
The handoff's advice — "do nothing for 14 days except watch" — is right about the
*capture*, and this work does not touch it. Building the analysis while the clock runs
costs nothing and removes the risk of discovering, on day 14, that the corpus cannot
answer the question. §8.5 of `docs/HANDOFF.md` names exactly this work as next.

Everything added lives in `research/`, which nothing imports.

## 2. What was built

| Component | Path | Role |
|---|---|---|
| Analysis event stream | `research/session.py` | Typed events (book changes, trades, invalidations) rebuilt from a session log, with per-market fee metadata. |
| Weighted statistics | `research/stats.py` | Time-weighted histograms, duration histograms, signed summaries with standard errors. |
| Microstructure | `research/microstructure.py` | Spread distribution, breakeven time, depth, quote lifetime, trade arrival vs. spread. |
| Adverse selection | `research/adverse_selection.py` | The curve: mid at +1s/+10s/+60s/session end/settlement, signed against the maker. |
| Constraint scanner | `research/constraints.py` | The §5 pivot: complement, implication, normalization violations as executable arbitrage. |
| Gate report | `research/phase2_report.py` | Single-pass driver, breakeven verdicts, the three exit criteria, CLI. |

## 3. Measurement decisions that change the answer

### 3.1 Gapped time is excluded, never interpolated

Every time-weighted quantity accrues only over intervals where the book is **valid and
two-sided**. A sequence gap ends the accrual; it does not extend the last known state
forward. Three consequences, all deliberate:

- A quote lifetime interrupted by a gap is **censored**, not recorded. Recording it
  would report the gap duration as quote persistence and inflate the single number
  that most directly determines whether joining the queue is worth it.
- An adverse selection horizon whose deadline lands while the book is invalid is
  recorded as `unresolved_book_invalid`, not sampled from stale state. The count is
  reported alongside the curve so a reader can see how much of the sample was lost.
- Trades arriving with no valid book are counted in `trades_without_book` and excluded
  from every conditional rate.

This is I2 and I9 applied to analysis rather than to quoting: an unmeasurable interval
is reported as unmeasured.

### 3.2 The adverse selection curve is decomposed, not double-counted

§4's breakeven model is

```
net = spread_captured - 2 * M_maker * 0.0175 * P * (1-P) - adverse_selection_cost
```

The obvious implementation measures `spread_captured` from the book and
`adverse_selection_cost` from the mid move, then subtracts. That double-counts,
because a maker who was filled at the bid has already captured half the spread at the
moment the mid is measured.

Two quantities are therefore reported per horizon:

- **`mid_move`** — signed mid change after the trade, positive when adverse to the
  maker who was filled. This is `adverse_selection_cost` as §4 defines it, and it is
  the quantity Phase 3 feeds into the fill model.
- **`net_maker_pnl`** — the maker's actual P&L per contract, measured from the fill
  price to the mid at the horizon, minus the maker fee on that leg at that fill price.

They satisfy `maker_pnl = spread_captured_at_fill - mid_move`, so reporting both makes
the decomposition auditable. The gate uses `net_maker_pnl`, because it is measured
rather than constructed.

Direction comes from `taker_outcome_side` with a fallback to the deprecated
`taker_side`. A trade with no usable direction is never signed — it is counted in
`trades_without_direction` and dropped. Guessing a direction would put a coin flip into
the most important output of the project.

### 3.3 Fees are inside the measurement, not applied afterwards

Per I8 there is no fee-free path. The maker fee is computed per trade at that trade's
own fill price with that series' own multiplier, inside the accumulator. Applying a
mean fee to a mean P&L afterwards would be wrong wherever the fill price distribution
is skewed, which is everywhere near 0¢ and 100¢.

The maker's leg price is `1 - yes_price` when the taker bought YES and `yes_price` when
the taker bought NO, but `P(1-P)` is symmetric, so one expression covers both.

**One correction to §2's framing.** §2 says naive two-sided quoting on an
`M_maker = 1` market is "structurally unprofitable" at 50¢ because the round trip costs
~0.875¢ against a 1¢ tick. The cost figure is right; "unprofitable" is not. A 1¢ edge
at 50¢ clears maker fees at every price — `0.07/4` never exceeds `0.01` — leaving about
0.125¢. The real claim is that maker fees consume **87.5% of the minimum tick**, so
almost nothing is left to pay for adverse selection. That is a much stronger statement
about the *margin* and a weaker one about the *sign*, and the tests assert the actual
numbers (`tests/test_microstructure.py`) rather than the slogan.

`fee_multiplier` of 0.5 is handled — `KXMLBGAME` keeps 56% of a 1¢ edge, not 12%.

### 3.4 Constraint violations are arbitrage packages, not mid comparisons

§5 lists four consistency checks. Implemented as mid-price comparisons they would
produce a stream of violations that cannot be traded. Each is therefore expressed as a
package of taker orders whose minimum payout is at least its cost:

| Check | Condition | Package |
|---|---|---|
| Complement | `yes_bid + no_bid > 1` | Buy YES and NO in one market; pays exactly $1. |
| Implication | `yes_bid(A) + no_bid(B) > 1` where A ⇒ B | Buy YES(B), buy NO(A); pays at least $1. |
| Normalization (yes) | `Σ no_bid > n - 1` | Buy every YES leg; pays exactly $1. |
| Normalization (no) | `Σ yes_bid > 1` | Buy every NO leg; pays exactly $(n-1). |

Implication pairs are derived from strike ladders. Each ladder is normalized to a chain
ordered by **decreasing probability** — `greater`-type strikes ascending, `less`-type
strikes descending — after which one rule covers both. For each rung the best
counterparty is found by a suffix maximum over YES bids, so non-adjacent violations are
caught in O(k) rather than O(k²) per book update.

Three properties worth stating because they are what make the output trustworthy:

- **Every opportunity is sized and fee-netted.** Size is the executable minimum across
  legs; fees are real taker fees per leg via `config/exchange.py`. Taker fees are
  large — 1.75¢ per contract per leg at 50¢ — so a 5¢ crossed book nets about 1.5¢, and
  a 4¢ normalization gap on three legs is a **loss**. Gross violations and the subset
  surviving fees are counted separately, and `tests/test_constraints.py` pins a case
  where fees erase the edge entirely.
- **Normalization is skipped unless the whole exclusive set was captured.** A partial
  set is not exhaustive and the sum means nothing. `event_market_count` from the
  exchange is compared against what we hold.
- **Duration is measured.** An arbitrage that exists for 3ms is not the one that exists
  for 30s. Each occurrence carries its duration, and occurrences truncated by a gap are
  flagged rather than counted as if they ended.

`net_dollars_upper_bound` sums the peak of each occurrence. It is an **upper bound on
notional opportunity, not a P&L forecast** — it ignores queue position, latency, and
the fact that the same resting order can back only one fill.

### 3.5 Sites are never merged

Both capture sites record the **same universe** by design. Merging them would
double-count every trade and every second of book time, roughly halving apparent
adverse selection per unit time and doubling apparent volume. Each site is evaluated
independently; the primary is the one with the most quotable hours, and cross-site
agreement on the qualifying set is reported as a separate consistency check.

Within a site, merging is correct: shards partition markets and restarts partition
time, so no interval is counted twice.

### 3.6 "Positive with margin" is stated statistically

§4 requires markets where `net > 0` **with margin**. A market qualifies only when all
of these hold:

- at least 24 hours of quotable time,
- at least 200 measured fills,
- mean `net_maker_pnl` above 0.1¢ per contract,
- and mean minus two standard errors still above zero.

The last is what "with margin" means operationally: a market does not qualify because
its point estimate happened to land above zero. Every failing market is reported with
its `blockers`, so a near miss is visible rather than silently absent.

The volume criterion needs an assumption we cannot measure — what share of a market's
volume we could actually capture. It is a CLI parameter (`--capture-share`, default 5%)
and its value is printed in the output. It is an assumption, and it is labelled as one.

## 4. Metadata added to capture

The scanner needs data Phase 1 did not record. `feed/universe.py` now carries
`event_ticker`, `strike_type`, `floor_strike`, `cap_strike`, `mutually_exclusive` and
`event_market_count` into the session manifest, sourced from `GET /markets` and a new
`GET /events` call per series.

- Running shards **do not have this yet.** They pick it up at the next universe refresh
  (`kalshi-universe.timer`, 09:15 UTC). Sessions captured before then support the
  complement check only; the other three degrade to "not evaluated", never to a wrong
  answer.
- The `GET /events` parameters have **not** been verified against the live API from
  this machine. The call is wrapped so that any failure yields empty metadata and
  capture proceeds unaffected — but confirm it returns `mutually_exclusive` before
  trusting a normalization result. This is the first thing to check after the next
  refresh.
- Cost is one extra read per series (~78 calls) once per day, at resolution time only.

## 5. Throughput: the corpus is large enough to matter

Measured on a synthetic log with realistic book depth: **12–17k events/second** for a
single pass driving all three analyses, with the stream itself the dominant cost.

At the observed capture rate (~53 GB/day/site, per the handoff), 14 days across two
sites is on the order of two billion records — days of single-threaded work. Sessions
are independent, so `research/phase2_report.py` fans out over a process pool
(`--jobs`, default cores−1), measured at 3.3× on four workers, and a test asserts the
parallel report is byte-identical to the serial one.

This is worth knowing now: if the analysis loop takes a day per iteration, Phase 2
becomes a batch job rather than an exploration, and that changes how it should be run.
If it needs to be faster, the profile points at JSON decoding and `Decimal` comparison
in `best_yes_bid`, in that order. Both are addressable without touching `feed/book.py`,
which should stay simple because it is on the trading path.

## 6. What is not built

- **Settlement outcomes.** The curve supports a settlement horizon and is tested, but
  nothing fetches settled results yet. Until it does, the terminal sample is
  `session_end` — the last observed mid — which is a weak proxy, and is labelled as a
  different horizon rather than passed off as settlement.
- **Cross-event implications.** Only implications derivable from strike ladders inside
  one event are generated. "Event A implies event B" across events needs a curated map.
- **Interest on idle balance and open positions.** Still not modelled. §2 requires it
  before any Phase 3 P&L number is believed.
- **Queue-aware fills.** Phase 3, and deliberately absent. Every net figure here
  assumes the fill happened. Whether we would have been at the front of the queue is
  precisely the question Phase 3 exists to answer, and §9 names assuming otherwise as
  the number one way backtests lie.

## 7. Running it

```
.venv/bin/python -m research.phase2_report --data-root data --jobs 8
.venv/bin/python -m research.phase2_report --data-root data --site use1 --capture-share 0.02
```

Exit 0 means all three Phase 2 exit criteria passed. Exit 1 means they did not — which
per §4 is a legitimate outcome and a reason to write the report and pivot, not a reason
to loosen the thresholds.
