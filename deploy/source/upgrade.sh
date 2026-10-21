#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

backup="runtime/backups/source-upgrade-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup"
cp -a .env "$backup/.env"

./stop.sh || true
pnpm install --frozen-lockfile
uv pip install --python .venv/bin/python -r agent_service/requirements.txt
pnpm build
./start.sh

echo "升级完成；配置备份：$backup/.env"
