#!/usr/bin/env bash
# ============================================================
# 一键部署 / 更新脚本
# 在服务器上的 deploy/ 目录里运行：  ./deploy.sh
# 首次部署和日常更新都用它（幂等，重复跑安全）
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"

echo "📥 [1/4] 拉取最新代码..."
git -C .. pull --ff-only || echo "⚠️  git pull 跳过（本地有改动或非 git 目录），继续用当前代码部署"

echo "🔎 [2/4] 检查 .env..."
if [ ! -f .env ]; then
  echo "❌ 缺少 deploy/.env"
  echo "   请先执行：  cp .env.example .env   然后填写密码 / DeepSeek Key / JWT_SECRET"
  exit 1
fi

echo "🔨 [3/4] 构建并启动容器（首次会比较久）..."
docker compose up -d --build

echo "🧹 [4/4] 清理悬空镜像..."
docker image prune -f >/dev/null 2>&1 || true

echo ""
echo "✅ 部署完成！容器状态："
docker compose ps
echo ""
IP=$(curl -s --max-time 3 ifconfig.me 2>/dev/null || echo "你的服务器IP")
echo "🌐 前端访问:  http://$IP"
echo "📖 接口文档:  http://$IP/docs"
echo "📜 查看日志:  docker compose logs -f backend"
