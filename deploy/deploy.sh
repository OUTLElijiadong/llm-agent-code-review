#!/usr/bin/env bash
# ============================================================
# 一键部署 / 更新脚本（在服务器 deploy/ 目录里运行）
#   用法：
#     ./deploy.sh            全量更新（前后端都重建，首次部署 / 后端有改动时用）
#     ./deploy.sh frontend   只更新前端（--no-deps：不重建、不重启 backend / mysql）
#     ./deploy.sh backend    只更新后端（不动 frontend / mysql）
# 幂等，重复跑安全。零停机：新镜像构建成功前旧容器不停。
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"

TARGET="${1:-all}"

echo "📥 [1/4] 同步最新代码 (origin/main)..."
# /opt/code-review 是 --depth 1 浅克隆：git pull --ff-only 会“静默不前进”，
# 必须 fetch + reset --hard。.env 是 gitignored，reset 不会动它。
if git -C .. rev-parse --git-dir >/dev/null 2>&1; then
  git -C .. fetch origin main
  git -C .. reset --hard origin/main
  echo "   当前代码 = $(git -C .. rev-parse --short HEAD)  ($(git -C .. log -1 --format=%s))"
else
  echo "⚠️  非 git 目录，跳过同步，用当前代码部署"
fi

echo "🔎 [2/4] 检查 .env..."
if [ ! -f .env ]; then
  echo "❌ 缺少 deploy/.env"
  echo "   请先执行：  cp .env.example .env   然后填写密码 / DeepSeek Key / JWT_SECRET"
  exit 1
fi

echo "🔨 [3/4] 构建并启动容器（target=$TARGET，首次会比较久）..."
case "$TARGET" in
  frontend)
    # 只构建前端镜像；--no-deps 确保不重建、不重启 backend / mysql（后端零波及）。
    # 注意：用 `up --build frontend`（不带 --no-deps）会把 backend 当依赖一起重建，
    # 导致后端无谓 recreate 抖动；这里分两步并显式 --no-deps 规避。
    docker compose build frontend
    docker compose up -d --no-deps frontend
    ;;
  backend)
    docker compose build backend
    docker compose up -d --no-deps backend
    ;;
  all|*)
    docker compose up -d --build
    ;;
esac

echo "🧹 [4/4] 清理悬空镜像..."
docker image prune -f >/dev/null 2>&1 || true

echo ""
echo "✅ 部署完成！容器状态："
docker compose ps
echo ""
# 说明：公网 443/HTTPS 被云侧（腾讯云/运营商边界）RST 注入，对外仅经 HTTP/80 访问。
SERVER_IP=$(awk -F= '/^SERVER_IP=/{print $2}' .env 2>/dev/null | tail -n 1 | tr -d "\"'")
SERVER_IP=${SERVER_IP:-81.70.251.90}
echo "🌐 前端访问:  http://$SERVER_IP"
echo "📖 接口文档:  http://$SERVER_IP/docs"
echo "📜 查看日志:  docker compose logs -f backend"
