#!/usr/bin/env bash
# Compress closed capture segments. Never touches the newest segment in a session
# directory, which may still be open for append: gzip unlinks the original after
# writing, and the daemon would go on writing into an unlinked inode.
set -euo pipefail

ROOT="${KALSHI_DATA_ROOT:-/home/ec2-user/kalshibot/data}"
AGE_MIN="${KALSHI_COMPRESS_AGE_MIN:-120}"

[ -d "$ROOT" ] || { echo "no data root at $ROOT"; exit 0; }

compressed=0
skipped_open=0

for dir in "$ROOT"/*/; do
    [ -d "$dir" ] || continue

    mapfile -t segments < <(find "$dir" -maxdepth 1 -name 'capture-*.jsonl' -print | sort)
    count=${#segments[@]}
    [ "$count" -gt 1 ] || { skipped_open=$((skipped_open + count)); continue; }

    skipped_open=$((skipped_open + 1))
    last=$((count - 1))
    for ((i = 0; i < last; i++)); do
        segment="${segments[$i]}"
        if [ -n "$(find "$segment" -maxdepth 0 -mmin +"$AGE_MIN" -print -quit 2>/dev/null)" ]; then
            gzip -9 "$segment"
            compressed=$((compressed + 1))
        fi
    done
done

echo "compressed=$compressed left_open=$skipped_open age_min=$AGE_MIN root=$ROOT"
