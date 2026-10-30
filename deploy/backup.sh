#!/usr/bin/env bash
# 备份产物卷到 deploy/backups/，默认保留 14 天。
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p backups

stamp="$(date +%Y%m%d-%H%M%S)"
archive="backups/touchlearn-outputs-${stamp}.tar.gz"

docker run --rm \
  -v touchlearn-outputs:/data:ro \
  -v "$PWD/backups":/backup \
  alpine:3.20 \
  tar czf "/backup/$(basename "$archive")" -C /data .

find backups -name 'touchlearn-outputs-*.tar.gz' -mtime +14 -delete
echo "已备份到 $archive"
