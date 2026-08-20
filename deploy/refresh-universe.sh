#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/ec2-user/kalshibot
PYTHON="$ROOT/.venv/bin/python"
TARGET="$ROOT/data/universe.json"
SHARDS="${KALSHI_SHARD_COUNT:-4}"

cd "$ROOT"

STAGED="$(mktemp "$ROOT/data/universe.XXXXXX.json")"
trap 'rm -f "$STAGED"' EXIT

if ! "$PYTHON" -m ops.resolve_universe --out "$STAGED" --shard-count "$SHARDS" --min-markets 100; then
    echo "universe resolution failed; keeping existing $TARGET" >&2
    exit 1
fi

mv "$STAGED"  "$TARGET"
trap - EXIT

for index in $(seq 0 $((SHARDS - 1))); do
    sudo systemctl restart "kalshi-capture@${index}.service"
    sleep 15
done
