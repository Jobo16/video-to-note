#!/usr/bin/env bash
set -euo pipefail

exec 9>/tmp/url2audio.deploy.lock
flock -n 9 || { echo "已有发布正在执行" >&2; exit 1; }

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
test -s /opt/stacks/url2audio/.env
test -d /opt/stacks/url2audio/data
cd "$repo_dir"
docker compose -f url2audio/compose.yaml build
docker compose -f url2audio/compose.yaml up -d
curl -fsS --retry 20 --retry-connrefused --retry-delay 2 http://127.0.0.1:14175/health >/dev/null
