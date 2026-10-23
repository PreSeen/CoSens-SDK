#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
scripts/run-local.sh >/dev/null
uv sync --frozen
python3 - "$ROOT" <<'PY'
import pathlib, shutil, sys, time
root = pathlib.Path(sys.argv[1])
target = pathlib.Path.home() / '.config/systemd/user/cosens-rsi.service'
target.parent.mkdir(parents=True, exist_ok=True)
if target.exists():
    shutil.copy2(target, root / 'var' / f'cosens-rsi.service.backup-{time.time_ns()}')
target.write_text((root / 'deploy/cosens-rsi.service').read_text().replace('@ROOT@', str(root)))
PY
systemctl --user daemon-reload
systemctl --user enable cosens-rsi.service
systemctl --user restart cosens-rsi.service
"$ROOT/.venv/bin/python" - "$ROOT" <<'PY'
import pathlib, sys, time
import httpx
root = pathlib.Path(sys.argv[1])
config = dict(line.split('=', 1) for line in (root / 'var/local.env').read_text().splitlines() if line)
for _ in range(80):
    try:
        response = httpx.get('http://127.0.0.1:18104/health/ready',
            headers={'Authorization': 'Bearer ' + config['COSENS_RSI_TOKEN']}, timeout=2)
        if response.status_code == 200 and response.json().get('status') == 'ok':
            print('CoSens RSI + Friday Memory ready at http://127.0.0.1:18104; systemd user service enabled.')
            break
    except httpx.HTTPError:
        pass
    time.sleep(.25)
else:
    raise SystemExit('Startup failed; inspect var/rsi.log and systemctl --user status cosens-rsi.service')
PY
