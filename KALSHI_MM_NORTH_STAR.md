# Kalshi Market Making Bot — North Star

**Status:** Phase 1 (data capture). Not trading. No capital at risk.
**Last updated:** 2026-08-19

---

## 0. Read this first

This document is the contract for the project. If a change conflicts with this
document, either the change is wrong or this document needs an explicit revision
commit. Do not silently diverge.

**The single most important rule:**

> Phase 2 analysis gates Phase 3+. Do not write an execution engine, an order
> state machine, or any code that can submit an order until the Phase 2 exit
> criteria are met with real recorded data. If Phase 2 says there is no edge,
> the correct outcome is to stop and pivot, not to build anyway and hope.

The failure mode this project is designed to prevent is building a beautiful
trading system for an edge that does not exist. Discovering "no edge" in Phase 2
costs five weeks and zero dollars. Discovering it in Phase 6 costs a semester and
real money.

**Correctness is the top priority throughout.** A market making bot that is 95%
correct is not 95% as profitable — it is unboundedly negative. Every silent state
divergence (stale book, drifted position, missed fill) converts directly into
losses that look like bad luck. Prefer halting to guessing, always.

---

## 1. What this is

A single-operator market making and liquidity-provision bot for Kalshi, a
CFTC-regulated event contract exchange (binary contracts settling at $0 or $1).

### Goals

1. Determine empirically whether a fee-adjusted, adverse-selection-adjusted edge
   exists for a retail-scale operator on Kalshi.
2. If yes, capture it with a system that is correct under partial failure.
3. Produce a defensible understanding of order book microstructure along the way.

### Non-goals

- Low-latency / HFT competition on liquid sports markets. We will lose that.
- Beating professional firms on speed anywhere.
- Directional speculation. If the bot takes a directional view, that view must
  come from an explicit fair value model, never from discretion or drift.
- Scale. Target capacity is small. Do not design for throughput we will not have.

### Success criteria

- **Minimum success:** Phase 2 completes with a clear, evidence-backed answer
  about whether an edge exists. This is a success even if the answer is "no."
- **Full success:** live system runs unattended for 30 days with positive realized
  P&L net of all fees, and live realized spread within 30% of Phase 3 prediction.

---

## 2. Domain facts (verify before relying on any of these)

Exchange rules and fee schedules change. Re-verify against
`https://kalshi.com/docs/kalshi-fee-schedule.pdf` and `https://docs.kalshi.com`
at the start of each phase. Any constant below belongs in `config/exchange.py`,
never inlined in strategy code.

### Contract mechanics

- Binary contracts settle at $0.00 or $1.00. No settlement fee.
- **There are no asks.** The book has YES bids and NO bids only. An ask of 60¢ on
  YES is economically a 40¢ bid on NO.
- Therefore the quotable spread is:
  ```
  edge_cents = 100 - (yes_bid + no_bid)
  ```
- A matched YES+NO pair pays exactly $1.00 at resolution. It self-liquidates with
  no exit fee. This is a real structural advantage over equities market making:
  we never have to pay to get out, we only have to wait.
- Capital is locked until resolution. Kalshi pays interest on idle balance and
  open positions, which partially offsets carry. Model this; do not ignore it.

### Fees (effective 2026-07-07 — verify)

```
taker_fee = round_up(M_taker * 0.07   * C * P * (1 - P))
maker_fee = round_up(M_maker * 0.0175 * C * P * (1 - P))
```

- `P` = price in dollars (50¢ → 0.5), `C` = contract count.
- `M_taker` default 1. `M_maker` **default 0**.
- Rounding is up, such that `fee + position_cost` lands on a centicent (1e-4).
- Critically: `M_maker = 1` on an explicit series list that includes essentially
  all liquid markets — every NFL/NBA/MLB/NHL/NCAA game series, CPI, Fed decisions,
  payrolls, unemployment, major championships and awards.

**Consequence to internalize:** on an `M_maker = 1` market at 50¢, maker round
trip costs ~0.875¢ against a 1¢ minimum tick. Naive two-sided quoting there is
structurally unprofitable before adverse selection is even considered. The
long tail (`M_maker = 0`) is where maker fees vanish. That asymmetry is the
central fact shaping strategy selection.

### Incentive programs

