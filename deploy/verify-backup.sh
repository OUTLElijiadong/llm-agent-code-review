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
默认恢复到独立容器 cr_testdb；VERIFY_DB_CONTAINER 不得指向生产 MySQL。
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

require_commands docker gzip awk date find stat sleep
validate_compose_environment
lock_dir="$(maintenance_lock_path)"
mkdir -p "$(dirname "$lock_dir")"
lock_acquired=0
if [[ "${PRISM_MAINTENANCE_LOCK_HELD:-0}" != "1" ]]; then
  acquire_directory_lock "$lock_dir"
  lock_acquired=1
fi

# 在完整清理器安装前，任何边车校验失败也必须释放自己获取的锁。
release_verify_lock() {
  if [[ "$lock_acquired" == "1" ]]; then
    release_directory_lock "$lock_dir"
  fi
}
trap release_verify_lock EXIT

gzip -t "$backup_file" || fatal "备份 gzip 完整性校验失败"

checksum_file="$backup_file.sha256"
if [[ -f "$checksum_file" ]]; then
  expected="$(awk 'NR==1 {print $1}' "$checksum_file")"
  actual="$(sha256_file "$backup_file")"
  [[ "$expected" == "$actual" ]] || fatal "备份 SHA-256 不匹配"
else
  fatal "备份缺少校验和文件: $checksum_file"
fi

metadata_file="$backup_file.meta"
[[ -f "$metadata_file" ]] || fatal "备份缺少元数据文件: $metadata_file"

deploy_env_file="${DEPLOY_ENV_FILE:-.env}"
configured_verify_container="$(read_env_value VERIFY_DB_CONTAINER "$deploy_env_file" || true)"
configured_minimum_memory="$(read_env_value VERIFY_MIN_HOST_AVAILABLE_MB "$deploy_env_file" || true)"
configured_cleanup_timeout="$(read_env_value VERIFY_CLEANUP_TIMEOUT_SECONDS "$deploy_env_file" || true)"
verify_container="${VERIFY_DB_CONTAINER:-${configured_verify_container:-cr_testdb}}"
[[ "$verify_container" =~ ^[A-Za-z0-9_.-]+$ ]] || fatal "验证库容器名非法"
[[ "$verify_container" != "cr_mysql" && "$verify_container" != "mysql" ]] \
  || fatal "拒绝在生产 MySQL 容器中执行备份恢复验证"

production_mysql_id="$(compose ps -q mysql)"
[[ -n "$production_mysql_id" ]] || fatal "无法识别生产 MySQL 容器"
verify_container_id="$(docker inspect --format '{{.Id}}' "$verify_container" 2>/dev/null || true)"
[[ -n "$verify_container_id" ]] || fatal "独立验证库容器不存在: $verify_container"
[[ "$verify_container_id" != "$production_mysql_id" ]] \
  || fatal "拒绝在生产 MySQL 容器中执行备份恢复验证"
[[ "$(docker inspect --format '{{.State.Running}}' "$verify_container" 2>/dev/null || true)" == "true" ]] \
  || fatal "独立验证库容器未运行: $verify_container"

minimum_memory_mb="${VERIFY_MIN_HOST_AVAILABLE_MB:-${configured_minimum_memory:-1536}}"
[[ "$minimum_memory_mb" =~ ^[0-9]+$ ]] || fatal "VERIFY_MIN_HOST_AVAILABLE_MB 必须是非负整数"
if (( minimum_memory_mb > 0 )); then
  available_memory_kb="$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo 2>/dev/null || true)"
  [[ "$available_memory_kb" =~ ^[0-9]+$ ]] || fatal "无法读取主机可用内存"
  if (( available_memory_kb < minimum_memory_mb * 1024 )); then
    fatal "主机可用内存不足，拒绝启动全量恢复验证"
  fi
fi

container_memory_bytes="$(docker inspect --format '{{.HostConfig.Memory}}' "$verify_container" 2>/dev/null || true)"
[[ "$container_memory_bytes" =~ ^[0-9]+$ ]] || fatal "无法读取验证库容器内存上限"
if (( minimum_memory_mb > 0 && container_memory_bytes > 0 \
  && container_memory_bytes < minimum_memory_mb * 1024 * 1024 )); then
  fatal "独立验证库容器内存上限低于 ${minimum_memory_mb} MiB"
fi

temp_database="prism_verify_$(date -u '+%Y%m%d%H%M%S')_${RANDOM}"
[[ "$temp_database" =~ ^[A-Za-z0-9_]+$ ]] || fatal "临时数据库名非法"
created=0
cleanup_timeout="${VERIFY_CLEANUP_TIMEOUT_SECONDS:-${configured_cleanup_timeout:-90}}"
[[ "$cleanup_timeout" =~ ^[1-9][0-9]*$ ]] || fatal "VERIFY_CLEANUP_TIMEOUT_SECONDS 必须是正整数"

