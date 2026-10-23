#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
scripts/run-local.sh >/dev/null
set -a
source "$ROOT/var/local.env"
set +a
export MEMORY_DB="${MEMORY_DB:-$ROOT/var/memory.sqlite3}"
export COSENS_RSI_DB="${COSENS_RSI_DB:-$ROOT/var/cosens-rsi.sqlite3}"
export COSENS_MEMORY_URL=http://127.0.0.1:18104/memory
export COSENS_MEMORY_PROJECT=default

if curl --silent --fail -H "Authorization: Bearer $COSENS_RSI_TOKEN" \
  http://127.0.0.1:18104/health/ready >/dev/null; then
  echo "CoSens RSI and its private Memory adapter are already healthy on 127.0.0.1:18104."
  exit 0
fi

if ss -H -ltn 'sport = :18104' | grep -q .; then
  echo "Port 18104 is occupied by an unrecognized service; refusing to start a duplicate listener." >&2
  exit 1
fi

uv run --project "$ROOT" cosens-rsi --host 0.0.0.0 --port 18104 >var/rsi.log 2>&1 &
RSI_PID=$!
cleanup() {
  kill "$RSI_PID" 2>/dev/null || true
  wait "$RSI_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in $(seq 1 60); do
  if curl --silent --fail -H "Authorization: Bearer $COSENS_RSI_TOKEN" \
    http://127.0.0.1:18104/health/ready >/dev/null; then
    echo "CoSens RSI API/MCP and private Friday Memory adapter are ready on 127.0.0.1:18104. Ctrl-C stops the service."
    break
  fi
  if ! kill -0 "$RSI_PID" 2>/dev/null; then
    echo "Service startup failed; inspect var/rsi.log" >&2
    exit 1
  fi
  sleep 0.5
done
curl --silent --fail -H "Authorization: Bearer $COSENS_RSI_TOKEN" \
  http://127.0.0.1:18104/health/ready >/dev/null || {
  echo "Service did not become ready; inspect var/rsi.log" >&2
  exit 1
}
wait "$RSI_PID"
