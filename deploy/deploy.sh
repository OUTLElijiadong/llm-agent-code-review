#!/usr/bin/env bash
# 按精确 Git SHA 执行带备份、迁移、健康门禁和应用回滚的发布事务。
set -Eeuo pipefail

cd "$(dirname "$0")"
# shellcheck source=lib/common.sh
source "lib/common.sh"

# 输出命令帮助。
# 参数: 无。
# 返回: 始终返回 0。
usage() {
  cat <<'USAGE'
用法: ./deploy.sh [all|backend|frontend] --revision <commit-ish>

说明:
  - revision 会解析为完整 SHA，并且必须等于当前干净工作区 HEAD；脚本不 pull/reset。
  - backend/all 发布前自动备份，随后由目标 Backend 镜像执行 Alembic。
  - 健康或冒烟失败时尝试切回 previous.env 记录的应用镜像，不自动降级数据库。
USAGE
}

target="all"
revision="HEAD"
target_seen=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    all|backend|frontend)
      [[ "$target_seen" == "0" ]] || fatal "只能指定一个发布目标"
      target="$1"
      target_seen=1
      shift
      ;;
    --revision)
      [[ $# -ge 2 ]] || fatal "--revision 缺少值"
      revision="$2"
      shift 2
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

require_commands docker git curl awk grep
validate_compose_environment
repo_dir="$(cd .. && pwd)"
git -C "$repo_dir" rev-parse --git-dir >/dev/null 2>&1 || fatal "上级目录不是 Git 仓库"
assert_deploy_sources_clean "$repo_dir" || fatal "拒绝从脏构建上下文发布"
target_sha="$(resolve_git_revision "$repo_dir" "$revision")" || fatal "无法解析 revision: $revision"
head_sha="$(current_git_sha "$repo_dir")"
[[ "$target_sha" == "$head_sha" ]] || fatal "revision=$target_sha 与当前 HEAD=$head_sha 不一致；请先安全 checkout 精确提交"
[[ "$target_sha" =~ ^[0-9a-f]{40}$ ]] || fatal "目标 SHA 格式非法"

release_dir="${RELEASE_STATE_DIR:-.releases}"
current_state="$release_dir/current.env"
previous_state="$release_dir/previous.env"
pending_state="$release_dir/pending.env"
mkdir -p "$release_dir"
chmod 700 "$release_dir"
lock_dir="$(maintenance_lock_path)"
mkdir -p "$(dirname "$lock_dir")"
acquire_directory_lock "$lock_dir"
trap 'release_directory_lock "$lock_dir"' EXIT

rollback_ready=0

# 发布异常时尝试切回上一应用镜像，并保留原始失败状态码。
# 参数: ERR trap 自动传入失败状态。
# 返回: 以原始失败状态退出。
on_deploy_error() {
  local rc=$?
  trap - ERR
  log_warn "发布事务失败(rc=$rc, target=$target, sha=$target_sha)"
  if [[ "$rollback_ready" == "1" && -f "$previous_state" ]]; then
    log_warn "开始应用层自动回滚；数据库不会自动 downgrade/restore"
    if ! ./rollback.sh "$target" --confirm ROLLBACK_APPLICATION --from-deploy-failure; then
      log_warn "应用自动回滚失败，请保持维护窗口并人工检查 current/previous/pending 状态"
    fi
  else
    log_warn "尚无可验证的上一镜像，未执行自动回滚"
  fi
  exit "$rc"
}
trap on_deploy_error ERR

bootstrap_tag="rollback-$(date -u '+%Y%m%d%H%M%S')"
if [[ -f "$current_state" ]]; then
  cp "$current_state" "$previous_state"
  current_sha="$(read_release_value "$current_state" RELEASE_SHA)"
  current_backend="$(read_release_value "$current_state" BACKEND_RELEASE)"
  current_frontend="$(read_release_value "$current_state" FRONTEND_RELEASE)"
else
  current_sha="$head_sha"
  current_backend="$(capture_running_image backend prism-backend "$bootstrap_tag" || printf 'local\n')"
  current_frontend="$(capture_running_image frontend prism-frontend "$bootstrap_tag" || printf 'local\n')"
  write_release_state \
    "$previous_state" "$current_sha" "$current_backend" "$current_frontend" \
    bootstrap none "$(current_alembic_revision)"
fi

case "$target" in
  all)
    desired_backend="$target_sha"
    desired_frontend="$target_sha"
    ;;
  backend)
    desired_backend="$target_sha"
    desired_frontend="$current_frontend"
    ;;
  frontend)
    desired_backend="$current_backend"
    desired_frontend="$target_sha"
    ;;