# 等待独立验证库可用。
# 参数: 无。
# 返回: 在清理等待时间内可连接返回 0，否则返回 1。
wait_for_verify_database() {
  local deadline=$((SECONDS + cleanup_timeout))
  while (( SECONDS < deadline )); do
    if docker exec "$verify_container" sh -ec '
      [[ -n "${MYSQL_ROOT_PASSWORD:-}" ]]
      MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
        --protocol=TCP -h 127.0.0.1 -uroot \
        --batch --skip-column-names -e "SELECT 1"
    ' >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  return 1
}

# 从独立验证库删除临时数据库，允许容器异常重启后重试。
# 参数: 无。
# 返回: 删除成功返回 0，超时返回 1。
drop_verify_database() {
  local deadline=$((SECONDS + cleanup_timeout))
  while (( SECONDS < deadline )); do
    if docker exec "$verify_container" sh -ec '
      db="$1"
      [[ "$db" =~ ^[A-Za-z0-9_]+$ ]]
      MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
        --protocol=TCP -h 127.0.0.1 -uroot \
        -e "DROP DATABASE IF EXISTS \`$db\`"
    ' sh "$temp_database" >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  return 1
}

# 删除隔离验证数据库。
# 参数: 无。
# 返回: 保留原始退出码；成功路径清理失败则返回 1。
cleanup() {
  local rc=$?
  if [[ "$created" == "1" ]]; then
    if drop_verify_database; then
      created=0
    else
      log_warn "无法在限定时间内清理临时验证库: $temp_database"
      [[ "$rc" != "0" ]] || rc=1
    fi
  fi
  if [[ "$lock_acquired" == "1" ]]; then
    release_directory_lock "$lock_dir"
  fi
  exit "$rc"
}
trap cleanup EXIT

wait_for_verify_database || fatal "独立验证库未就绪: $verify_container"
log_info "在独立容器 $verify_container 创建隔离恢复库: $temp_database"
docker exec "$verify_container" sh -ec '
  db="$1"
  [[ "$db" =~ ^[A-Za-z0-9_]+$ ]]
  MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
    --protocol=TCP -h 127.0.0.1 -uroot \
    -e "CREATE DATABASE \`$db\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
' sh "$temp_database"
created=1

gzip -dc "$backup_file" | docker exec -i "$verify_container" sh -ec '
  db="$1"
  [[ "$db" =~ ^[A-Za-z0-9_]+$ ]]
  MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
    --protocol=TCP -h 127.0.0.1 -uroot \
    --init-command="SET SESSION sql_log_bin=0" "$db"
' sh "$temp_database"

table_count="$(docker exec "$verify_container" sh -ec '
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
expected_table_count="$(awk -F= '$1 == "table_count" {print $2; exit}' "$metadata_file" | tr -d '\r')"
if [[ "$expected_table_count" =~ ^[0-9]+$ ]]; then
  [[ "$table_count" == "$expected_table_count" ]] \
    || fatal "恢复表数 $table_count 与备份元数据 $expected_table_count 不一致"
fi

alembic_revision="$(docker exec "$verify_container" sh -ec '
  db="$1"
  [[ "$db" =~ ^[A-Za-z0-9_]+$ ]]
  MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
    --protocol=TCP -h 127.0.0.1 -uroot "$db" \
    --batch --skip-column-names \
    -e "SELECT version_num FROM alembic_version LIMIT 1"
' sh "$temp_database" 2>/dev/null | tr -d '\r' | tail -n 1 || true)"
expected_alembic_revision="$(awk -F= '$1 == "alembic_revision" {print $2; exit}' "$metadata_file" | tr -d '\r')"
if [[ -n "$expected_alembic_revision" && "$expected_alembic_revision" != "unknown" ]]; then
  [[ "$alembic_revision" == "$expected_alembic_revision" ]] \
    || fatal "恢复迁移版本 ${alembic_revision:-missing} 与备份元数据 $expected_alembic_revision 不一致"
fi

if [[ "$alembic_revision" =~ ^0*([0-9]+)$ ]] && (( 10#${BASH_REMATCH[1]} >= 24 )); then
  archive_table_count="$(docker exec "$verify_container" sh -ec '
    db="$1"
    MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
      --protocol=TCP -h 127.0.0.1 -uroot "$db" \
      --batch --skip-column-names \
      -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=\"$db\" AND table_name=\"project_source_archive\""
  ' sh "$temp_database" 2>/dev/null | tr -d '\r' | tail -n 1 || true)"
  [[ "$archive_table_count" == "1" ]] || fatal "024 及以上备份缺少隔离源码归档表"
fi

drop_verify_database || fatal "备份验证完成，但临时验证库清理失败"
created=0
log_info "独立恢复验证通过(container=$verify_container, tables=$table_count, alembic=${alembic_revision:-unknown})"
