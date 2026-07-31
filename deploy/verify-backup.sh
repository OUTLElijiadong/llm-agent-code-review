#!/usr/bin/env bash
# 将 SQL 备份恢复到隔离临时数据库并验证，绝不覆盖生产数据库。
set -Eeuo pipefail

cd "$(dirname "$0")"
# shellcheck source=lib/common.sh
source "lib/common.sh"

# 输出命令帮助。
# 参数: 无。
# 返回: 始终返回 0。
usage() {
  cat <<'USAGE'
用法: ./verify-backup.sh [BACKUP.sql.gz]

未指定文件时自动选择 BACKUP_DIR（默认 ../backups）中的最新备份。
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
[[ $# -le 1 ]] || fatal "参数过多"

backup_dir="${BACKUP_DIR:-../backups}"
backup_file="${1:-}"
if [[ -z "$backup_file" ]]; then
  backup_file="$(latest_file_by_mtime "$backup_dir" 'code_review_*.sql.gz' || true)"
fi
[[ -n "$backup_file" && -f "$backup_file" ]] || fatal "找不到待验证备份"

require_commands docker gzip awk date find stat
validate_compose_environment
wait_for_service_health mysql "${MYSQL_HEALTH_TIMEOUT:-120}" || fatal "MySQL 未就绪"
gzip -t "$backup_file" || fatal "备份 gzip 完整性校验失败"

checksum_file="$backup_file.sha256"
if [[ -f "$checksum_file" ]]; then
  expected="$(awk 'NR==1 {print $1}' "$checksum_file")"
  actual="$(sha256_file "$backup_file")"
  [[ "$expected" == "$actual" ]] || fatal "备份 SHA-256 不匹配"
else
  log_warn "未找到校验和文件: $checksum_file"
fi

temp_database="prism_verify_$(date -u '+%Y%m%d%H%M%S')_${RANDOM}"
[[ "$temp_database" =~ ^[A-Za-z0-9_]+$ ]] || fatal "临时数据库名非法"
created=0

# 删除隔离验证数据库。
# 参数: 无。
# 返回: 始终返回 0。
cleanup() {
  if [[ "$created" == "1" ]]; then
    compose exec -T mysql sh -ec '
      db="$1"
      [[ "$db" =~ ^[A-Za-z0-9_]+$ ]]
      MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
        --protocol=TCP -h 127.0.0.1 -uroot \
        -e "DROP DATABASE IF EXISTS \`$db\`"
    ' sh "$temp_database" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

log_info "创建隔离恢复库: $temp_database"
compose exec -T mysql sh -ec '
  db="$1"
  [[ "$db" =~ ^[A-Za-z0-9_]+$ ]]
  MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
    --protocol=TCP -h 127.0.0.1 -uroot \
    -e "CREATE DATABASE \`$db\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
' sh "$temp_database"
created=1

gzip -dc "$backup_file" | compose exec -T mysql sh -ec '
  db="$1"
  [[ "$db" =~ ^[A-Za-z0-9_]+$ ]]
  MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
    --protocol=TCP -h 127.0.0.1 -uroot "$db"
' sh "$temp_database"

table_count="$(compose exec -T mysql sh -ec '
  db="$1"
  [[ "$db" =~ ^[A-Za-z0-9_]+$ ]]
  MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
    --protocol=TCP -h 127.0.0.1 -uroot "$db" \
    --batch --skip-column-names \
    -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=\"$db\""
' sh "$temp_database" | tr -d '\r' | tail -n 1)"
minimum_tables="${VERIFY_MIN_TABLES:-20}"
[[ "$table_count" =~ ^[0-9]+$ ]] || fatal "恢复后的表数量不是整数"
(( table_count >= minimum_tables )) || fatal "恢复后仅有 $table_count 张表，低于下限 $minimum_tables"

alembic_revision="$(compose exec -T mysql sh -ec '
  db="$1"
  [[ "$db" =~ ^[A-Za-z0-9_]+$ ]]
  MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
    --protocol=TCP -h 127.0.0.1 -uroot "$db" \
    --batch --skip-column-names \
    -e "SELECT version_num FROM alembic_version LIMIT 1"
' sh "$temp_database" 2>/dev/null | tr -d '\r' | tail -n 1 || true)"

log_info "隔离恢复验证通过(tables=$table_count, alembic=${alembic_revision:-unknown})"
