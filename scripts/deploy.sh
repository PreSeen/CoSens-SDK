#!/usr/bin/env bash
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for dependency in node npm git wrangler curl; do
  command -v "$dependency" >/dev/null || { echo "Missing required command: $dependency" >&2; exit 1; }
done

if [[ "$(git branch --show-current)" != main ]]; then
  echo 'Production deployment requires the main branch.' >&2
  exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo 'Commit your changes before deploying production.' >&2
  exit 1
fi

npm run build
npm test
if [[ -n "$(git status --porcelain)" ]]; then
  echo 'The build changed tracked assets. Commit the generated files, then deploy again.' >&2
  exit 1
fi

commit="$(git rev-parse HEAD)"
wrangler whoami
wrangler pages deploy dist --project-name cosens --branch main \
  --commit-hash "$commit" --commit-message "$(git log -1 --format=%s)"

COSENS_DEPLOY_COMMIT="$commit" npm run verify:live
