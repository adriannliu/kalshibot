#!/usr/bin/env bash
# Run ON the capture instance, after the repo and the Kalshi key are in place:
#   bash ~/kalshibot/deploy/bootstrap-instance.sh use1
set -euo pipefail

SITE="${1:-}"
[ -n "$SITE" ] || { echo "usage: $0 <site-label>   e.g. $0 use1" >&2; exit 2; }

ROOT="$HOME/kalshibot"
SHARDS="${SHARDS:-4}"
KEY="$HOME/.kalshi/kalshi_key.pem"

cd "$ROOT"

echo "==> system packages"
sudo dnf install -y -q git python3.11 python3.11-pip rsync >/dev/null

echo "==> clock discipline"
sudo systemctl enable --now chronyd >/dev/null 2>&1 || true
chronyc tracking | sed -n '1,4p;/System time/p;/Leap status/p' || {
    echo "chrony is not tracking; clock offset measurements would be meaningless" >&2
    exit 1
}

echo "==> python environment"
[ -d .venv ] || python3.11 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

echo "==> credentials"
[ -f "$KEY" ] || { echo "missing $KEY -- scp it up first" >&2; exit 1; }
chmod 600 "$KEY"
mkdir -p "$HOME/.kalshi"

if [ ! -f "$HOME/.kalshi/env" ]; then
    cat > "$HOME/.kalshi/env" <<ENVEOF
KALSHI_API_KEY_ID=REPLACE_ME
KALSHI_PRIVATE_KEY_PATH=$KEY
KALSHI_ENV=prod
KALSHI_SITE=$SITE
KALSHI_SHARD_COUNT=$SHARDS
ENVEOF
    chmod 600 "$HOME/.kalshi/env"
    echo "    wrote $HOME/.kalshi/env -- set KALSHI_API_KEY_ID, then re-run"
    exit 1
fi

grep -q '^KALSHI_API_KEY_ID=REPLACE_ME' "$HOME/.kalshi/env" && {
    echo "    KALSHI_API_KEY_ID is still REPLACE_ME in ~/.kalshi/env" >&2
    exit 1
}
grep -q '^export ' "$HOME/.kalshi/env" && {
    echo "    ~/.kalshi/env must not use 'export'; systemd EnvironmentFile rejects it" >&2
    exit 1
}

echo "==> tests"
./.venv/bin/python -m pytest -q

echo "==> preflight"
set -a; . "$HOME/.kalshi/env"; set +a
mkdir -p "$ROOT/data"
./.venv/bin/python -m ops.preflight >"$ROOT/data/preflight.json" || {
    echo "preflight failed; see $ROOT/data/preflight.json" >&2
    exit 1
}
./.venv/bin/python -c "
import json;d=json.load(open('$ROOT/data/preflight.json'))
print('    ready:',d['ready'],'markets:',d['universe']['markets'],'regimes:',d['universe']['fee_regime_counts'])"

echo "==> first universe snapshot"
./.venv/bin/python -m ops.resolve_universe --out "$ROOT/data/universe.json" --shard-count "$SHARDS"

echo "==> installing systemd units"
sudo cp deploy/kalshi-capture@.service deploy/kalshi-capture.target \
        deploy/kalshi-universe.service deploy/kalshi-universe.timer \
        deploy/kalshi-compress.service deploy/kalshi-compress.timer \
        /etc/systemd/system/
sudo install -m 440 -o root -g root deploy/kalshi-sudoers /etc/sudoers.d/kalshi
sudo visudo -c >/dev/null
sudo systemctl daemon-reload

for i in $(seq 0 $((SHARDS - 1))); do
    sudo systemctl enable --now "kalshi-capture@${i}.service"
done
sudo systemctl enable --now kalshi-universe.timer kalshi-compress.timer

sleep 20
echo
systemctl --no-pager --plain list-units 'kalshi-capture@*' || true
echo
echo "capture started at site=$SITE with $SHARDS shards"
echo "watch:   journalctl -u 'kalshi-capture@*' -f"
echo "report:  cd $ROOT && ./.venv/bin/python -m research.phase1_report --data-root data"