- **Liquidity Incentive Program** — pays for resting two-sided depth regardless
  of fills, scored on per-second book snapshots against a Reference Price with a
  distance Discount Factor. Snapshots lacking two-sided depth at Target Size are
  excluded and scale the payout down. Daily pools ~$10–$1,000.
- **Volume Incentive Program** — pro-rata from a pool, capped at $0.005 per
  contract, on contracts priced $0.03–$0.97.

**Treat all incentive revenue as a terminable subsidy.** Current terms run to
2026-09-01. Report subsidy P&L and trading P&L as separate line items, always.
A strategy that is only profitable with subsidy must be labeled as such in
every report, so we never confuse "we have an edge" with "we are being paid to
show up."

### API facts

- REST prod: `https://external-api.kalshi.com/trade-api/v2`
- REST demo: `https://external-api.demo.kalshi.co/trade-api/v2`
- Auth: RSA-PSS SHA256 over `timestamp + method + path`. Path is signed
  **without** query parameters. Timestamp in **milliseconds**.
- WebSocket path for signing: `/trade-api/ws/v2`.
- Server pings every ~10s; must pong or the connection closes.
- **Fixed-point migration:** prices/counts are fixed-point strings
  (`_dollars`, `_fp` suffixes). Legacy integer fields removed.
- **Order endpoint:** `POST /portfolio/events/orders` (V2). Requires
  `time_in_force` and `self_trade_prevention_type`. Legacy endpoints cost 5x
  rate-limit tokens — never use them.
- **Fills no longer echo `client_order_id`.** Reconcile via `order_id`.
- `tick_size` is removed. Use `price_level_structure` and `price_ranges[].step`.
- Rate limits are token-cost buckets, separate read/write. Fetch
  `GET /account/limits` and `GET /account/endpoint_costs` at startup. Never
  hardcode tier values.
- Matching is **price, then time**. Queue position is real and matters.

---

## 3. Non-negotiable invariants

These are correctness requirements. Every one of them has a test. If any is
violated at runtime, the system halts and cancels rather than continuing.

### I1 — No floats in any price or size path
All prices, sizes, fees, and P&L use `Decimal` or scaled integers, end to end,
including logs and serialization. A float rounding error at 3¢ produces
off-by-one-tick errors that are indistinguishable from edge in a backtest.

### I2 — A book with a sequence gap is dead
Every `orderbook_delta` carries a sequence number. On any gap: mark the book
invalid, stop quoting that market immediately, refetch a REST snapshot, resume
only from the snapshot. Never patch forward across a gap. Log every occurrence
with market and duration; a rising gap rate is a bug, not weather.

### I3 — Reconnect always rebuilds from snapshot
Never resume a local book across a reconnect. Never trust cached state after any
transport interruption.

### I4 — Local position state is assumed wrong until reconciled
Reconcile positions and open orders against the exchange on a timer and after
every reconnect. On any mismatch: halt, cancel all, alert, require manual
restart. Do not auto-correct and continue; a mismatch means an unknown bug.

### I5 — Order submission is idempotent
Use `client_order_id` for submission idempotency. Retries are bounded and must
not double-send. Reconcile via `order_id` since fills do not echo the client id.

### I6 — Limits are enforced pre-submission
Per-market and aggregate inventory limits, and total capital at risk, are checked
before an order is constructed, not after it is acknowledged.

### I7 — Self-trade prevention is always set explicitly
Inventory skew will flip our fair value and cross our own quotes otherwise.

### I8 — Fee-aware everywhere
Any code path computing edge, P&L, or a quote decision uses the real fee formula
with the correct per-series multiplier. There is no "ignore fees for now" mode,
not even in research notebooks.

### I9 — Halting beats guessing
On any ambiguity — stale feed, unavailable model input, unrecognized message,
unexpected state — cancel everything and stop. Silence is not a signal to
continue.

---

## 4. Phases and exit criteria

Do not begin a phase until the previous phase's exit criteria are met and
recorded in `docs/phase_reports/`.

### Phase 1 — Read-only capture (weeks 1–3)

**Build:** a daemon subscribing to `orderbook_delta`, `trade`, `ticker` across
100+ markets spanning weather, culture, econ, and sports. Maintains in-memory
books. Appends every update to a durable log with **both** local receive
timestamp and exchange `ts_ms`.

