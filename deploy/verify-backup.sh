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
lock_dir="$(maintenance_lock_path)"
lock_owned=0
if [[ "${PRISM_MAINTENANCE_LOCK_HELD:-0}" != "1" ]]; then
  mkdir -p "$(dirname "$lock_dir")"
  acquire_directory_lock "$lock_dir"
  lock_owned=1
else
  [[ -d "$lock_dir" ]] || fatal "声明已持有维护锁，但锁目录不存在: $lock_dir"
fi
temp_database=""
created=0

# 删除隔离验证库并释放本脚本持有的维护锁。
# 参数: 无。
# 返回: 始终返回 0。
cleanup() {
  if [[ "$created" == "1" && -n "$temp_database" ]]; then
    compose exec -T mysql sh -ec '
      db="$1"
      [[ "$db" =~ ^[A-Za-z0-9_]+$ ]]
      MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
        --protocol=TCP -h 127.0.0.1 -uroot \
        -e "DROP DATABASE IF EXISTS \`$db\`"
    ' sh "$temp_database" >/dev/null 2>&1 || true
  fi
  [[ "$lock_owned" != "1" ]] || release_directory_lock "$lock_dir"
}
trap cleanup EXIT

wait_for_service_health mysql "${MYSQL_HEALTH_TIMEOUT:-120}" || fatal "MySQL 未就绪"
gzip -t "$backup_file" || fatal "备份 gzip 完整性校验失败"

checksum_file="$backup_file.sha256"
metadata_file="$backup_file.meta"
[[ -f "$checksum_file" ]] || fatal "备份缺少校验和文件: $checksum_file"
[[ -f "$metadata_file" ]] || fatal "备份缺少元数据文件: $metadata_file"
expected="$(awk 'NR==1 {print $1}' "$checksum_file")"
actual="$(sha256_file "$backup_file")"
[[ "$expected" =~ ^[0-9a-f]{64}$ && "$expected" == "$actual" ]] || fatal "备份 SHA-256 不匹配"
[[ "$(read_env_value format_version "$metadata_file" || true)" == "2" ]] || fatal "备份元数据版本不受支持"
[[ "$(read_env_value sha256 "$metadata_file" || true)" == "$actual" ]] || fatal "备份元数据 SHA-256 不匹配"
[[ "$(read_env_value file "$metadata_file" || true)" == "$(basename "$backup_file")" ]] \
  || fatal "备份元数据文件名不匹配"
expected_revision="$(read_env_value alembic_revision "$metadata_file" || true)"
expected_table_count="$(read_env_value table_count "$metadata_file" || true)"
expected_archive_rows="$(read_env_value archive_row_count "$metadata_file" || true)"
expected_archive_bytes="$(read_env_value archive_blob_bytes "$metadata_file" || true)"
[[ -n "$expected_revision" && "$expected_revision" != "unknown" ]] || fatal "备份元数据缺少 Alembic revision"
[[ "$expected_table_count" =~ ^[0-9]+$ ]] || fatal "备份元数据表数无效"
[[ "$expected_archive_rows" =~ ^[0-9]+$ ]] || fatal "备份元数据隔离归档行数无效"
[[ "$expected_archive_bytes" =~ ^[0-9]+$ ]] || fatal "备份元数据隔离归档字节数无效"

temp_database="prism_verify_$(date -u '+%Y%m%d%H%M%S')_${RANDOM}"
[[ "$temp_database" =~ ^[A-Za-z0-9_]+$ ]] || fatal "临时数据库名非法"

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
    --protocol=TCP -h 127.0.0.1 -uroot --max-allowed-packet=64M "$db"
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
[[ "$table_count" == "$expected_table_count" ]] \
  || fatal "恢复库表数与备份元数据不一致"

archive_table_exists="$(compose exec -T mysql sh -ec '
  db="$1"
  [[ "$db" =~ ^[A-Za-z0-9_]+$ ]]
  MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
    --protocol=TCP -h 127.0.0.1 -uroot --max-allowed-packet=64M "$db" \
    --batch --skip-column-names \
    -e "SELECT COUNT(*) FROM information_schema.tables
        WHERE table_schema=DATABASE() AND table_name=\"project_source_archive\""
' sh "$temp_database" | tr -d '\r' | tail -n 1)"
[[ "$archive_table_exists" == "0" || "$archive_table_exists" == "1" ]] \
  || fatal "恢复库隔离归档表状态无效"
archive_integrity=0
archive_rows=0
archive_bytes=0
if [[ "$archive_table_exists" == "1" ]]; then
  archive_integrity="$(compose exec -T mysql sh -ec '
    db="$1"
    [[ "$db" =~ ^[A-Za-z0-9_]+$ ]]
    MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
      --protocol=TCP -h 127.0.0.1 -uroot --max-allowed-packet=64M "$db" \
      --batch --skip-column-names \
      -e "SELECT COUNT(*) FROM project_source_archive
          WHERE LOWER(SHA2(archive_blob, 256)) <> LOWER(archive_sha256)
             OR OCTET_LENGTH(archive_blob) <> compressed_size"
  ' sh "$temp_database" | tr -d '\r' | tail -n 1)"
  archive_stats="$(compose exec -T mysql sh -ec '
    db="$1"
    [[ "$db" =~ ^[A-Za-z0-9_]+$ ]]
    MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
      --protocol=TCP -h 127.0.0.1 -uroot --max-allowed-packet=64M "$db" \
      --batch --skip-column-names \
      -e "SELECT COUNT(*), COALESCE(SUM(OCTET_LENGTH(archive_blob)),0)
          FROM project_source_archive"
  ' sh "$temp_database" | tr -d '\r' | tail -n 1)"
  IFS=$'\t' read -r archive_rows archive_bytes <<< "$archive_stats"
fi
[[ "$archive_integrity" == "0" ]] || fatal "隔离源码归档在恢复库中的字节数或 SHA-256 不一致"
[[ "$archive_rows" == "$expected_archive_rows" ]] || fatal "恢复库隔离归档行数不一致"
[[ "$archive_bytes" == "$expected_archive_bytes" ]] || fatal "恢复库隔离归档字节数不一致"

alembic_revision="$(compose exec -T mysql sh -ec '
  db="$1"
  [[ "$db" =~ ^[A-Za-z0-9_]+$ ]]
  MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
    --protocol=TCP -h 127.0.0.1 -uroot "$db" \
    --batch --skip-column-names \
    -e "SELECT version_num FROM alembic_version LIMIT 1"
' sh "$temp_database" 2>/dev/null | tr -d '\r' | tail -n 1 || true)"
[[ "$alembic_revision" == "$expected_revision" ]] \
  || fatal "恢复库 Alembic revision 与备份元数据不一致"
if [[ "$alembic_revision" =~ ^[0-9]+$ ]] && (( 10#$alembic_revision >= 24 )); then
  [[ "$archive_table_exists" == "1" ]] || fatal "024 及以上备份缺少隔离源码归档表"
fi

log_info "隔离恢复验证通过(tables=$table_count, alembic=${alembic_revision:-unknown})"
