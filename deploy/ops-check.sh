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
stdout 仅输出 JSON；ok/degraded 退出码为 0，阻断性 error 退出码为 1。

可选环境变量:
  OPS_DISK_MAX_PERCENT       磁盘使用率上限，默认 85
  OPS_DISK_CRITICAL_PERCENT  磁盘临界使用率，默认 95
  OPS_MEMORY_MAX_PERCENT     内存使用率上限，默认 90
  OPS_MEMORY_CRITICAL_PERCENT 内存临界使用率，默认 98
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
  printf '{"schema_version":1,"status":"error","can_continue":false,"summary":"巡检预检失败，不能继续","actions":[{"code":"ops_check_repair","label":"修复巡检预检","requires_human":true}],"checked_at_utc":"%s","checks":{"preflight":{"ok":false,"status":"error","message":"%s"}}}\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$(json_escape "$message")"
}

# 验证百分比阈值为 0 到 100 的整数。
# 参数: $1 待验证值。
# 返回: 合法时 0，否则 1。
valid_percent() {
  local value="$1"
  [[ "$value" =~ ^[0-9]+$ ]] && (( value >= 0 && value <= 100 ))
}

# 将资源使用量映射为 ok/degraded/error 三态。
# 参数: $1 使用率；$2 告警阈值；$3 临界阈值。
# 返回: stdout 输出状态字符串。
resource_status() {
  local used="$1" warning="$2" critical="$3"
  # 达到阈值即进入对应状态，避免 85%/95% 边界被误判为更安全的级别。
  if [[ ! "$used" =~ ^[0-9]+$ ]] || (( used >= critical )); then
    printf 'error\n'
  elif (( used >= warning )); then
    printf 'degraded\n'
  else
    printf 'ok\n'
  fi
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
if [[ -n "${OPS_DISK_CRITICAL_PERCENT:-}" ]]; then
  disk_critical_threshold="$OPS_DISK_CRITICAL_PERCENT"
elif [[ "$disk_threshold" =~ ^[0-9]+$ ]] && (( disk_threshold > 95 )); then
  disk_critical_threshold="$disk_threshold"
else
  disk_critical_threshold=95
fi
if [[ -n "${OPS_MEMORY_CRITICAL_PERCENT:-}" ]]; then
  memory_critical_threshold="$OPS_MEMORY_CRITICAL_PERCENT"
elif [[ "$memory_threshold" =~ ^[0-9]+$ ]] && (( memory_threshold > 98 )); then
  memory_critical_threshold="$memory_threshold"
else
  memory_critical_threshold=98
fi
backup_max_age="${BACKUP_MAX_AGE_HOURS:-30}"
backup_dir="${BACKUP_DIR:-../backups}"
https_required="${OPS_HTTPS_REQUIRED:-true}"
if ! valid_percent "$disk_threshold" || ! valid_percent "$disk_critical_threshold" \
  || ! valid_percent "$memory_threshold" || ! valid_percent "$memory_critical_threshold" \
  || (( disk_critical_threshold < disk_threshold )) \
  || (( memory_critical_threshold < memory_threshold )) \
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
[[ "$disk_used" =~ ^[0-9]+$ ]] || disk_used=-1
disk_status="$(resource_status "$disk_used" "$disk_threshold" "$disk_critical_threshold")"
disk_ok=false
[[ "$disk_status" == "ok" ]] && disk_ok=true

memory_used="$(memory_used_percent)"
memory_status="$(resource_status "$memory_used" "$memory_threshold" "$memory_critical_threshold")"
memory_ok=false
[[ "$memory_status" == "ok" ]] && memory_ok=true

latest_backup="$(latest_file_by_mtime "$backup_dir" 'code_review_*.sql.gz' || true)"
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
alembic_status="ok"
[[ "$alembic_ok" == "true" ]] || alembic_status="error"

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

containers_status="ok"
[[ "$containers_ok" == "true" ]] || containers_status="error"
backup_status="ok"
[[ "$backup_ok" == "true" ]] || backup_status="error"
https_status="ok"
[[ "$https_ok" == "true" ]] || https_status="error"

actions_json=""
blocking_checks_json=""
add_action() {
  local code="$1" label="$2" message="$3" requires_human="$4"
  [[ -z "$actions_json" ]] || actions_json+=","
  actions_json+="{\"code\":\"$(json_escape "$code")\",\"label\":\"$(json_escape "$label")\",\"message\":\"$(json_escape "$message")\",\"requires_human\":$requires_human}"
}
add_blocking_check() {
  [[ -z "$blocking_checks_json" ]] || blocking_checks_json+=","
  blocking_checks_json+="\"$(json_escape "$1")\""
}

if [[ "$disk_status" == "degraded" ]]; then
  add_action "disk_cleanup_review" "审阅磁盘清理" "磁盘使用率 ${disk_used}% 已达到或超过告警阈值 ${disk_threshold}%，请先执行 cleanup.sh dry-run 并由人确认。" true
elif [[ "$disk_status" == "error" ]]; then
  add_action "disk_emergency_capacity" "人工处置磁盘" "磁盘使用率 ${disk_used}% 已达到临界阈值 ${disk_critical_threshold}%，禁止继续发布或写入。" true
  add_blocking_check disk
fi
if [[ "$memory_status" == "degraded" ]]; then
  add_action "memory_pressure_review" "审阅内存压力" "内存使用率 ${memory_used}% 已达到或超过告警阈值 ${memory_threshold}%，请检查异常任务或扩容。" true
elif [[ "$memory_status" == "error" ]]; then
  add_action "memory_emergency_capacity" "人工处置内存" "内存使用率 ${memory_used}% 已达到临界阈值 ${memory_critical_threshold}%，禁止继续高负载操作。" true
  add_blocking_check memory
fi
if [[ "$containers_status" == "error" ]]; then
  add_blocking_check containers
  add_action "containers_recovery" "恢复关键容器" "至少一个关键容器未处于 healthy/running，请由值班人员检查日志后恢复。" true
fi
if [[ "$backup_status" == "error" ]]; then
  add_blocking_check backup
  add_action "backup_recovery" "修复备份链路" "最近备份不存在、过期或校验失败，禁止继续发布并先完成可验证备份。" true
fi
if [[ "$alembic_status" == "error" ]]; then
  add_blocking_check alembic
  add_action "alembic_reconcile" "修复数据库迁移" "数据库当前版本与唯一 head 不一致，禁止继续业务迁移或发布。" true
fi
if [[ "$https_status" == "error" ]]; then
  add_blocking_check https
  add_action "https_recovery" "恢复 HTTPS" "公网 HTTPS 或健康检查失败，请先恢复入口再继续发布。" true
fi

status="ok"
exit_code=0
can_continue=true
if [[ -n "$blocking_checks_json" ]]; then
  status="error"
  can_continue=false
  exit_code=1
elif [[ "$disk_status" == "degraded" || "$memory_status" == "degraded" ]]; then
  status="degraded"
fi
summary="全部生产关键检查通过"
[[ "$status" == "degraded" ]] && summary="关键服务可继续，但存在需要人工处理的降级项"
[[ "$status" == "error" ]] && summary="存在阻断性生产故障，已停止自动继续"

cat <<JSON
{
  "schema_version": 1,
  "status": "$status",
  "can_continue": $can_continue,
  "summary": "$(json_escape "$summary")",
  "actions": [$actions_json],
  "blocking_checks": [$blocking_checks_json],
  "checked_at_utc": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
  "checks": {
    "containers": {"ok": $containers_ok, "status": "$containers_status", "services": {$services_json}},
    "disk": {"ok": $disk_ok, "status": "$disk_status", "used_percent": $disk_used, "max_percent": $disk_threshold, "critical_percent": $disk_critical_threshold},
    "memory": {"ok": $memory_ok, "status": "$memory_status", "used_percent": $memory_used, "max_percent": $memory_threshold, "critical_percent": $memory_critical_threshold},
    "backup": {"ok": $backup_ok, "status": "$backup_status", "file": "$(json_escape "$backup_name")", "age_hours": $backup_age_hours, "max_age_hours": $backup_max_age, "gzip_ok": $backup_gzip_ok, "checksum_ok": $backup_checksum_ok},
    "alembic": {"ok": $alembic_ok, "status": "$alembic_status", "current": "$(json_escape "$alembic_current")", "head": "$(json_escape "$alembic_head")"},
    "https": {"ok": $https_ok, "status": "$https_status", "mode": "$https_mode", "http_redirect_code": "$(json_escape "$http_redirect_code")", "health": "$https_health"}
  }
}
JSON
exit "$exit_code"
