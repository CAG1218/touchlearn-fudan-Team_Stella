#!/usr/bin/env bash
# TouchLearn v0.3.0 一键更新脚本（在服务器上以 root 运行）
#
# 用法：
#   bash /root/install.sh
# 或指定发布包和项目目录：
#   APP_DIR=/root/touchlearn-fudan-Team_Stella bash /root/install.sh /root/touchlearn-0.3.0.tar.gz

set -euo pipefail

ARCHIVE="${1:-/root/touchlearn-0.3.0.tar.gz}"

say()  { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "请用 root 运行：sudo bash $0"
[ -f "$ARCHIVE" ] || die "找不到发布包：$ARCHIVE"

if [ -n "${APP_DIR:-}" ]; then
  [ -f "$APP_DIR/deploy/.env" ] || die "APP_DIR 不正确，找不到 deploy/.env：$APP_DIR"
else
  mapfile -t candidates < <(
    find /root /opt /srv /home /usr/local /www /data /var/www /workspace -maxdepth 5 -name 'docker-compose*.yml' -printf '%h\n' 2>/dev/null \
      | sed 's#/deploy$##' | sort -u
  )
  if [ "${#candidates[@]}" -eq 0 ]; then
    die "没找到现有项目目录。请手动指定：APP_DIR=/你的目录 bash $0 $ARCHIVE"
  elif [ "${#candidates[@]}" -gt 1 ]; then
    warn "找到多个候选目录："
    printf '  %s\n' "${candidates[@]}"
    die "请指定正确目录：APP_DIR=/正确目录 bash $0 $ARCHIVE"
  fi
  APP_DIR="${candidates[0]}"
fi

cd "$APP_DIR"
[ -f deploy/.env ] || die "$APP_DIR/deploy/.env 不存在，无法保留配置"
[ -f deploy/docker-compose.ip.yml ] || [ -f deploy/docker-compose.yml ] || die "缺少 compose 文件"

say "项目目录：$APP_DIR"
say "更新前版本：$(grep -oE '__version__ = "[0-9.]+"' src/touchlearn/__init__.py 2>/dev/null || echo 未知)"

STAMP="$(date +%F-%H%M)"
cp deploy/.env "/root/touchlearn-env-$STAMP.bak"
say "已备份环境变量：/root/touchlearn-env-$STAMP.bak"

if docker volume inspect touchlearn-outputs >/dev/null 2>&1; then
  say "备份账号库与历史作业（可能几十秒）..."
  if docker run --rm -v touchlearn-outputs:/data -v /root:/backup alpine:3.20 \
      tar czf "/backup/touchlearn-outputs-$STAMP.tar.gz" -C /data .; then
    say "已备份数据卷：/root/touchlearn-outputs-$STAMP.tar.gz"
  else
    warn "数据卷备份失败，继续部署。请稍后手动备份 touchlearn-outputs 卷。"
  fi
else
  warn "没有找到 touchlearn-outputs 数据卷，跳过数据备份。"
fi

say "解压 v0.3.0 代码..."
tar -xzf "$ARCHIVE" -C "$APP_DIR"
grep -q '__version__ = "0.3.0"' src/touchlearn/__init__.py \
  || die "解压后版本不是 0.3.0，已停止；原 deploy/.env 未改动。"

ENV_FILE="$APP_DIR/deploy/.env"
set_env() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

set_env TOUCHLEARN_3D_PROVIDER tokenhub
set_env TOUCHLEARN_3D_MODEL hy-3d-3.1
set_env TOUCHLEARN_3D_FACES 300000
set_env TOUCHLEARN_3D_TIMEOUT 600
set_env TOUCHLEARN_3D_POLL 4

if ! grep -qE '^TOUCHLEARN_LLM_KEY=.+' "$ENV_FILE" && ! grep -qE '^TOUCHLEARN_3D_KEY=.+' "$ENV_FILE"; then
  warn "没有检测到 TokenHub Key。"
  read -rsp "请粘贴 TokenHub API Key（输入不回显，直接回车跳过）: " TLKEY
  echo
  if [ -n "${TLKEY:-}" ]; then
    set_env TOUCHLEARN_3D_KEY "$TLKEY"
    say "已写入 TOUCHLEARN_3D_KEY。"
  else
    warn "已跳过。照片级重建可能显示未配置，可稍后编辑 deploy/.env 补上。"
  fi
fi

if ! grep -qE '^TOUCHLEARN_3D_KEY=.+' "$ENV_FILE"; then
  llm_provider="$(grep -E '^TOUCHLEARN_LLM_PROVIDER=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
  if [ -n "$llm_provider" ] && [ "$llm_provider" != "tokenhub" ]; then
    warn "TOUCHLEARN_LLM_PROVIDER=$llm_provider，而 TOUCHLEARN_3D_KEY 为空。"
    warn "TokenHub 3D 不会复用非 TokenHub 的 Key；请稍后编辑 deploy/.env 填写 TOUCHLEARN_3D_KEY。"
  fi
fi

mode="$(grep -E '^DEPLOY_MODE=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
mode="${mode:-domain}"
if [ "$mode" = "ip" ]; then
  COMPOSE_FILE="docker-compose.ip.yml"
else
  COMPOSE_FILE="docker-compose.yml"
fi

if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  die "没有找到 docker compose，请先安装 Docker Compose 插件。"
fi

say "使用 $COMPOSE_FILE 构建并启动 v0.3.0（首次约 3–8 分钟）..."
cd deploy
"${DC[@]}" --env-file .env -f "$COMPOSE_FILE" up -d --build --remove-orphans

say "等待服务就绪..."
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8765/api/health >/tmp/touchlearn-health.json 2>/dev/null; then
    break
  fi
  sleep 2
done

say "健康检查："
cat /tmp/touchlearn-health.json 2>/dev/null | python3 -m json.tool || warn "健康检查失败，请执行：cd $APP_DIR/deploy && ${DC[*]} -f $COMPOSE_FILE logs --tail 100"

say "三维重建配置："
"${DC[@]}" --env-file .env -f "$COMPOSE_FILE" exec -T touchlearn \
  python -c "import json; from touchlearn.agent.reconstruct import reconstruction_status as s; print(json.dumps(s(), ensure_ascii=False, indent=2))" \
  || warn "无法读取三维配置，请检查容器是否已启动。"

say "部署完成。浏览器打开：http://47.103.61.115:8765"
say "回滚环境变量：cp /root/touchlearn-env-$STAMP.bak $APP_DIR/deploy/.env"
