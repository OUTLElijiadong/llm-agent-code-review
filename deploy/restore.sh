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

./verify-backup.sh "$backup_file"
if [[ "$skip_safety_backup" != "1" ]]; then
  ./backup.sh --reason pre_restore >/dev/null
fi

lock_dir="${BACKUP_DIR:-../backups}/.restore.lock"
mkdir -p "$(dirname "$lock_dir")"
acquire_directory_lock "$lock_dir"
trap 'release_directory_lock "$lock_dir"' EXIT

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

gzip -dc "$backup_file" | compose exec -T mysql sh -ec '
  MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
    --protocol=TCP -h 127.0.0.1 -uroot "$MYSQL_DATABASE"
'

run_admin_alembic upgrade head
assert_alembic_at_head || fatal "恢复后 Alembic 未位于 head，Backend 保持停止"
compose up -d --no-deps backend
wait_for_service_health backend "${BACKEND_HEALTH_TIMEOUT:-180}" || fatal "恢复后 Backend 未恢复健康"
log_info "生产数据库恢复完成并通过健康检查"
