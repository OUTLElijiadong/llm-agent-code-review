#!/usr/bin/env bash
# ============================================================
# 本地开发一键启动：后端热重载(uvicorn --reload) + 前端 HMR(vite)
# 数据库复用 docker 的 MySQL 容器(cr_mysql，本机 3307 端口)
# 用法：  ./dev.sh        Ctrl+C 一并停止前后端(MySQL 继续后台跑)
# ============================================================
set -uo pipefail
cd "$(dirname "$0")"

# 1) 确保 docker 的 MySQL 在跑（前后端都本地跑，只依赖这个容器）
if ! docker ps --filter "name=cr_mysql" --filter "status=running" --format '{{.Names}}' | grep -q cr_mysql; then
  echo "▶ MySQL 容器没在跑，启动中…"
  docker compose -f deploy/docker-compose.yml up -d mysql
  echo "  等待 MySQL 就绪…"
  until docker exec cr_mysql mysqladmin ping -h localhost --silent >/dev/null 2>&1; do sleep 1; done
fi
echo "✓ MySQL 就绪 (localhost:3307)"

# 2) 后端：热重载（改 .py 自动重启）
# 显式连本机 docker MySQL 的映射端口 3307（compose 把容器 3306 映射到主机 3307）。
# 这里覆盖 .env，避免全新 checkout 按 .env.example 的默认 3306 连不上数据库。
echo "▶ 启动后端  http://localhost:8000  (--reload)"
( cd backend && source .venv/bin/activate && export DB_HOST=127.0.0.1 DB_PORT=3307 && exec uvicorn app.main:app --reload --port 8000 ) &
BACK_PID=$!

# 3) 前端：HMR（改 src 即时热更新）
echo "▶ 启动前端  http://localhost:5173  (HMR)"
( cd frontend && exec npm run dev ) &
FRONT_PID=$!

# Ctrl+C / 退出时一并清理前后端
trap 'echo; echo "停止前后端…"; kill "$BACK_PID" "$FRONT_PID" 2>/dev/null || true; exit 0' INT TERM

echo ""
echo "============================================================"
echo "  ✅ 本地开发已启动 —— 浏览器打开  http://localhost:5173"
echo "     改 backend/  → 后端自动重载"
echo "     改 frontend/src/ → 前端即时热更新"
echo "     Ctrl+C 停止前后端（MySQL 容器继续后台运行）"
echo "============================================================"
wait