**Exit criteria:**
- 14+ consecutive days of capture with <0.1% of session time in gapped state.
- Books reconstructable offline from logs, byte-identical to live state on replay.
- Clock offset distribution (local vs `ts_ms`) characterized.

### Phase 2 — Offline analysis (weeks 3–5) — **GO/NO-GO GATE**

**Compute, per market and per series:**
- Time-weighted spread distribution; fraction of time above fee-adjusted breakeven.
- Top-of-book depth and size resting ahead of a joining order.
- Quote lifetime at top of book.
- Trade arrival rate conditional on spread level. *No trades means no revenue
  regardless of how wide the spread looks.*
- **The adverse selection curve.** For every trade in the log, mid at +1s, +10s,
  +60s, and at settlement, conditioned on trade direction. This is the single
  most important output of the project.

**Breakeven model:**
```
net = spread_captured
    - 2 * M_maker * 0.0175 * P * (1 - P)     # fees, both legs
    - adverse_selection_cost(P, market, horizon)
```

**Exit criteria (all must hold to proceed):**
- A named, enumerated set of markets where `net > 0` with margin.
- Estimated weekly contract volume in that set sufficient to matter.
- The result is stable across at least two disjoint time windows in the data.

**If these fail:** stop. Do not build the bot. Write the phase report, then
pivot — the highest-value pivot is cross-market constraint violations (§5).

### Phase 3 — Backtester (weeks 5–7)

**Build:** a simulator with a **queue-aware fill model**. Track simulated queue
position on join; decrement as trades execute at the level and as cancels ahead
occur (inferable from size deltas not accompanied by a trade); fill only when the
queue ahead is exhausted. Apply the Phase 2 adverse selection curve to every
simulated fill.

**Exit criteria:**
- Naive fill model and queue-aware fill model both implemented, results reported
  side by side. Queue-aware fill rate must be a small fraction of naive.
- Strategy remains profitable under the **queue-aware** model, with fees.
- Sensitivity analysis: results survive ±50% error in the adverse selection estimate.

> If a strategy is profitable under naive fills and unprofitable under queue-aware
> fills, the queue-aware answer is the true one. This is where most retail
> backtests lie.

### Phase 4 — Fair value model (weeks 7–9)

**This is the actual product.** Quoting without independent fair value means our
quotes are centered on the market's mid, which means we have no view, which means
we are a pure adverse-selection sponge. The market making machinery is commodity
engineering. The fair value is the edge.

Per-vertical approaches:
- **Weather:** NWS/NOAA ensemble forecasts mapped onto exact resolution criteria.
- **Econ:** nowcasts; term structure implied by related Kalshi contracts.
- **Culture/awards:** aggregators and base rates.
- **Cross-market (highest priority — see §5).**

**Exit criteria:**
- Model produces a calibrated probability with an explicit uncertainty estimate.
- Backtested calibration: predicted probabilities match realized frequencies.
- Model beats "market mid" on log loss out of sample. If it does not, we have no
  view and must not quote.

### Phase 5 — Execution engine, demo only (weeks 9–11)

**Build:** fair value → target quotes (spread widened by inventory skew and by
fair value uncertainty) → diff against resting orders → cancel/replace only what
changed. Never rewrite the whole book each tick; it burns write tokens and
destroys queue position for nothing.

**Exit criteria:**
- Full order state machine with out-of-order ack/fill handling, tested.
- Chaos testing passed: kill the WebSocket mid-fill, kill the process holding
  positions, induce sequence gaps, induce 429s, induce duplicate acks. System
  recovers to a correct reconciled state or halts cleanly, every time.
- 7 days unattended in demo with zero state divergences.

### Phase 6 — Live, deliberately tiny (weeks 11+)

Start at **$200 total exposure.** We are testing the system, not the strategy;
the strategy was tested in Phase 3.

**Exit criteria for any size increase:**
- 14 days live with zero state divergences and zero unexplained P&L.
- **Live realized spread within 30% of Phase 3 prediction.** The gap between
  predicted and realized is the size of our self-deception; closing it is the
  real work. Size increases only after the gap is understood, never merely
  because P&L was positive.

---

## 5. Priority pivot: cross-market constraint violations

If Phase 2 gates fail — the likely outcome — this is the first pivot, and it may
deserve parallel investigation even if Phase 2 passes.

