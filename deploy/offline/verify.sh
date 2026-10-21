#!/usr/bin/env bash
set -euo pipefail

PORT="${PPT_AS_PORT:-8080}"
BASE="http://127.0.0.1:$PORT"

for path in / /api/health /api/studio/health; do
  status="$(curl -fsS -o /dev/null -w '%{http_code}' "$BASE$path")"
  [[ "$status" == "200" ]] || {
    echo "$path 返回 $status" >&2
    exit 1
  }
done

echo "HTTP 健康检查通过。"
echo "下一步：使用离线黄金样例生成一份 PPT，并验证 PPTX/PDF 和页面操作。"