esac

case "$target" in
  all)
    validate_geolite_database
    if release_image_exists prism-backend "$current_backend" \
      && release_image_exists prism-frontend "$current_frontend"; then
      rollback_ready=1
    fi
    ;;
  backend)
    validate_geolite_database
    release_image_exists prism-backend "$current_backend" && rollback_ready=1 || true
    ;;
  frontend)
    release_image_exists prism-frontend "$current_frontend" && rollback_ready=1 || true
    ;;
esac

export APP_RELEASE="$target_sha"
export BACKEND_RELEASE="$desired_backend"
export FRONTEND_RELEASE="$desired_frontend"
write_release_state \
  "$pending_state" "$target_sha" "$desired_backend" "$desired_frontend" \
  "$target" none "$(current_alembic_revision)"
validate_compose_environment
log_info "发布预检通过(target=$target, sha=$target_sha)"

backup_file="none"
if [[ "$target" == "all" || "$target" == "backend" ]]; then
  compose up -d mysql clamav
  wait_for_service_health mysql "${MYSQL_HEALTH_TIMEOUT:-180}" || fatal "MySQL 未就绪"
  wait_for_service_health clamav "${CLAMAV_HEALTH_TIMEOUT:-420}" || fatal "ClamAV 未就绪"
  backup_file="$(PRISM_MAINTENANCE_LOCK_HELD=1 ./backup.sh --reason pre_deploy | tail -n 1)"
  [[ -f "$backup_file" ]] || fatal "发布前备份未生成"
  PRISM_MAINTENANCE_LOCK_HELD=1 ./verify-backup.sh "$backup_file"
  log_info "发布前备份已完成"
  compose build backend
  run_admin_alembic upgrade head
  assert_alembic_at_head || fatal "Alembic 未位于唯一 head"
  # GeoLite2 以只读 bind 挂载进容器，而后端以非 root(prism, uid 10001)运行；
  # 宿主机文件若属主 501 且权限 640，容器内将 Permission denied，导致
  # /overview/geo 登录来源地图全部定位失败返回空。此处强制放开为 644 防复发。
  geolite_host="${GEOLITE_DB_HOST_PATH:-/opt/code-review/backend/GeoLite2-City.mmdb}"
  if [[ -f "$geolite_host" ]]; then
    chmod 644 "$geolite_host" 2>/dev/null || log_warn "无法调整 GeoLite2 权限: $geolite_host"
  fi
  compose up -d --no-deps backend
  wait_for_service_health backend "${BACKEND_HEALTH_TIMEOUT:-240}" || fatal "Backend 未恢复健康"
  smoke_backend "$target_sha" || fatal "Backend 冒烟失败"
fi

if [[ "$target" == "frontend" ]]; then
  wait_for_service_health backend "${BACKEND_HEALTH_TIMEOUT:-180}" || fatal "现有 Backend 不健康"
fi
if [[ "$target" == "all" || "$target" == "frontend" ]]; then
  compose build frontend
  compose up -d --no-deps frontend
  wait_for_service_health frontend "${FRONTEND_HEALTH_TIMEOUT:-120}" || fatal "Frontend 未恢复健康"
fi

smoke_https "$desired_backend" || fatal "HTTPS/同源冒烟失败"
alembic_revision="$(current_alembic_revision)"
write_release_state \
  "$current_state" "$target_sha" "$desired_backend" "$desired_frontend" \
  "$target" "$backup_file" "$alembic_revision"
rm -f "$pending_state"
trap - ERR
log_info "发布完成(target=$target, sha=$target_sha, alembic=$alembic_revision)"
compose ps
