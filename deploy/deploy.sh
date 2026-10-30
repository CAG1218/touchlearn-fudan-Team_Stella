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
  echo "已生成 deploy/.env，请填写 DEPLOY_MODE、TOUCHLEARN_PUBLIC_URL 和访问码后重跑。" >&2
  exit 1
fi

mode="$(grep -E '^DEPLOY_MODE=' .env | tail -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
mode="${mode:-domain}"
case "$mode" in
  domain)
    compose_file="docker-compose.yml"
    ;;
  ip)
    compose_file="docker-compose.ip.yml"
    ;;
  *)
    echo "DEPLOY_MODE 只能是 domain 或 ip，当前为：${mode}" >&2
    exit 1
    ;;
esac

if [ "$mode" = "domain" ] && grep -q "touch.example.com" .env; then
  echo "deploy/.env 里还是示例域名，请改成你自己的已备案域名。" >&2
  exit 1
fi

if grep -q "TL-XXXXXXXX" .env; then
  echo "deploy/.env 里还是示例访问码，请生成一个新的。" >&2
  exit 1
fi

if [ "$mode" = "ip" ]; then
  public_url="$(grep -E '^TOUCHLEARN_PUBLIC_URL=' .env | tail -1 | cut -d= -f2- || true)"
  case "$public_url" in
    http://*) ;;
    *)
      echo "IP 模式请在 .env 设置 TOUCHLEARN_PUBLIC_URL=http://<公网IP>:8765" >&2
      exit 1
      ;;
  esac
  echo "IP 模式：不启用 HTTPS，请确认安全组只放行 22 和 8765。" >&2
fi

docker compose --env-file .env -f "$compose_file" up -d --build --remove-orphans
docker compose --env-file .env -f "$compose_file" ps

address="$(grep -E '^TOUCHLEARN_PUBLIC_URL=' .env | tail -1 | cut -d= -f2- || true)"
echo
echo "部署完成：${address:-http://<服务器 IP>}"
echo "健康检查：${address%/}/api/health"
