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
用法: ./deploy.sh [all] --revision <commit-ish>

说明:
  - revision 会解析为完整 SHA，并且必须等于当前干净工作区 HEAD；脚本不 pull/reset。
  - 当前仅支持 all，前后端必须使用同一提交发布。
  - 发布前自动备份并验证恢复，随后由目标 Backend 镜像执行 Alembic。
  - 前端切换后自动同步 assets 卷并检查同源 HTTPS。
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
version_file="$repo_dir/VERSION"
[[ -f "$version_file" ]] || fatal "缺少根目录 VERSION，拒绝发布无法识别的版本"
app_version="$(tr -d '[:space:]' < "$version_file")"
[[ "$app_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fatal "VERSION 必须是 x.y.z 语义版本"
target_sha="$(resolve_git_revision "$repo_dir" "$revision")" || fatal "无法解析 revision: $revision"
head_sha="$(current_git_sha "$repo_dir")"
[[ "$target_sha" == "$head_sha" ]] || fatal "revision=$target_sha 与当前 HEAD=$head_sha 不一致；请先安全 checkout 精确提交"
[[ "$target_sha" =~ ^[0-9a-f]{40}$ ]] || fatal "目标 SHA 格式非法"
[[ "$target" == "all" ]] || fatal "当前生产版本必须使用 all 发布，禁止单独发布 backend/frontend 造成版本漂移"

release_dir="${RELEASE_STATE_DIR:-.releases}"
current_state="$release_dir/current.env"
previous_state="$release_dir/previous.env"
pending_state="$release_dir/pending.env"
mkdir -p "$release_dir"
chmod 700 "$release_dir"
lock_dir="$(maintenance_lock_path)"
mkdir -p "$(dirname "$lock_dir")"
acquire_directory_lock "$lock_dir"
rollback_ready=0
deployment_mutated=0
migration_attempted=0
failure_handled=0
lock_released=0
deploy_stage="preflight"

# 故障处理与 EXIT 可到达同一路径，只释放本事务持有的锁一次。
release_deploy_lock() {
  if [[ "$lock_released" == "0" ]]; then
    lock_released=1
    release_directory_lock "$lock_dir"
  fi
}

# EXIT 也覆盖显式 fatal/exit；ERR trap 本身无法捕获这些退出。
on_deploy_exit() {
  local rc=$?
  if [[ "$rc" != "0" && "$failure_handled" == "0" ]]; then
    finish_deploy_failure "$rc" "发布阶段异常退出"
  fi
  release_deploy_lock
}
trap on_deploy_exit EXIT

# 发布异常时尝试切回上一应用镜像，并保留原始失败状态码。
# 参数: $1 原始错误码；$2 失败摘要。
# 返回: 以原始失败状态退出。
finish_deploy_failure() {
  local rc="$1"
  local reason="$2"
  trap - ERR
  # -E 会把 ERR 传播进 $(...)。子 Shell 只把错误码交回主事务，
  # 不得提前释放共用维护锁或重复执行回滚。
  if (( BASH_SUBSHELL > 0 )); then
    exit "$rc"
  fi
  failure_handled=1
  log_warn "发布事务失败(rc=$rc, stage=$deploy_stage, target=$target, sha=$target_sha): $reason"
  if [[ "$migration_attempted" == "1" ]]; then
    log_warn "数据库迁移已尝试，当前结构需核验；应用回滚不代表数据库已还原。备份: ${backup_file:-none}"
  fi
  if [[ "$deployment_mutated" == "1" && "$rollback_ready" == "1" && -f "$previous_state" ]]; then
    log_warn "开始应用层自动回滚；数据库不会自动 downgrade/restore"
    if ! ./rollback.sh "$target" --confirm ROLLBACK_APPLICATION --from-deploy-failure; then
      log_warn "应用自动回滚失败，请保持维护窗口并人工检查 current/previous/pending 状态"
    else
      log_info "应用自动回滚完成；请核对数据库兼容性与公网冒烟结果"
    fi
  elif [[ "$deployment_mutated" != "1" ]]; then
    log_warn "应用尚未切换，跳过回滚；pending 状态保留供人工核对"
  else
    log_warn "尚无可验证的上一镜像，未执行自动回滚"
  fi
  release_deploy_lock
  exit "$rc"
}

# 显式处理 `command || fatal` 场景。ERR trap 不会覆盖 OR 列表右侧的
# fatal，因此所有应用切换后的显式失败都必须从这里进入回滚事务。
deploy_fatal() {
  local rc=$?
  [[ "$rc" != "0" ]] || rc=1
  finish_deploy_failure "$rc" "$*"
}

# 参数: ERR trap 自动传入失败状态。
on_deploy_error() {
  local rc=$?
  finish_deploy_failure "$rc" "未捕获命令失败"
}
trap on_deploy_error ERR

if [[ -f "$current_state" ]]; then
  load_release_environment "$current_state"
  assert_running_release_environment
  current_sha="$APP_RELEASE"
  current_backend="$BACKEND_RELEASE"
  current_frontend="$FRONTEND_RELEASE"
  write_bound_release_state "$previous_state" "$current_state"
else
  backend_container="$(service_container_id backend || true)"
  frontend_container="$(service_container_id frontend || true)"
  current_backend=""
  current_frontend=""
  if [[ -n "$backend_container" || -n "$frontend_container" ]]; then
    [[ -n "$backend_container" && -n "$frontend_container" ]] || fatal "缺少发布账本且运行服务不完整，拒绝猜测上一版本"
    backend_image="$(docker inspect --format '{{.Config.Image}}' "$backend_container")"
    frontend_image="$(docker inspect --format '{{.Config.Image}}' "$frontend_container")"
    [[ "$backend_image" == prism-backend:* && "$frontend_image" == prism-frontend:* ]] || fatal "运行镜像缺少可验证发布标签"
    current_backend="${backend_image#prism-backend:}"
    current_frontend="${frontend_image#prism-frontend:}"
    current_sha="$current_backend"
    current_version="$(resolve_release_version "$current_sha" "$current_backend" "$current_frontend")"
    export APP_RELEASE="$current_sha" APP_VERSION="$current_version"
    export BACKEND_RELEASE="$current_backend" FRONTEND_RELEASE="$current_frontend"
    BOUND_BACKEND_IMAGE_ID="$(release_image_id "$backend_image")"
    BOUND_FRONTEND_IMAGE_ID="$(release_image_id "$frontend_image")"
    export BOUND_BACKEND_IMAGE_ID BOUND_FRONTEND_IMAGE_ID
    assert_running_release_environment
    write_release_state \
      "$previous_state" "$current_sha" "$current_backend" "$current_frontend" \
      bootstrap none "$(current_alembic_revision)" "$current_version"
  fi
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
export APP_VERSION="$app_version"
export BACKEND_RELEASE="$desired_backend"
export FRONTEND_RELEASE="$desired_frontend"
write_release_state \
  "$pending_state" "$target_sha" "$desired_backend" "$desired_frontend" \
  "$target" none "$(current_alembic_revision)" "$app_version"
validate_compose_environment
assert_compose_release_environment
log_info "发布预检通过(target=$target, version=$APP_VERSION, sha=$target_sha)"

backup_file="none"
if [[ "$target" == "all" || "$target" == "backend" ]]; then
  deploy_stage="dependencies"
  compose up -d mysql clamav
  wait_for_service_health mysql "${MYSQL_HEALTH_TIMEOUT:-180}" || fatal "MySQL 未就绪"
  wait_for_service_health clamav "${CLAMAV_HEALTH_TIMEOUT:-420}" || fatal "ClamAV 未就绪"
  deploy_stage="backup"
  backup_file="$(PRISM_MAINTENANCE_LOCK_HELD=1 ./backup.sh --reason pre_deploy | tail -n 1)"
  [[ -f "$backup_file" ]] || fatal "发布前备份未生成"
  deploy_stage="backup_verify"
  PRISM_MAINTENANCE_LOCK_HELD=1 ./verify-backup.sh "$backup_file"
  log_info "发布前备份已完成"
  deploy_stage="backend_build"
  compose build backend
  deploy_stage="migration_preflight"
  prepare_admin_alembic
  deploy_stage="migration"
  migration_attempted=1
  run_admin_alembic upgrade head
  deploy_stage="migration_verify"
  assert_alembic_at_head || fatal "Alembic 未位于唯一 head"
  # GeoLite2 以只读 bind 挂载进容器，而后端以非 root(prism, uid 10001)运行；
  # 宿主机文件若属主 501 且权限 640，容器内将 Permission denied，导致
  # /overview/geo 登录来源地图全部定位失败返回空。此处强制放开为 644 防复发。
  geolite_host="${GEOLITE_DB_HOST_PATH:-/opt/code-review/backend/GeoLite2-City.mmdb}"
  if [[ -f "$geolite_host" ]]; then
    chmod 644 "$geolite_host" 2>/dev/null || log_warn "无法调整 GeoLite2 权限: $geolite_host"
  fi
  deployment_mutated=1
  deploy_stage="backend_switch"
  compose up -d --no-deps --no-build --pull never backend
  deploy_stage="backend_health"
  wait_for_service_health backend "${BACKEND_HEALTH_TIMEOUT:-240}" || deploy_fatal "Backend 未恢复健康"
  deploy_stage="backend_smoke"
  smoke_backend "$target_sha" || deploy_fatal "Backend 冒烟失败"
fi

if [[ "$target" == "frontend" ]]; then
  wait_for_service_health backend "${BACKEND_HEALTH_TIMEOUT:-180}" || fatal "现有 Backend 不健康"
fi
if [[ "$target" == "all" || "$target" == "frontend" ]]; then
  deploy_stage="frontend_build"
  compose build frontend
  deployment_mutated=1
  deploy_stage="frontend_switch"
  compose up -d --no-deps --no-build --pull never frontend
  # assets 是命名卷挂载，必须把新镜像 dist 同步进卷，否则 index.html 引用的
  # 新哈希文件 404 导致页面空白。
  deploy_stage="frontend_assets"
  ./sync-frontend-assets.sh "$desired_frontend" || deploy_fatal "前端 assets 卷同步失败"
  deploy_stage="frontend_health"
  wait_for_service_health frontend "${FRONTEND_HEALTH_TIMEOUT:-120}" || deploy_fatal "Frontend 未恢复健康"
fi

deploy_stage="https_smoke"
smoke_https "$desired_backend" || deploy_fatal "HTTPS/同源冒烟失败"
deploy_stage="release_ledger"
alembic_revision="$(current_alembic_revision)"
write_release_state \
  "$current_state" "$target_sha" "$desired_backend" "$desired_frontend" \
  "$target" "$backup_file" "$alembic_revision" "$app_version"
rm -f "$pending_state"
# 提交发布账本后仅剩信息展示，不能因 compose ps 失败撤销已验收版本。
failure_handled=1
trap - ERR
log_info "发布完成(target=$target, sha=$target_sha, alembic=$alembic_revision)"
if ! assert_compose_release_environment default; then
  log_warn "应用已按目标版本发布，但默认 Compose 环境仍漂移；校准默认配置前禁止直接重建"
fi
compose ps || log_warn "发布已完成，但容器列表读取失败；请重试只读运维检查"
