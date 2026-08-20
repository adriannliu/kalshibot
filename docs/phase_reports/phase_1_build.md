# Phase 1 — Read-only capture: build report

**Status:** capture system built and verified end-to-end against a scripted server.
Zero days of real capture recorded. Exit criteria **not yet met** — they cannot be
met until the daemon has run against production for 14+ days.

**Date:** 2026-08-19
**Trading code written:** none. `tests/test_layering.py` fails the build if any
module in the trading path gains an order-submission call site.

---

## 1. What was built

| Component | Path | Role |
|---|---|---|
| Exchange constants and fee math | `config/exchange.py` | Single source of truth. No constant is inlined elsewhere. |
| Market universe spec | `config/markets.yaml` | Selection *rules*, not tickers — daily markets expire. |
| Universe resolver | `feed/universe.py` | Expands rules into concrete tickers via `GET /series` + `GET /markets`. |
| RSA-PSS signing | `auth/signer.py`, `auth/credentials.py` | Millisecond timestamps, query-stripped paths, secrets from env only. |
| Fixed-point parsing | `feed/fixed.py` | Prices to `Decimal`, counts to scaled ints. Rejects bare numbers. |
| Book state and gap detection | `feed/book.py` | Per-subscription sequence tracking, per-market books, digests. |
| Durable log | `feed/recorder.py` | Append-only JSONL, verbatim raw payloads, contiguous ordinals. |
| Transport | `feed/ws_client.py` | Auth handshake, heartbeat, bounded-jitter reconnect. |
| Capture daemon | `ops/capture_daemon.py` | Orchestration, resync, digest and health emission. |
| Health accounting | `ops/monitor.py` | Gapped-time ledger, clock-offset reservoir. |
| Preflight | `ops/preflight.py` | Credentials, limits, universe — before any capture starts. |
| Offline replay | `research/replay.py` | Rebuilds books from the log, checks against live digests. |
| Exit-criteria report | `research/phase1_report.py` | Evaluates all four Phase 1 gates across sessions. |

## 2. How each exit criterion is measured

**"14+ consecutive days of capture with <0.1% of session time in gapped state."**
`ops/monitor.py` keeps a per-market ledger of valid vs. gapped seconds. A market is
gapped from subscription until its first snapshot, and from any sequence gap,
integrity error, or reconnect until its next snapshot. The reported fraction is
market-weighted, so one permanently broken market out of 120 cannot hide inside a
healthy average. `research/phase1_report.py` aggregates across sessions and
returns a nonzero exit code if the gate fails.

**"Books reconstructable offline from logs, byte-identical to live state on replay."**
The recorder stores each WebSocket frame **verbatim** as a string, so the log is
lossless regardless of parser bugs. Every `digest_interval` the daemon writes a
`book_digest` record: a SHA-256 over each valid book's canonical serialization plus
an aggregate over all books. `research/replay.py` re-runs the identical book code
over the log and compares at every digest record. Divergence is an error, not a
warning. This is proven in `tests/test_capture_replay.py`, which drives a real
capture through a deliberate sequence gap and a mid-stream disconnect, then
replays and asserts zero mismatches.

**"Clock offset distribution characterized."**
Every message carrying `ts_ms` contributes `recv_ms - ts_ms` to a reservoir sample,
reported overall and per message type with min/p01/p50/p95/p99/max.

## 3. API facts verified against `docs.kalshi.com` on 2026-08-19

Verified from the authoritative AsyncAPI spec (`docs.kalshi.com/asyncapi.yaml`) and
the REST reference, not from memory.

Confirmed as stated in the north star: RSA-PSS SHA-256 over
`timestamp + method + path`, path signed without query parameters, timestamp in
milliseconds; WebSocket signing path `/trade-api/ws/v2`; server pings every ~10s;
fixed-point strings with `_dollars` / `_fp` suffixes; price-then-time matching.

### Corrections and additions to §2 of the north star

