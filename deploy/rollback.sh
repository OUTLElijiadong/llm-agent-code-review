#!/usr/bin/env bash
# 切回 previous.env 记录的应用镜像；不自动恢复或降级数据库。
set -Eeuo pipefail

cd "$(dirname "$0")"
# shellcheck source=lib/common.sh
source "lib/common.sh"

# 输出命令帮助。
# 参数: 无。
# 返回: 始终返回 0。
usage() {
  cat <<'USAGE'
用法: ./rollback.sh [all|backend|frontend] --confirm ROLLBACK_APPLICATION

说明: 仅切换应用镜像，不执行 Alembic downgrade 或数据库恢复。
USAGE
}

target="all"
confirmation=""
from_deploy_failure=0
target_seen=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    all|backend|frontend)
      [[ "$target_seen" == "0" ]] || fatal "只能指定一个回滚目标"
      target="$1"
      target_seen=1
      shift
      ;;
    --confirm)
      [[ $# -ge 2 ]] || fatal "--confirm 缺少值"
      confirmation="$2"
      shift 2
      ;;
    --from-deploy-failure)
      from_deploy_failure=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fatal "未知参数: $1"
      ;;
  esac
done

[[ "$confirmation" == "ROLLBACK_APPLICATION" ]] || fatal "确认口令不匹配，回滚已取消"
require_commands docker curl
validate_compose_environment
release_dir="${RELEASE_STATE_DIR:-.releases}"
current_state="$release_dir/current.env"
previous_state="$release_dir/previous.env"
pending_state="$release_dir/pending.env"
[[ -f "$previous_state" ]] || fatal "不存在上一发布状态: $previous_state"

lock_dir="$release_dir/.deploy.lock"
if [[ "$from_deploy_failure" != "1" ]]; then
  acquire_directory_lock "$lock_dir"
  trap 'release_directory_lock "$lock_dir"' EXIT
fi

load_release_environment "$previous_state"
previous_sha="$APP_RELEASE"
if [[ "$target" == "all" || "$target" == "backend" ]]; then
  release_image_exists prism-backend "$BACKEND_RELEASE" \
    || fatal "回滚 Backend 镜像不存在: prism-backend:$BACKEND_RELEASE"
fi
if [[ "$target" == "all" || "$target" == "frontend" ]]; then
  release_image_exists prism-frontend "$FRONTEND_RELEASE" \
    || fatal "回滚 Frontend 镜像不存在: prism-frontend:$FRONTEND_RELEASE"
fi

log_warn "开始应用层回滚(target=$target, release=$previous_sha)；数据库保持当前 revision"
if [[ "$target" == "all" || "$target" == "backend" ]]; then
  compose up -d --no-deps backend
  wait_for_service_health backend "${BACKEND_HEALTH_TIMEOUT:-240}" || fatal "回滚 Backend 不健康"
  curl --fail --silent --show-error --max-time 15 \
    "${BACKEND_SMOKE_URL:-http://127.0.0.1:8000}/healthz" >/dev/null \
    || fatal "回滚 Backend 存活探测失败"
fi
if [[ "$target" == "all" || "$target" == "frontend" ]]; then
  compose up -d --no-deps frontend
  wait_for_service_health frontend "${FRONTEND_HEALTH_TIMEOUT:-120}" || fatal "回滚 Frontend 不健康"
fi
smoke_https unknown || fatal "回滚后 HTTPS 冒烟失败"

mkdir -p "$release_dir"
rollback_from="$release_dir/rollback-from-$(date -u '+%Y%m%d%H%M%S').env"
if [[ -f "$current_state" ]]; then
  cp "$current_state" "$rollback_from"
fi
cp "$previous_state" "$current_state"
if [[ -f "$rollback_from" ]]; then
  cp "$rollback_from" "$previous_state"
fi
rm -f "$pending_state"
log_info "应用层回滚完成(release=$previous_sha)；请确认数据库向后兼容性"
compose ps
