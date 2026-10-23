#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p var
chmod 700 var
if [[ ! -f var/local.env ]]; then
  umask 077
  mem_token=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
  rsi_token=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
  admin_token=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
  printf 'COSENS_MEMORY_INTERNAL_TOKEN=%s\nCOSENS_RSI_TOKEN=%s\nCOSENS_RSI_ADMIN_TOKEN=%s\n' "$mem_token" "$rsi_token" "$admin_token" > var/local.env
fi
chmod 600 var/local.env
find var -maxdepth 1 -type f -exec chmod 600 {} +
set -a
source var/local.env
set +a
export MEMORY_DB="${MEMORY_DB:-$PWD/var/memory.sqlite3}"
export COSENS_RSI_DB="${COSENS_RSI_DB:-$PWD/var/cosens-rsi.sqlite3}"
export COSENS_MEMORY_DB="$MEMORY_DB"
export COSENS_MEMORY_URL=http://127.0.0.1:18104/memory
export COSENS_MEMORY_PROJECT=default
printf '%s\n' "Local dev credentials are in $PWD/var/local.env (mode 0600)."
printf '%s\n' "Start combined RSI + private Memory service: scripts/start-local.sh (0.0.0.0:18104)"