1. **The WebSocket host differs from the REST host.** Production WebSocket is
   `wss://external-api-ws.kalshi.com/trade-api/ws/v2`, not the REST host. §2 lists
   only REST hosts, which invites the wrong URL.

2. **`seq` is per-subscription, not per-market.** The sequence number lives on the
   message envelope alongside `sid`, and one `orderbook_delta` subscription can
   cover many markets. A gap therefore means *some* market in that subscription
   missed an update, but not which one — so **every** book under that `sid` must be
   invalidated, not just one. I2 should be read as plural.

   This makes subscription shard size a correctness-adjacent tuning knob, not a
   cosmetic one: it is exactly the blast radius of a single gap. Default is 20
   markets per subscription (`config/exchange.py:MARKETS_PER_SUBSCRIPTION`).

3. **The REST orderbook carries no sequence number**, so it cannot be spliced into
   the WebSocket sequence stream — you cannot know which deltas it supersedes. I2's
   "refetch a REST snapshot" is therefore not implementable correctly as written.
   The correct primitive is the WebSocket
   `update_subscription` command with `action: get_snapshot`, whose snapshot arrives
   **in-band with its own `seq`**, making the supersession point unambiguous. This
   is what the daemon does. REST orderbook is used for nothing in the recovery path.

4. **The maker-fee list is `fee_type`, not `fee_multiplier`** — and it is far smaller
   than §2 implies. `GET /series` returns both fields per series. They mean different
   things and conflating them inverts the entire fee picture:

   - `fee_type: "quadratic"` → taker fees only, **`M_maker = 0`**
   - `fee_type: "quadratic_with_maker_fees"` → **`M_maker` > 0**
   - `fee_multiplier` (1, 0.5, or 0) is a **separate scalar that scales both legs**

   Measured on 2026-08-19 across all 13,306 series: **130 charge maker fees, 13,176 do
   not.** That is ~1% of the exchange, not the "essentially all liquid markets" §2
   describes. 107 of the 130 are Sports.

   This answers open question §10 without `/margin/fee_tiers`, which turns out to
   cover only 16 perpetual crypto series on a completely different linear schedule
   (maker 0.0002 / taker 0.0012) — not a general source. §10 can be closed: prefer
   `GET /series`.

   Two further findings §2 does not anticipate:

   - **`fee_multiplier` can be 0.5.** `KXMLBGAME` is `quadratic_with_maker_fees` at
     0.5 — maker fees at *half* rate. Treating the maker list as binary overstates
     its cost by 2x on one of the highest-volume series on the exchange.
   - **14 series charge no fees at all**, both legs, via `fee_multiplier: 0` —
     `KXTRUMPOUT`, `KXBTCY`, `KXGREENLAND`, `KXGDPYEAR` and others in
     Elections/Politics/Economics/Crypto. On these the breakeven model's entire fee
     term vanishes and net edge is spread minus adverse selection alone.

   **§2's central claim needs qualifying.** It says the `M_maker = 1` list "includes
   essentially all liquid markets" and that the long tail is where maker fees vanish.
   The first half is false: the highest-volume series on the exchange, `KXBTC15M`
   (12.3B contracts) and `KXBTCD` (6.4B), are both maker-fee-**free**, as are
   `KXATPCHALLENGERMATCH`, `KXITFMATCH`, `KXUFCFIGHT` and `KXMLBTOTAL`. High volume
   and maker-fee-free are not mutually exclusive. The strategic implication is
   *better* than §2 assumes — but only if Phase 1 actually captures those markets.

5. **`use_yes_price` will change meaning under us.** The orderbook subscription
   accepts `use_yes_price`, today defaulting to `false` — no-side levels quoted in
   no-leg pricing, which is what makes `edge = 100 - (yes_bid + no_bid)` correct.
   Kalshi has announced it will flip the default to `true` and later remove the flag
   entirely. A silent flip would reinterpret every recorded no-side price and
   silently corrupt Phase 2. The daemon therefore always sends the flag explicitly
   and records its value in the manifest.

