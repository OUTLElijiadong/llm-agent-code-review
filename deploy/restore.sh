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
mkdir -p "$(dirname "$lock_dir")"
acquire_directory_lock "$lock_dir"
export PRISM_MAINTENANCE_LOCK_HELD=1
safety_backup=""
# 失败处置时由人工确认后执行：restore_database_file "$safety_backup"
on_restore_exit() {
  exit_code="$?"
  release_directory_lock "$lock_dir"
  if [[ "$exit_code" != "0" ]]; then
    log_warn "生产保持维护状态，需人工确认恢复结果"
  fi
  return "$exit_code"
}
trap on_restore_exit EXIT

./verify-backup.sh "$backup_file"

if [[ "$skip_safety_backup" != "1" ]]; then
  safety_backup="$(./backup.sh --reason pre_restore | tail -n 1)"
  ./verify-backup.sh "$safety_backup"
fi

restore_database_file() {
  local source_file="$1"
  gzip -dc "$source_file" | compose exec -T mysql sh -ec '
    MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
      --protocol=TCP -h 127.0.0.1 -uroot \
      --max-allowed-packet=64M "$MYSQL_DATABASE"
  '
}

log_warn "即将停止 Backend 并重建生产应用数据库"
compose stop backend

database_name="$(compose exec -T mysql sh -ec 'printf "%s" "$MYSQL_DATABASE"')"
[[ "$database_name" =~ ^[A-Za-z0-9_]+$ ]] || fatal "MYSQL_DATABASE 名称不安全"
compose exec -T mysql sh -ec '
  db="$1"
  [[ "$db" =~ ^[A-Za-z0-9_]+$ ]]
  MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
    --protocol=TCP -h 127.0.0.1 -uroot \
    -e "DROP DATABASE IF EXISTS \`$db\`; CREATE DATABASE \`$db\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
' sh "$database_name"

restore_rc=0
if restore_database_file "$backup_file"; then
  restore_rc=0
else
  restore_rc=$?
fi
if [[ "$restore_rc" != "0" ]]; then
  if [[ -n "$safety_backup" && -f "$safety_backup" ]]; then
    log_warn "恢复事务失败，正在回填事前安全备份"
    compose stop backend
    restore_database_file "$safety_backup"
    run_admin_alembic upgrade head
    compose up -d --no-deps backend
    wait_for_service_health backend "${BACKEND_HEALTH_TIMEOUT:-180}" || fatal "安全备份回填后 Backend 未恢复健康"
    log_warn "已回填事前数据并恢复 Backend"
  else
    fatal "恢复事务失败且未创建事前安全备份"
  fi
  exit "$restore_rc"
fi

run_admin_alembic upgrade head
assert_alembic_at_head || fatal "恢复后 Alembic 未位于 head，Backend 保持停止"
compose up -d --no-deps backend
wait_for_service_health backend "${BACKEND_HEALTH_TIMEOUT:-180}" || fatal "恢复后 Backend 未恢复健康"
log_info "生产数据库恢复完成并通过健康检查"
