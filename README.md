# kalshibot

Kalshi market making research. Governed by [KALSHI_MM_NORTH_STAR.md](KALSHI_MM_NORTH_STAR.md).

**Current phase: 1 — read-only capture. Not trading. No capital at risk.**

## Setup

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# every command below uses .venv/bin/python -- the deps live only in the venv,
# and macOS has no bare `python` on PATH
export KALSHI_API_KEY_ID=...
export KALSHI_PRIVATE_KEY_PATH=/path/to/key.pem
export KALSHI_ENV=demo          # or prod
```

Secrets come from the environment only. Never commit a key.

## Commands

| Command | Purpose |
|---|---|
| `.venv/bin/python -m ops.preflight` | Check credentials, rate limits, and resolve the market universe. Run before capture. |
| `.venv/bin/python -m ops.capture_cli --data-root data` | Run the capture daemon. Ctrl-C stops cleanly. |
| `.venv/bin/python -m research.replay data/<session-id>` | Rebuild books offline and verify against live digests. Exit 0 means byte-identical. |
| `.venv/bin/python -m research.phase1_report --data-root data` | Evaluate all four Phase 1 exit criteria. Exit 0 means the gate passes. |
| `.venv/bin/python -m pytest` | Test suite, including an end-to-end capture-and-replay proof. |

## Layout

`config/` exchange constants and the market universe spec · `auth/` request signing ·
`feed/` transport, book state, durable log · `ops/` capture daemon, health, preflight ·
`research/` offline analysis and replay · `tests/` · `docs/phase_reports/`

`research/` may import anything. Nothing imports `research/`. This is enforced by
`tests/test_layering.py`, which also fails the build if any trading-path module
gains an order-submission call site.

## Phase 1 exit criteria

1. 14+ consecutive days of capture with <0.1% of session time in gapped state.
2. Books reconstructable offline from logs, byte-identical to live state on replay.
3. Clock offset distribution characterized.

See [docs/phase_reports/phase_1_build.md](docs/phase_reports/phase_1_build.md) for how
each is measured, and for corrections to the north star's API notes found while
verifying against the live spec.