6. **`taker_side` is deprecated** in favour of `taker_outcome_side` and
   `taker_book_side`. Trade direction is the conditioning variable for the adverse
   selection curve — the single most important Phase 2 output — so this matters. The
   recorder stores raw frames, so all three fields are preserved whatever happens to
   the deprecation.

7. **Per-subscription market limits exist** (WebSocket error 26) but the spec does
   not publish the value. Sharding keeps us well clear; error 26 and the
   buffer-overflow error 25 are recorded as integrity events if they ever appear.

## 4. Deliberate deviation from I9

I9 says halting beats guessing. For a **read-only** capture, halting the process on
an unrecognized message would trade a real exit criterion (14 days of continuity)
for no safety benefit — there is no capital at risk and the raw bytes are already
durably logged. I9 is therefore scoped to market granularity in Phase 1: an
unparseable frame, unknown message type, or book integrity violation is recorded as
an integrity event and **that market's book is invalidated and resynced**, while
capture continues. Nothing is guessed and nothing is patched forward.

This scoping is Phase-1-only. When `exec/` exists, I9 applies at process
granularity as written.

## 5. Universe design: stratified by fee regime

Because the fee landscape above is the "central fact shaping strategy selection"
(§2), the universe is stratified by fee regime rather than by vertical alone. Ranking
by volume within a category — the obvious approach — selects almost entirely
maker-charged series, which is precisely the half of the exchange §2 predicts is
structurally unprofitable. Phase 2 would then have analysed only the markets that
cannot work and concluded there is no edge.

Resolved universe as of 2026-08-20, from `.venv/bin/python -m ops.preflight`:

| Stratum | Markets | Regime |
|---|---|---|
| `weather_maker_free` | 25 | maker-free |
| `culture_maker_free` | 25 | maker-free |
| `econ_maker_free` | 20 | maker-free |
| `sports_maker_free` | 20 | maker-free |
| `sports_maker_charged` | 20 | maker-charged |
| `econ_maker_charged` | 12 | maker-charged |
| `zero_fee` | 12 | no fees, either leg |
| `culture_maker_charged` | 3 | maker-charged |
| **Total** | **137** across 40 series | 90 free / 35 charged / 12 zero |

The maker-charged strata are retained deliberately as a **control**. §2 predicts they
are unprofitable before adverse selection is considered; Phase 2 should be able to
*measure* that rather than assume it. If the maker-charged markets show positive net
edge, something is wrong with the fee model and the whole analysis is suspect.

Each market's `fee_type`, `fee_multiplier`, `maker_multiplier`, `taker_multiplier`
and `fee_regime` are written into the session manifest at capture time, so Phase 2
never has to reconstruct what the fee regime was.

## 6. Known limitations

- The category names in `config/markets.yaml` are guesses at Kalshi's taxonomy.
  `ops/preflight.py` reports which configured categories matched nothing and lists
  every category the exchange actually returned, so the file can be corrected in one
  pass before capture starts. Preflight refuses to report ready if any vertical is
  empty or the universe is under 100 markets.
- Universe is resolved once at startup. Daily weather markets will expire mid-run;
  a long capture should be restarted daily, or `universe_refresh_seconds` wired up.
- Interest paid on idle balance and open positions is not yet modelled. It is not
  needed for capture, but §2 requires it before any Phase 3 P&L number is believed.

## 7. Running it

```
pip install -r requirements.txt
export KALSHI_API_KEY_ID=...
export KALSHI_PRIVATE_KEY_PATH=/path/to/key.pem
export KALSHI_ENV=demo

.venv/bin/python -m ops.preflight
.venv/bin/python -m ops.capture_cli --data-root data
.venv/bin/python -m research.replay data/<session-id>
.venv/bin/python -m research.phase1_report --data-root data
```
