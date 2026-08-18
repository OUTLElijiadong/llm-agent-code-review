#!/usr/bin/env bash
# 为 Prism MySQL 创建一致性压缩备份、校验和与元数据。
set -Eeuo pipefail

cd "$(dirname "$0")"
# shellcheck source=lib/common.sh
source "lib/common.sh"

# 输出命令帮助。
# 参数: 无。
# 返回: 始终返回 0。
usage() {
  cat <<'USAGE'
用法: ./backup.sh [--reason TEXT] [--output-dir DIR] [--retention-days N]

默认输出目录: BACKUP_DIR 或 ../backups
默认保留天数: BACKUP_RETENTION_DAYS 或 14
USAGE
}

reason="manual"
backup_dir="${BACKUP_DIR:-../backups}"
retention_days="${BACKUP_RETENTION_DAYS:-14}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reason)
      [[ $# -ge 2 ]] || fatal "--reason 缺少值"
      reason="$2"
      shift 2
      ;;
    --output-dir)
      [[ $# -ge 2 ]] || fatal "--output-dir 缺少值"
      backup_dir="$2"
      shift 2
      ;;
    --retention-days)
      [[ $# -ge 2 ]] || fatal "--retention-days 缺少值"
      retention_days="$2"
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

[[ "$retention_days" =~ ^[0-9]+$ ]] || fatal "保留天数必须是非负整数"
require_commands docker gzip awk date
validate_compose_environment

mkdir -p "$backup_dir"
chmod 700 "$backup_dir" 2>/dev/null || true
lock_dir="$(maintenance_lock_path)"
mkdir -p "$(dirname "$lock_dir")"
lock_acquired=0
if [[ "${PRISM_MAINTENANCE_LOCK_HELD:-0}" != "1" ]]; then
  acquire_directory_lock "$lock_dir"
  lock_acquired=1
fi
tmp_file=""

# 清理临时文件和锁。
# 参数: 无。
# 返回: 始终返回 0。
cleanup() {
  [[ -z "$tmp_file" || ! -f "$tmp_file" ]] || rm -f "$tmp_file"
  [[ "$lock_acquired" == "1" ]] || return 0
  release_directory_lock "$lock_dir"
}
trap cleanup EXIT

wait_for_service_health mysql "${MYSQL_HEALTH_TIMEOUT:-120}" || fatal "MySQL 未就绪，无法备份"

# 归档表在 mysqldump 期间必须保持稳定，避免备份元数据与数据内容不一致。
read_archive_stats() {
  compose exec -T mysql sh -ec '
    MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
      --protocol=TCP -h 127.0.0.1 -uroot "$MYSQL_DATABASE" \
      --batch --skip-column-names \
      -e "SELECT COUNT(*), COALESCE(SUM(compressed_size),0), COALESCE(SUM(expanded_size),0) FROM project_source_archive"
  ' 2>/dev/null | tr -d '\r' | tail -n 1
}

archive_stats_before="$(read_archive_stats)"

timestamp="$(date -u '+%Y%m%dT%H%M%SZ')"
git_sha="$(current_git_sha ..)"
short_sha="${git_sha:0:12}"
[[ "$short_sha" == "unknown" ]] && short_sha="nogit"
base_name="code_review_${timestamp}_${short_sha}"
backup_file="$backup_dir/${base_name}.sql.gz"
tmp_file="$backup_file.tmp"
checksum_file="$backup_file.sha256"
metadata_file="$backup_file.meta"

log_info "开始 MySQL 一致性备份(reason=$reason)"
compose exec -T mysql sh -ec '
  MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump \
    --protocol=TCP -h 127.0.0.1 -uroot \
    --single-transaction --quick --routines --triggers --events \
    --hex-blob --set-gtid-purged=OFF --no-tablespaces \
    --default-character-set=utf8mb4 "$MYSQL_DATABASE"
' | gzip -9 > "$tmp_file"

archive_stats_after="$(read_archive_stats)"
[[ "$archive_stats_before" == "$archive_stats_after" ]] || fatal "隔离归档统计在备份期间发生变化"

gzip -t "$tmp_file" || fatal "备份 gzip 完整性校验失败"
[[ -s "$tmp_file" ]] || fatal "备份文件为空"
mv "$tmp_file" "$backup_file"
tmp_file=""
log_info "最后发布 .sql.gz 完成标志: $backup_file"
chmod 600 "$backup_file"
checksum="$(sha256_file "$backup_file")"
printf '%s  %s\n' "$checksum" "$(basename "$backup_file")" > "$checksum_file"
chmod 600 "$checksum_file"

alembic_revision="$(current_alembic_revision)"
table_count="$(compose exec -T mysql sh -ec '
  MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
    --protocol=TCP -h 127.0.0.1 -uroot "$MYSQL_DATABASE" \
    --batch --skip-column-names \
    -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()"
' 2>/dev/null | tr -d '\r' | tail -n 1 || true)"

cat > "$metadata_file" <<META
created_at_utc=$timestamp
reason=$reason
git_sha=$git_sha
alembic_revision=$alembic_revision
table_count=${table_count:-unknown}
sha256=$checksum
file=$(basename "$backup_file")
META
chmod 600 "$metadata_file"

if (( retention_days > 0 )); then
  find "$backup_dir" -maxdepth 1 -type f \
    \( -name 'code_review_*.sql.gz' -o -name 'code_review_*.sql.gz.sha256' -o -name 'code_review_*.sql.gz.meta' \) \
    -mtime "+$retention_days" -print -delete 2>/dev/null || true
fi

log_info "备份完成: $backup_file"
printf '%s\n' "$backup_file"