Many Kalshi events are internally inconsistent in ways that are **model-free and
mechanically detectable** from data we already record:

- **Monotonicity:** a ladder of "above X" strikes must be non-increasing in X.
- **Normalization:** mutually exclusive, exhaustive outcome sets must sum to 1
  (net of fees and spread).
- **Complement:** `yes_bid + no_bid <= 100` must hold; violations are free money.
- **Cross-event implication:** if event A implies event B, `P(A) <= P(B)`.

This requires no forecasting skill, no proprietary data feed, and no speed
advantage — only correct bookkeeping. Build a continuous scanner for these
violations in Phase 2 as a matter of course, since the data is already there.

---

## 6. Repo layout

```
kalshi-mm/
  config/
    exchange.py         # fees, multipliers, endpoints, limits. Single source of truth.
    markets.yaml        # market universe per phase
  auth/                 # RSA-PSS signing, key loading from env
  feed/
    ws_client.py        # reconnect, heartbeat, auth handshake
    book.py             # book state, sequence tracking, gap detection
    recorder.py         # durable append-only log
  research/             # Phase 2 analysis; notebooks allowed here only
  backtest/
    fill_model.py       # queue-aware + naive, both, always
    engine.py
  model/                # fair value per vertical
  exec/
    order_state.py      # state machine
    quoter.py           # fair value -> target quotes -> diff
    reconciler.py
    killswitch.py
  ops/
    monitor.py
    alerts.py
  tests/
  docs/phase_reports/
```

**Layering rule:** `exec/` may import from `model/` and `feed/`. Nothing imports
from `research/`. `research/` may import anything. This keeps exploratory code
from ever reaching the trading path.

---

## 7. Kill switches

All of these cancel every resting order and halt quoting. Restart is manual.

| Trigger | Threshold |
|---|---|
| Stale feed | no message in N seconds |
| Sequence gap unrecovered | > N seconds |
| Fair value input stale/unavailable | any |
| Realized P&L below daily floor | configured per phase |
| Position mismatch after reconciliation | any |
| Inventory outside limit | any |
| Approaching settlement | configured per series |
| Rate limit 429 sustained | > N consecutive |
| Unrecognized message schema | any |

The kill switch path must be the simplest, most-tested code in the repo. It has
to work when everything else is broken.

---

## 8. Conventions

- **Python** throughout. Do not port anything to C until Phase 6 demonstrates a
  live positive edge that is demonstrably latency-constrained. Latency is not the
  binding constraint on long-tail markets; a fast bot with no fair value model
  just loses money more efficiently.
- **No new code comments.** Preserve any comments that already exist. Names and
  structure carry the meaning; the phase reports carry the reasoning.
- `Decimal` or scaled ints for all monetary and size values (see I1).
- Every exchange constant lives in `config/exchange.py`.
- Structured logging (JSON lines) for anything that will be analyzed later.
- Secrets from environment variables only. Never in the repo, never in logs.
- Every invariant in §3 has a corresponding test.

---

## 9. Anti-patterns

Things that will produce a confident, wrong answer:

- Assuming a fill whenever the market trades at your price. **This is the #1 way
  backtests lie.**
- Ignoring fees "for now" in research.
- Using the market mid as fair value, then quoting around it, then believing the
  backtest.
- Patching a book forward across a sequence gap.
- Auto-correcting a position mismatch instead of halting.
- Reporting subsidy revenue and trading revenue as one number.
- Optimizing latency before establishing edge.
- Increasing size because P&L was positive, rather than because the
  prediction/realization gap was understood.
- Treating a good week as evidence. Market making P&L is fat-tailed on the
  downside; four good months can be erased by one settlement.

---

## 10. Open questions

Track answers here as they are resolved.

- [ ] Does the Liquidity Incentive Program extend past 2026-09-01?
- [ ] Exact current `M_maker` series list — pull programmatically, do not transcribe.
- [ ] Per-market position limits by series.
- [ ] `GET /margin/fee_tiers` returns per-ticker maker/taker rates — prefer this
      over the PDF as the runtime source of truth?
- [ ] Tax treatment of event contract P&L; 1099 handling. Consult a professional
      before any scale.
- [ ] Which markets have sub-cent ticks via `price_ranges[].step`?
