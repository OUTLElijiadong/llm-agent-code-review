#!/usr/bin/env bash
# 输出 Prism 生产运行状态 JSON，并以退出码驱动 systemd/云告警。
set -Eeuo pipefail

cd "$(dirname "$0")"
# shellcheck source=lib/common.sh
source "lib/common.sh"

# 输出命令帮助。
# 参数: 无。
# 返回: 始终返回 0。
usage() {
  cat <<'USAGE'
用法: ./ops-check.sh

只读检查 Compose 配置、容器健康、磁盘、内存、备份、Alembic 与 HTTPS。
stdout 仅输出 JSON；任一必需检查失败时退出码为 1。

可选环境变量:
  OPS_DISK_MAX_PERCENT       磁盘使用率上限，默认 85
  OPS_MEMORY_MAX_PERCENT     内存使用率上限，默认 90
  BACKUP_MAX_AGE_HOURS       最近备份最大年龄，默认 30
  BACKUP_DIR                 备份目录，默认 ../backups
  OPS_HTTPS_REQUIRED         是否要求 HTTPS，默认 true
  DEPLOY_ENV_FILE            Compose dotenv，默认 .env
USAGE
}

# 将字符串转义为 JSON 字符串内容。
# 参数: $1 原始字符串。
# 返回: stdout 输出不带双引号的转义结果。
json_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\r'/\\r}"
  value="${value//$'\t'/\\t}"
  printf '%s' "$value"
}

# 输出仅包含预检失败的机器可读 JSON。
# 参数: $1 错误摘要。
# 返回: 始终返回 0。
emit_preflight_failure() {
  local message="$1"
  printf '{"schema_version":1,"status":"error","checked_at_utc":"%s","checks":{"preflight":{"ok":false,"message":"%s"}}}\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$(json_escape "$message")"
}

# 验证百分比阈值为 0 到 100 的整数。
# 参数: $1 待验证值。
# 返回: 合法时 0，否则 1。
valid_percent() {
  local value="$1"
  [[ "$value" =~ ^[0-9]+$ ]] && (( value >= 0 && value <= 100 ))
}

# 获取文件修改时间的 Unix 秒，兼容 GNU/BSD stat。
# 参数: $1 文件路径。
# 返回: stdout 输出 Unix 秒；无法获取时返回 1。
file_mtime_epoch() {
  local file_path="$1"
  stat -c '%Y' "$file_path" 2>/dev/null || stat -f '%m' "$file_path" 2>/dev/null
}

# 估算宿主机内存使用百分比，兼容 Linux 和 macOS。
# 参数: 无。
# 返回: stdout 输出整数百分比；无法计算时输出 -1。
memory_used_percent() {
  local total available page_size available_pages
  if [[ -r /proc/meminfo ]]; then
    awk '
      /^MemTotal:/ { total=$2 }
      /^MemAvailable:/ { available=$2 }
      END {
        if (total > 0 && available >= 0) printf "%.0f\n", ((total-available)*100)/total
        else print -1
      }
    ' /proc/meminfo
    return 0
  fi
  if command -v sysctl >/dev/null 2>&1 && command -v vm_stat >/dev/null 2>&1; then
    total="$(sysctl -n hw.memsize 2>/dev/null || true)"
    page_size="$(vm_stat | awk 'NR==1 { for (i=1; i<=NF; i++) if ($i=="of") { gsub(/[^0-9]/, "", $(i+1)); print $(i+1); exit } }')"
    available_pages="$(vm_stat | awk '
      /Pages free:|Pages inactive:|Pages speculative:/ {
        value=$3; gsub(/[^0-9]/, "", value); sum+=value
      }
      END { print sum+0 }
    ')"
    if [[ "$total" =~ ^[0-9]+$ && "$page_size" =~ ^[0-9]+$ && "$available_pages" =~ ^[0-9]+$ ]] \
      && (( total > 0 )); then
      awk -v total="$total" -v available="$((page_size * available_pages))" \
        'BEGIN { used=((total-available)*100)/total; if (used < 0) used=0; if (used > 100) used=100; printf "%.0f\n", used }'
      return 0
    fi
  fi
  printf '%s\n' '-1'
}

