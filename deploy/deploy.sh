#!/usr/bin/env bash
# 在服务器上启动或更新 TouchLearn。首次运行前先复制 .env.example 为 .env。
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "未找到 docker，请先安装 Docker Engine 与 compose 插件。" >&2
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "已生成 deploy/.env，请填写 SITE_ADDRESS、TOUCHLEARN_PUBLIC_URL 和访问码后重跑。" >&2
  exit 1
fi

if grep -q "touch.example.com" .env; then
  echo "deploy/.env 里还是示例域名，请改成你自己的域名或 :80。" >&2
  exit 1
fi

if grep -q "TL-XXXXXXXX" .env; then
  echo "deploy/.env 里还是示例访问码，请生成一个新的。" >&2
  exit 1
fi

docker compose --env-file .env up -d --build
docker compose ps

address="$(grep -E '^TOUCHLEARN_PUBLIC_URL=' .env | tail -1 | cut -d= -f2-)"
echo
echo "部署完成：${address:-http://<服务器 IP>}"
echo "健康检查：${address%/}/api/health"
