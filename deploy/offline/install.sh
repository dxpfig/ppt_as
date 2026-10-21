#!/usr/bin/env bash
set -euo pipefail

BUNDLE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$BUNDLE"

sha256sum -c SHA256SUMS

IMAGE_ARCHIVE="$(find images -maxdepth 1 -name 'ppt-as-images-*.tar.gz' -print -quit)"
[[ -n "$IMAGE_ARCHIVE" ]] || {
  echo "找不到离线镜像包。" >&2
  exit 1
}
VERSION="$(basename "$IMAGE_ARCHIVE" | sed -E 's/^ppt-as-images-(.*)\.tar\.gz$/\1/')"
export PPT_AS_AGENT_IMAGE="ppt-as-agent:$VERSION"
export PPT_AS_BACKEND_IMAGE="ppt-as-backend:$VERSION"
export PPT_AS_FRONTEND_IMAGE="ppt-as-frontend:$VERSION"

gzip -dc "$IMAGE_ARCHIVE" | docker load

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  echo "已创建 .env，请填写离线模型、知识库和渲染器配置。" >&2
  exit 1
fi

docker compose -f deploy/docker/compose.yml up -d --no-build
echo "离线容器已启动；执行 ./verify-offline.sh 检查。"
