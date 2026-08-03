#!/usr/bin/env bash
# 显式确认后恢复生产数据库；默认先创建安全备份并验证目标备份。
set -Eeuo pipefail

cd "$(dirname "$0")"
# shellcheck source=lib/common.sh
source "lib/common.sh"

# 输出命令帮助。
# 参数: 无。
# 返回: 始终返回 0。
usage() {
  cat <<'USAGE'
用法: ./restore.sh BACKUP.sql.gz --confirm RESTORE_PRODUCTION [--skip-safety-backup]

警告: 该命令会重建应用数据库。没有精确确认口令时不会执行。
USAGE
}

backup_file=""
confirmation=""
skip_safety_backup=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --confirm)
      [[ $# -ge 2 ]] || fatal "--confirm 缺少值"
      confirmation="$2"
      shift 2
      ;;
    --skip-safety-backup)
      skip_safety_backup=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -* )
      fatal "未知参数: $1"
      ;;
    *)
      [[ -z "$backup_file" ]] || fatal "只能指定一个备份文件"
      backup_file="$1"
      shift
      ;;
  esac
done

[[ -n "$backup_file" && -f "$backup_file" ]] || fatal "必须指定存在的 .sql.gz 备份"
[[ "$confirmation" == "RESTORE_PRODUCTION" ]] || fatal "确认口令不匹配，恢复已取消"
require_commands docker gzip
validate_compose_environment

lock_dir="$(maintenance_lock_path)"
safety_backup=""
backend_stopped=0
restore_started=0
restore_completed=0
database_name=""

# 重建生产库并导入指定备份。
# 参数: $1 为已验证的 .sql.gz 备份。
# 返回: 导入成功时返回 0。
restore_database_file() {
  local source_file="$1"
  compose exec -T mysql sh -ec '
    db="$1"
    [[ "$db" =~ ^[A-Za-z0-9_]+$ ]]
    MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
      --protocol=TCP -h 127.0.0.1 -uroot \
      -e "DROP DATABASE IF EXISTS \`$db\`; CREATE DATABASE \`$db\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
  ' sh "$database_name" || return $?
  gzip -dc "$source_file" | compose exec -T mysql sh -ec '
    MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
      --protocol=TCP -h 127.0.0.1 -uroot --max-allowed-packet=64M "$MYSQL_DATABASE"
  ' || return $?
}

# 任一恢复阶段失败时，用事前已验证备份自动回填生产库。
# 参数: EXIT trap 传入的原始退出码。
# 返回: 保留原始退出码。
on_restore_exit() {
  local rc=$?
  trap - EXIT
  if [[ "$rc" != "0" && "$restore_started" == "1" && "$restore_completed" != "1" ]]; then
    set +e
    if [[ -n "$safety_backup" && -f "$safety_backup" ]]; then
      log_warn "恢复事务失败，正在回填事前安全备份"
      if ! compose stop backend; then
        log_warn "无法确认 Backend 已停止，取消自动回填并保持维护状态"
      elif restore_database_file "$safety_backup"; then
        compose up -d --no-deps backend
        if wait_for_service_health backend "${BACKEND_HEALTH_TIMEOUT:-180}"; then
          log_warn "已回填事前数据并恢复 Backend"
        else
          log_warn "事前数据已回填，但 Backend 未恢复健康"
        fi
      else
        log_warn "事前安全备份回填失败，生产保持维护状态"
      fi
    else
      log_warn "未生成事前安全备份，无法自动回填数据"
    fi
  elif [[ "$rc" != "0" && "$backend_stopped" == "1" ]]; then
    set +e
    log_warn "数据库尚未重建，正在恢复 Backend"
    compose up -d --no-deps backend
    wait_for_service_health backend "${BACKEND_HEALTH_TIMEOUT:-180}" \
      || log_warn "Backend 未恢复健康"
  fi
  release_directory_lock "$lock_dir"
  exit "$rc"
}

mkdir -p "$(dirname "$lock_dir")"
acquire_directory_lock "$lock_dir"
trap on_restore_exit EXIT

database_name="$(compose exec -T mysql sh -ec 'printf "%s" "$MYSQL_DATABASE"')"
[[ "$database_name" =~ ^[A-Za-z0-9_]+$ ]] || fatal "MYSQL_DATABASE 名称不安全"

PRISM_MAINTENANCE_LOCK_HELD=1 ./verify-backup.sh "$backup_file"

log_warn "目标备份已验证；即将停止 Backend 并冻结生产写入"
compose stop backend
backend_stopped=1
if [[ "$skip_safety_backup" != "1" ]]; then
  safety_backup="$(PRISM_MAINTENANCE_LOCK_HELD=1 ./backup.sh --reason pre_restore | tail -n 1)"
  [[ -f "$safety_backup" ]] || fatal "事前安全备份未生成"
  PRISM_MAINTENANCE_LOCK_HELD=1 ./verify-backup.sh "$safety_backup"
fi

log_warn "停写后的安全备份已完成；即将重建生产应用数据库"
restore_started=1
restore_database_file "$backup_file"

run_admin_alembic upgrade head
assert_alembic_at_head || fatal "恢复后 Alembic 未位于 head，Backend 保持停止"
compose up -d --no-deps backend
wait_for_service_health backend "${BACKEND_HEALTH_TIMEOUT:-180}" || fatal "恢复后 Backend 未恢复健康"
restore_completed=1
log_info "生产数据库恢复完成并通过健康检查"
