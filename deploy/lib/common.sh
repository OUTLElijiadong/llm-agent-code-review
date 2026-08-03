#!/usr/bin/env bash
# Prism 部署脚本共享函数。此文件只提供函数，不应单独执行。

if [[ -n "${PRISM_DEPLOY_COMMON_LOADED:-}" ]]; then
  return 0
fi
readonly PRISM_DEPLOY_COMMON_LOADED=1

# 输出带 UTC 时间戳的信息日志。
# 参数: $* 为日志正文。
# 返回: 始终返回 0。
log_info() {
  printf '[%s] INFO  %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

# 输出带 UTC 时间戳的警告日志。
# 参数: $* 为日志正文。
# 返回: 始终返回 0。
log_warn() {
  printf '[%s] WARN  %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

# 输出错误并终止当前脚本。
# 参数: $* 为错误正文。
# 返回: 不返回，进程以状态码 1 退出。
fatal() {
  printf '[%s] ERROR %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
  exit 1
}

# 验证命令是否存在。
# 参数: 一个或多个命令名。
# 返回: 全部存在时返回 0，否则终止脚本。
require_commands() {
  local command_name
  for command_name in "$@"; do
    command -v "$command_name" >/dev/null 2>&1 || fatal "缺少必需命令: $command_name"
  done
}

# 执行 Docker Compose，确保所有脚本使用同一入口。
# 参数: 原样传递给 docker compose。
# 返回: docker compose 的退出状态。
compose() {
  docker compose "$@"
}

# 从简单 dotenv 文件读取单个值，不执行其中的 Shell 内容。
# 参数: $1 为变量名，$2 为 dotenv 路径（默认 .env）。
# 返回: stdout 输出去除首尾引号后的值；未找到时返回 1。
read_env_value() {
  local key="$1"
  local env_file="${2:-.env}"
  local raw
  [[ -f "$env_file" ]] || return 1
  raw="$(awk -v wanted="$key" '
    $0 ~ "^[[:space:]]*" wanted "[[:space:]]*=" {
      line=$0
      sub("^[[:space:]]*" wanted "[[:space:]]*=[[:space:]]*", "", line)
      value=line
    }
    END { if (value != "") print value }
  ' "$env_file")"
  [[ -n "$raw" ]] || return 1
  if [[ "$raw" == \"*\" && "$raw" == *\" ]]; then
    raw="${raw:1:${#raw}-2}"
  elif [[ "$raw" == \'*\' && "$raw" == *\' ]]; then
    raw="${raw:1:${#raw}-2}"
  fi
  printf '%s\n' "$raw"
}

# 计算文件 SHA-256，兼容 Linux sha256sum 与 macOS shasum。
# 参数: $1 为文件路径。
# 返回: stdout 输出十六进制摘要。
sha256_file() {
  local file_path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file_path" | awk '{print $1}'
  else
    shasum -a 256 "$file_path" | awk '{print $1}'
  fi
}

# 按文件修改时间选择目录中的最新文件，不依赖文件名字典序。
# 参数: $1 目录；$2 find -name 模式。
# 返回: stdout 输出最新文件；无匹配时返回 1。
latest_file_by_mtime() {
  local directory="$1"
  local pattern="$2"
  local candidate candidate_mtime latest latest_mtime resolved_directory
  resolved_directory="$(cd "$directory" 2>/dev/null && pwd -P)" || return 1
  latest=""
  latest_mtime=-1
  while IFS= read -r candidate; do
    candidate_mtime="$(stat -c '%Y' "$candidate" 2>/dev/null || stat -f '%m' "$candidate" 2>/dev/null || true)"
    [[ "$candidate_mtime" =~ ^[0-9]+$ ]] || continue
    if (( candidate_mtime > latest_mtime )); then
      latest="$candidate"
      latest_mtime="$candidate_mtime"
    fi
  done < <(find "$resolved_directory" -maxdepth 1 -type f -name "$pattern" -print 2>/dev/null)
  [[ -n "$latest" ]] || return 1
  printf '%s\n' "$latest"
}

# 获取 Compose 服务对应的容器 ID。
# 参数: $1 为服务名。
# 返回: stdout 输出容器 ID；服务不存在时返回 1。
service_container_id() {
  local service="$1"
  local container_id
  container_id="$(compose ps -q "$service" 2>/dev/null || true)"
  [[ -n "$container_id" ]] || return 1
  printf '%s\n' "$container_id"
}

# 等待 Compose 服务达到 healthy；若未定义 healthcheck，则要求 running。
# 参数: $1 服务名；$2 超时秒数（默认 120）。
# 返回: 健康时 0；超时或退出时 1。
wait_for_service_health() {
  local service="$1"
  local timeout_seconds="${2:-120}"
  local started_at status container_id
  started_at="$(date +%s)"
  status="missing"
  while true; do
    container_id="$(service_container_id "$service" || true)"
    if [[ -n "$container_id" ]]; then
      status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
      case "$status" in
        healthy|running)
          log_info "服务 $service 已就绪(status=$status)"
          return 0
          ;;
        unhealthy|exited|dead)
          log_warn "服务 $service 状态异常(status=$status)"
          return 1
          ;;
      esac
    fi
    if (( $(date +%s) - started_at >= timeout_seconds )); then
      log_warn "等待服务 $service 就绪超时(${timeout_seconds}s, status=$status)"
      return 1
    fi
    sleep 2
  done
}

# 获取当前 Git 完整 SHA；非 Git 目录返回 unknown。
# 参数: $1 为仓库目录（默认上级目录）。
# 返回: stdout 输出 SHA 或 unknown。
current_git_sha() {
  local repo_dir="${1:-..}"
  git -C "$repo_dir" rev-parse HEAD 2>/dev/null || printf 'unknown\n'
}

# 解析任意 Git revision 为完整 commit SHA。
# 参数: $1 仓库目录；$2 revision。
# 返回: stdout 输出完整 SHA；无法解析时返回 1。
resolve_git_revision() {
  local repo_dir="$1"
  local revision="$2"
  git -C "$repo_dir" rev-parse --verify "${revision}^{commit}" 2>/dev/null
}

# 断言会进入镜像构建上下文的仓库路径没有未提交变更。
# 参数: $1 仓库目录。
# 返回: 干净时 0；发现 tracked/untracked 变更时返回 1。
assert_deploy_sources_clean() {
  local repo_dir="$1"
  local dirty
  dirty="$(git -C "$repo_dir" status --porcelain --untracked-files=all -- \
    backend frontend deploy/docker-compose.yml deploy/lib deploy/*.sh 2>/dev/null || true)"
  if [[ -n "$dirty" ]]; then
    log_warn "部署构建上下文存在未提交变更："
    printf '%s\n' "$dirty" >&2
    return 1
  fi
}

# 获取数据库当前 Alembic revision，不回显数据库密码。
# 参数: 无。
# 返回: stdout 输出 revision；查询失败输出 unknown。
current_alembic_revision() {
  local revision
  revision="$(compose exec -T mysql sh -ec '
    MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
      --protocol=TCP -h 127.0.0.1 -uroot "$MYSQL_DATABASE" \
      --batch --skip-column-names \
      -e "SELECT version_num FROM alembic_version LIMIT 1"
  ' 2>/dev/null | tr -d '\r' | tail -n 1 || true)"
  printf '%s\n' "${revision:-unknown}"
}

# 使用 mkdir 原子获取目录锁，防止并发备份或发布。
# 参数: $1 为锁目录。
# 返回: 成功时 0；锁已存在时终止脚本。
acquire_directory_lock() {
  local lock_dir="$1"
  if ! mkdir "$lock_dir" 2>/dev/null; then
    fatal "检测到并发任务或遗留锁: $lock_dir"
  fi
}

# 删除当前脚本持有的目录锁。
# 参数: $1 为锁目录。
# 返回: 始终返回 0。
release_directory_lock() {
  local lock_dir="$1"
  rmdir "$lock_dir" 2>/dev/null || true
}

# 返回备份、验证、恢复、发布、回滚和清理共用的维护锁路径。
# 参数: 无。
# 返回: stdout 输出锁目录路径。
maintenance_lock_path() {
  printf '%s\n' "${MAINTENANCE_LOCK_DIR:-${RELEASE_STATE_DIR:-.releases}/.maintenance.lock}"
}

# 统计密钥包含的小写、大写、数字和符号字符类别数。
# 参数: $1 为待检查密钥。
# 返回: stdout 输出 0-4 的整数。
secret_character_class_count() {
  local secret="$1"
  local count=0
  [[ "$secret" =~ [[:lower:]] ]] && ((count += 1))
  [[ "$secret" =~ [[:upper:]] ]] && ((count += 1))
  [[ "$secret" =~ [[:digit:]] ]] && ((count += 1))
  [[ "$secret" =~ [^[:alnum:]] ]] && ((count += 1))
  printf '%s\n' "$count"
}

# 验证 MySQL root 与应用账号使用不同的强密码。
# 参数: $1 为 dotenv 路径。
# 返回: 合格时 0，否则终止当前部署操作。
validate_database_credentials() {
  local env_file="$1"
  local compose_environment line key value
  local root_password="" app_password="" root_classes app_classes
  compose_environment="$(
    DEPLOY_ENV_FILE="$env_file" compose --env-file "$env_file" config --environment 2>/dev/null
  )" || fatal "docker compose 环境解析失败"
  while IFS= read -r line; do
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      MYSQL_ROOT_PASSWORD) root_password="$value" ;;
      MYSQL_PASSWORD) app_password="$value" ;;
    esac
  done <<< "$compose_environment"
  unset compose_environment line key value
  [[ ${#root_password} -ge 32 ]] || fatal "MYSQL_ROOT_PASSWORD 必须至少 32 个字符"
  [[ ${#app_password} -ge 32 ]] || fatal "MYSQL_PASSWORD 必须至少 32 个字符"
  root_classes="$(secret_character_class_count "$root_password")"
  app_classes="$(secret_character_class_count "$app_password")"
  (( root_classes >= 3 )) || fatal "MYSQL_ROOT_PASSWORD 必须包含至少三类字符"
  (( app_classes >= 3 )) || fatal "MYSQL_PASSWORD 必须包含至少三类字符"
  [[ "$root_password" != "$app_password" ]] || fatal "MySQL root 与应用账号不得共用密码"
}

# 检查部署 .env 与 Compose 配置是否可解析。
# 参数: 无。
# 返回: 配置有效时 0，否则终止脚本。
validate_compose_environment() {
  local env_file="${DEPLOY_ENV_FILE:-.env}"
  [[ -f "$env_file" ]] || fatal "缺少 deploy/$env_file，请从 .env.example 创建并填写安全值"
  validate_database_credentials "$env_file"
  DEPLOY_ENV_FILE="$env_file" compose --env-file "$env_file" config --quiet \
    || fatal "docker compose 配置解析失败"
}

# 验证登录来源地图使用的 GeoLite2 只读数据源。
# 参数: 无；从 deploy 环境文件读取 GEOLITE_DB_HOST_PATH。
# 返回: 文件存在、可读且为绝对路径时 0，否则终止脚本。
validate_geolite_database() {
  local env_file="${DEPLOY_ENV_FILE:-.env}"
  local database_path
  database_path="$(read_env_value GEOLITE_DB_HOST_PATH "$env_file" 2>/dev/null || true)"
  database_path="${database_path:-/opt/code-review/backend/GeoLite2-City.mmdb}"
  [[ "$database_path" == /* ]] || fatal "GEOLITE_DB_HOST_PATH 必须是绝对路径"
  [[ -f "$database_path" ]] || fatal "GeoLite2 数据库不存在或不是普通文件: $database_path"
  [[ -r "$database_path" ]] || fatal "GeoLite2 数据库不可读: $database_path"
}

# 使用 MySQL 容器网络命名空间内的 root@% TCP 账号执行数据库结构迁移。
# 参数: 原样传递给 alembic。
# 返回: alembic 的退出状态。
run_admin_alembic() {
  local env_file="${DEPLOY_ENV_FILE:-.env}"
  local mysql_container backend_container backend_image
  [[ -f "$env_file" ]] || fatal "缺少部署环境文件: $env_file"

  mysql_container="$(service_container_id mysql)" || fatal "MySQL 容器不存在，无法执行迁移"
  if [[ -n "${BACKEND_RELEASE:-}" ]]; then
    validate_release_token "$BACKEND_RELEASE" BACKEND_RELEASE
    backend_image="prism-backend:$BACKEND_RELEASE"
  else
    backend_container="$(service_container_id backend)" || fatal "无法确定 Alembic 所在的 Backend 镜像"
    backend_image="$(docker inspect --format '{{.Config.Image}}' "$backend_container")"
  fi
  [[ -n "$backend_image" ]] || fatal "Backend 镜像名为空"
  docker image inspect "$backend_image" >/dev/null 2>&1 || fatal "Backend 迁移镜像不存在: $backend_image"

  # 生产开启 binary log 时，创建触发器需要数据库管理权限。
  # 共享 MySQL 网络命名空间并连接 127.0.0.1；生产 skip_name_resolve=1
  # 时实际匹配 root@%。该凭据仅交给一次性迁移容器，不进入长期 Backend。
  docker run --rm \
    --network "container:$mysql_container" \
    --env-file "$env_file" \
    -e "APP_RELEASE=${APP_RELEASE:-migration}" \
    -e MALWARE_SCAN_FAIL_CLOSED=true \
    -e DB_HOST=127.0.0.1 \
    -e DB_PORT=3306 \
    -e DB_USER=root \
    "$backend_image" \
    sh -ec '
      test -n "$MYSQL_ROOT_PASSWORD"
      test -n "$MYSQL_DATABASE"
      export DB_PASSWORD="$MYSQL_ROOT_PASSWORD"
      export DB_NAME="$MYSQL_DATABASE"
      exec alembic "$@"
    ' sh "$@"
}

# 在 Backend 容器中执行 Alembic 并断言 current 与唯一 head 一致。
# 参数: 无。
# 返回: 一致时 0，否则返回 1。
assert_alembic_at_head() {
  local heads current
  heads="$(compose run --rm --no-deps backend alembic heads 2>/dev/null | awk '{print $1}' | sed '/^$/d')"
  if [[ "$(printf '%s\n' "$heads" | wc -l | tr -d ' ')" != "1" ]]; then
    log_warn "Alembic 必须只有一个 head，实际: ${heads:-none}"
    return 1
  fi
  current="$(compose run --rm --no-deps backend alembic current 2>/dev/null | awk '{print $1}' | tail -n 1)"
  if [[ "$current" != "$heads" ]]; then
    log_warn "Alembic revision 不一致(current=${current:-none}, head=$heads)"
    return 1
  fi
  log_info "Alembic 已位于 head=$heads"
}

# 从发布状态文件读取单个值，不执行文件内容。
# 参数: $1 状态文件；$2 键名。
# 返回: stdout 输出值；键不存在时返回 1。
read_release_value() {
  read_env_value "$2" "$1"
}

# 校验发布状态字段可安全写入 dotenv 和 Docker tag。
# 参数: $1 字段值；$2 字段名。
# 返回: 合法时 0，否则终止脚本。
validate_release_token() {
  local value="$1"
  local field="$2"
  [[ "$value" =~ ^[A-Za-z0-9_.:-]+$ ]] || fatal "发布状态字段 $field 非法"
}

# 原子写入发布状态文件。
# 参数: $1 文件；$2 SHA；$3 Backend tag；$4 Frontend tag；$5 target；$6 备份；$7 Alembic。
# 返回: 写入成功时 0。
write_release_state() {
  local state_file="$1"
  local release_sha="$2"
  local backend_release="$3"
  local frontend_release="$4"
  local target="$5"
  local backup_file="${6:-none}"
  local alembic_revision="${7:-unknown}"
  local temp_file="${state_file}.tmp"
  validate_release_token "$release_sha" RELEASE_SHA
  validate_release_token "$backend_release" BACKEND_RELEASE
  validate_release_token "$frontend_release" FRONTEND_RELEASE
  validate_release_token "$target" TARGET
  mkdir -p "$(dirname "$state_file")"
  umask 077
  cat > "$temp_file" <<STATE
RELEASE_SHA=$release_sha
BACKEND_RELEASE=$backend_release
FRONTEND_RELEASE=$frontend_release
TARGET=$target
BACKUP_FILE=$backup_file
ALEMBIC_REVISION=$alembic_revision
DEPLOYED_AT_UTC=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
STATE
  mv "$temp_file" "$state_file"
}

# 将状态文件中的镜像 tag 和 release 导出给 Docker Compose。
# 参数: $1 状态文件。
# 返回: 成功时 0；字段缺失时终止脚本。
load_release_environment() {
  local state_file="$1"
  local release_sha backend_release frontend_release
  [[ -f "$state_file" ]] || fatal "发布状态不存在: $state_file"
  release_sha="$(read_release_value "$state_file" RELEASE_SHA)" || fatal "状态缺少 RELEASE_SHA"
  backend_release="$(read_release_value "$state_file" BACKEND_RELEASE)" || fatal "状态缺少 BACKEND_RELEASE"
  frontend_release="$(read_release_value "$state_file" FRONTEND_RELEASE)" || fatal "状态缺少 FRONTEND_RELEASE"
  validate_release_token "$release_sha" RELEASE_SHA
  validate_release_token "$backend_release" BACKEND_RELEASE
  validate_release_token "$frontend_release" FRONTEND_RELEASE
  export APP_RELEASE="$release_sha"
  export BACKEND_RELEASE="$backend_release"
  export FRONTEND_RELEASE="$frontend_release"
}

# 给当前运行容器镜像创建可回滚的本地 tag。
# 参数: $1 Compose 服务；$2 镜像仓库名；$3 tag。
# 返回: stdout 输出 tag 后缀；容器不存在时返回 1。
capture_running_image() {
  local service="$1"
  local image_repository="$2"
  local release_tag="$3"
  local container_id image_id
  container_id="$(service_container_id "$service")" || return 1
  image_id="$(docker inspect --format '{{.Image}}' "$container_id")"
  [[ -n "$image_id" ]] || return 1
  docker image tag "$image_id" "${image_repository}:${release_tag}"
  printf '%s\n' "$release_tag"
}

# 验证回滚目标镜像 tag 存在于本机。
# 参数: $1 镜像仓库；$2 tag。
# 返回: 镜像存在时 0，否则返回 1。
release_image_exists() {
  docker image inspect "$1:$2" >/dev/null 2>&1
}

# 验证 Backend 存活、数据库就绪以及可选 release 标识。
# 参数: $1 预期 release（unknown 时跳过 release 断言）。
# 返回: 全部探测通过时 0，否则返回 1。
smoke_backend() {
  local expected_release="${1:-unknown}"
  local base_url="${BACKEND_SMOKE_URL:-http://127.0.0.1:8000}"
  local health ready
  health="$(curl --fail --silent --show-error --max-time 15 "$base_url/healthz")" || return 1
  printf '%s' "$health" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' || return 1
  if [[ "$expected_release" != "unknown" ]]; then
    printf '%s' "$health" | grep -Eq "\"release\"[[:space:]]*:[[:space:]]*\"$expected_release\"" || return 1
  fi
  ready="$(curl --fail --silent --show-error --max-time 15 "$base_url/readyz")" || return 1
  printf '%s' "$ready" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ready"' || return 1
  log_info "Backend API 冒烟通过(release=$expected_release)"
}

# 验证 HTTP 308、HTTPS 首页和同源 Backend 健康端点。
# 参数: $1 预期 Backend release。
# 返回: 全部通过时 0；显式 SKIP_HTTPS_SMOKE=1 时直接成功。
smoke_https() {
  local expected_release="${1:-unknown}"
  local domain http_code health ready
  if [[ "${SKIP_HTTPS_SMOKE:-0}" == "1" ]]; then
    log_warn "已按显式配置跳过 HTTPS 冒烟"
    return 0
  fi
  domain="$(read_env_value APP_DOMAIN "${DEPLOY_ENV_FILE:-.env}")" || return 1
  [[ -n "$domain" ]] || return 1
  http_code="$(curl --silent --output /dev/null --write-out '%{http_code}' \
    --max-time 15 --resolve "$domain:80:127.0.0.1" "http://$domain/")" || return 1
  [[ "$http_code" == "308" ]] || return 1
  curl --fail --silent --show-error --max-time 20 \
    --resolve "$domain:443:127.0.0.1" "https://$domain/" >/dev/null || return 1
  health="$(curl --fail --silent --show-error --max-time 20 \
    --resolve "$domain:443:127.0.0.1" "https://$domain/healthz")" || return 1
  printf '%s' "$health" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' || return 1
  if [[ "$expected_release" != "unknown" ]]; then
    printf '%s' "$health" | grep -Eq "\"release\"[[:space:]]*:[[:space:]]*\"$expected_release\"" || return 1
  fi
  ready="$(curl --fail --silent --show-error --max-time 20 \
    --resolve "$domain:443:127.0.0.1" "https://$domain/readyz")" || return 1
  if [[ "$expected_release" == "unknown" ]]; then
    printf '%s' "$ready" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ready"' || return 1
  else
    [[ "$ready" == "{\"status\":\"ready\",\"release\":\"$expected_release\"}" ]] || return 1
  fi
  log_info "HTTP→HTTPS 与同源 health/ready 冒烟通过(domain=$domain)"
}
