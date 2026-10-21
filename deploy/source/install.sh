#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

for command in node pnpm python3 uv; do
  command -v "$command" >/dev/null || {
    echo "缺少命令：$command" >&2
    exit 1
  }
done

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  echo "已创建 .env，请填写模型、知识库与渲染器配置后重新执行。" >&2
  exit 1
fi

mkdir -p runtime/studio runtime/services
pnpm install --frozen-lockfile
if [[ ! -x .venv/bin/python ]]; then
  uv venv .venv
fi
uv pip install --python .venv/bin/python -r agent_service/requirements.txt
pnpm build

echo "源码部署构建完成。"
echo "启动：./start.sh"
echo "状态：./status.sh"
