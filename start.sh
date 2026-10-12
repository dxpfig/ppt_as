#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  echo '缺少项目虚拟环境。请先运行：uv venv .venv && uv pip install --python .venv/bin/python -r agent_service/requirements.txt' >&2
  exit 1
fi
case "${1:-}" in
  '') action=start ;;
  --restart) action=restart ;;
  *) echo '用法：./start.sh [--restart]' >&2; exit 2 ;;
esac
exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/services.py" "$action"