# 获取 Compose 服务的健康或运行状态。
# 参数: $1 服务名。
# 返回: stdout 输出 healthy/running/starting/missing 等状态。
compose_service_status() {
  local service="$1"
  local container_id status
  container_id="$(compose ps -q "$service" 2>/dev/null || true)"
  if [[ -z "$container_id" ]]; then
    printf 'missing\n'
    return 0
  fi
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
    "$container_id" 2>/dev/null || true)"
  printf '%s\n' "${status:-unknown}"
}

# 判断常见真假字符串是否表示 true。
# 参数: $1 字符串。
# 返回: true/1/yes/on 时 0，否则 1。
is_true() {
  local normalized
  normalized="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$normalized" in
    true|1|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
[[ $# -eq 0 ]] || {
  emit_preflight_failure "未知参数: $1"
  exit 2
}

missing_commands=""
for command_name in docker curl awk date df find sort gzip stat tr; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    missing_commands+=" ${command_name}"
  fi
done
if [[ -n "$missing_commands" ]]; then
  emit_preflight_failure "缺少命令:${missing_commands}"
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  emit_preflight_failure "docker compose 不可用"
  exit 1
fi

env_file="${DEPLOY_ENV_FILE:-.env}"
disk_threshold="${OPS_DISK_MAX_PERCENT:-85}"
memory_threshold="${OPS_MEMORY_MAX_PERCENT:-90}"
backup_max_age="${BACKUP_MAX_AGE_HOURS:-30}"
backup_dir="${BACKUP_DIR:-../backups}"
https_required="${OPS_HTTPS_REQUIRED:-true}"
if ! valid_percent "$disk_threshold" || ! valid_percent "$memory_threshold" \
  || [[ ! "$backup_max_age" =~ ^[0-9]+$ ]]; then
  emit_preflight_failure "巡检阈值格式非法"
  exit 1
fi
if [[ ! -f "$env_file" ]]; then
  emit_preflight_failure "Compose dotenv 不存在"
  exit 1
fi
if ! DEPLOY_ENV_FILE="$env_file" compose --env-file "$env_file" config --quiet >/dev/null 2>&1; then
  emit_preflight_failure "Compose 配置解析失败"
  exit 1
fi

containers_ok=true
services_json=""
for service in mysql redis clamav backend frontend; do
  status="$(compose_service_status "$service")"
  if [[ "$status" != "healthy" && "$status" != "running" ]]; then
    containers_ok=false
  fi
  [[ -z "$services_json" ]] || services_json+=","
  services_json+="\"$(json_escape "$service")\":\"$(json_escape "$status")\""
done

disk_used="$(df -P .. | awk 'NR==2 { value=$5; gsub(/%/, "", value); print value }' || true)"
disk_ok=false
if [[ "$disk_used" =~ ^[0-9]+$ ]] && (( disk_used <= disk_threshold )); then
  disk_ok=true
fi
[[ "$disk_used" =~ ^[0-9]+$ ]] || disk_used=-1

memory_used="$(memory_used_percent)"
memory_ok=false
if [[ "$memory_used" =~ ^-?[0-9]+$ ]] && (( memory_used >= 0 && memory_used <= memory_threshold )); then
  memory_ok=true
fi

latest_backup="$(find "$backup_dir" -maxdepth 1 -type f -name 'code_review_*.sql.gz' -print \
  2>/dev/null | sort | tail -n 1 || true)"
backup_ok=false
backup_age_hours=-1
backup_name="none"
backup_checksum_ok=false
backup_gzip_ok=false
if [[ -n "$latest_backup" && -s "$latest_backup" ]]; then
  backup_name="$(basename "$latest_backup")"
  modified_at="$(file_mtime_epoch "$latest_backup" || true)"
  if [[ "$modified_at" =~ ^[0-9]+$ ]]; then
    now_epoch="$(date +%s)"
    backup_age_hours="$(( (now_epoch - modified_at) / 3600 ))"
    (( backup_age_hours < 0 )) && backup_age_hours=0
  fi
  if gzip -t "$latest_backup" >/dev/null 2>&1; then
    backup_gzip_ok=true
  fi
  if [[ -s "$latest_backup.sha256" ]]; then
    expected_checksum="$(awk 'NR==1 {print $1}' "$latest_backup.sha256")"
    actual_checksum="$(sha256_file "$latest_backup")"
    if [[ -n "$expected_checksum" && "$expected_checksum" == "$actual_checksum" ]]; then
      backup_checksum_ok=true
    fi
  fi
  if [[ "$backup_gzip_ok" == "true" && "$backup_checksum_ok" == "true" \
    && "$backup_age_hours" =~ ^[0-9]+$ ]] && (( backup_age_hours <= backup_max_age )); then
    backup_ok=true
  fi
fi

alembic_current="$(current_alembic_revision)"
alembic_heads="$(compose exec -T backend alembic heads 2>/dev/null | awk '{print $1}' | sed '/^$/d' || true)"
alembic_head_count="$(printf '%s\n' "$alembic_heads" | sed '/^$/d' | wc -l | tr -d ' ')"
alembic_head="$(printf '%s\n' "$alembic_heads" | sed '/^$/d' | tail -n 1)"
alembic_ok=false
if [[ "$alembic_head_count" == "1" && "$alembic_current" != "unknown" \
  && -n "$alembic_head" && "$alembic_current" == "$alembic_head" ]]; then
  alembic_ok=true
fi

https_ok=false
https_mode="required"
http_redirect_code="000"
https_health="failed"
if ! is_true "$https_required"; then
  https_ok=true
  https_mode="skipped"
  https_health="skipped"
else
  domain="$(read_env_value APP_DOMAIN "$env_file" || true)"
  if [[ -n "$domain" ]]; then
    http_redirect_code="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 15 \
      --resolve "$domain:80:127.0.0.1" "http://$domain/" 2>/dev/null || true)"
    [[ "$http_redirect_code" =~ ^[0-9][0-9][0-9]$ ]] || http_redirect_code="000"
    if curl --fail --silent --show-error --max-time 20 \
      --resolve "$domain:443:127.0.0.1" "https://$domain/" >/dev/null 2>&1; then
      health_payload="$(curl --fail --silent --show-error --max-time 20 \
        --resolve "$domain:443:127.0.0.1" "https://$domain/healthz" 2>/dev/null || true)"
      if printf '%s' "$health_payload" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"'; then
        https_health="ok"
      fi
    fi
    if [[ "$http_redirect_code" == "308" && "$https_health" == "ok" ]]; then
      https_ok=true
    fi
  fi
fi

overall_ok=true
for check_value in "$containers_ok" "$disk_ok" "$memory_ok" "$backup_ok" "$alembic_ok" "$https_ok"; do
  if [[ "$check_value" != "true" ]]; then
    overall_ok=false
  fi
done
status="ok"
exit_code=0
if [[ "$overall_ok" != "true" ]]; then
  status="error"
  exit_code=1
fi

cat <<JSON
{
  "schema_version": 1,
  "status": "$status",
  "checked_at_utc": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
  "checks": {
    "containers": {"ok": $containers_ok, "services": {$services_json}},
    "disk": {"ok": $disk_ok, "used_percent": $disk_used, "max_percent": $disk_threshold},
    "memory": {"ok": $memory_ok, "used_percent": $memory_used, "max_percent": $memory_threshold},
    "backup": {"ok": $backup_ok, "file": "$(json_escape "$backup_name")", "age_hours": $backup_age_hours, "max_age_hours": $backup_max_age, "gzip_ok": $backup_gzip_ok, "checksum_ok": $backup_checksum_ok},
    "alembic": {"ok": $alembic_ok, "current": "$(json_escape "$alembic_current")", "head": "$(json_escape "$alembic_head")"},
    "https": {"ok": $https_ok, "mode": "$https_mode", "http_redirect_code": "$(json_escape "$http_redirect_code")", "health": "$https_health"}
  }
}
JSON
exit "$exit_code"
