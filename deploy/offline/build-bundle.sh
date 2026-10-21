#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="${1:-$(date +%Y%m%d-%H%M%S)}"
OUT="${2:-$ROOT/runtime/offline/ppt-as-$VERSION}"
cd "$ROOT"

command -v docker >/dev/null
mkdir -p "$OUT/images" "$OUT/python-wheels"

docker build -f deploy/docker/agent.Dockerfile -t "ppt-as-agent:$VERSION" .
docker build -f deploy/docker/backend.Dockerfile -t "ppt-as-backend:$VERSION" .
docker build -f deploy/docker/frontend.Dockerfile -t "ppt-as-frontend:$VERSION" .

docker save \
  "ppt-as-agent:$VERSION" \
  "ppt-as-backend:$VERSION" \
  "ppt-as-frontend:$VERSION" \
  | gzip -1 > "$OUT/images/ppt-as-images-$VERSION.tar.gz"

git archive --format=tar.gz --prefix="ppt-as-$VERSION/" HEAD > "$OUT/ppt-as-source-$VERSION.tar.gz"

python3 -m pip download \
  --only-binary=:all: \
  -r agent_service/requirements.txt \
  -d "$OUT/python-wheels"

cp -a deploy "$OUT/deploy"
cp .env.example "$OUT/.env.example"
cp deploy/offline/install.sh "$OUT/install-offline.sh"
cp deploy/offline/verify.sh "$OUT/verify-offline.sh"

(
  cd "$OUT"
  find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
)

echo "离线包已生成：$OUT"
