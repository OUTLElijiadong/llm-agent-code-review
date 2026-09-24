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

# 发布前检查备份、构建和应用所在文件系统的剩余容量。沿用 ops-check
# 的临界百分比，但告警阈值本身不阻断发布；另留绝对空间给备份恢复验证和镜像构建。
# 参数: $1 仓库目录；$2 备份目录（可尚未创建）。
# 返回: 容量足够时 0；采集失败、配置错误或容量不足时终止脚本。
assert_deploy_capacity() {
  local repo_dir="$1" backup_dir="$2"
  local warning="${OPS_DISK_MAX_PERCENT:-85}"
  local critical="${OPS_DISK_CRITICAL_PERCENT:-}"
  local min_free_gib="${DEPLOY_MIN_FREE_GIB:-12}"
  local min_free_kib docker_root label path df_output metrics available_kib used_percent

  [[ "$warning" =~ ^[0-9]+$ ]] && (( warning <= 100 )) \
    || fatal "OPS_DISK_MAX_PERCENT 必须为 0-100 的整数"
  if [[ -z "$critical" ]]; then
    if (( warning > 95 )); then critical="$warning"; else critical=95; fi
  fi
  [[ "$critical" =~ ^[0-9]+$ ]] && (( critical <= 100 && critical >= warning )) \
    || fatal "OPS_DISK_CRITICAL_PERCENT 必须为不低于告警阈值的 0-100 整数"
  [[ "$min_free_gib" =~ ^[1-9][0-9]*$ ]] && (( min_free_gib <= 1024 )) \
    || fatal "DEPLOY_MIN_FREE_GIB 必须为 1-1024 的整数"
  min_free_kib=$(( min_free_gib * 1024 * 1024 ))

  # 使用 Docker daemon 实际数据根目录，避免 Docker 独立挂载时只检查仓库磁盘。
  docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" \
    || fatal "无法读取 Docker 数据目录，拒绝在容量未知时发布"
  [[ "$docker_root" == /* && -d "$docker_root" ]] \
    || fatal "Docker 数据目录不可访问，拒绝在容量未知时发布"

  # backup.sh 会创建尚不存在的目录；本门禁查询最近的现存父目录所处文件系统。
  while [[ ! -e "$backup_dir" ]]; do
    path="$(dirname "$backup_dir")"
    [[ "$path" != "$backup_dir" ]] || fatal "无法定位备份目录所在文件系统"
    backup_dir="$path"
  done
  [[ -d "$backup_dir" ]] || fatal "备份目录不是目录"

  for label in repository backup docker; do
    case "$label" in
      repository) path="$repo_dir" ;;
      backup) path="$backup_dir" ;;
      docker) path="$docker_root" ;;
    esac
    df_output="$(df -Pk "$path" 2>/dev/null)" \
      || fatal "无法读取 $label 文件系统容量"
    metrics="$(printf '%s\n' "$df_output" | awk 'NR == 2 {print $4, $5}')"
    read -r available_kib used_percent <<< "$metrics"
    used_percent="${used_percent%%%}"
    [[ "$available_kib" =~ ^[0-9]+$ && "$used_percent" =~ ^[0-9]+$ ]] \
      || fatal "无法解析 $label 文件系统容量，拒绝发布"
    if (( used_percent >= critical || available_kib < min_free_kib )); then
      fatal "$label 文件系统容量不足：已用 ${used_percent}%（临界 ${critical}%），可用 ${available_kib} KiB（最低 ${min_free_kib} KiB）；发布尚未开始备份或构建"
    fi
    log_info "发布容量检查通过($label: used=${used_percent}%, available_kib=$available_kib, min_free_gib=$min_free_gib)"
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
    VERSION backend frontend deploy/docker-compose.yml deploy/lib deploy/*.sh 2>/dev/null || true)"
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
prepare_admin_alembic() {
  [[ -n "${BACKEND_RELEASE:-}" && -n "${APP_RELEASE:-}" && -n "${APP_VERSION:-}" ]] \
    || fatal "迁移前必须绑定已验证的发布账本或目标版本"
  validate_app_version "$APP_VERSION"
  validate_release_token "$BACKEND_RELEASE" BACKEND_RELEASE
  MIGRATION_BACKEND_IMAGE_ID="$(release_image_id "prism-backend:$BACKEND_RELEASE")" || return 1
  MIGRATION_MYSQL_CONTAINER_ID="$(service_container_id mysql)" || fatal "MySQL 容器不存在，无法执行迁移"
  local heads
  heads="$(docker run --rm --network none --entrypoint alembic \
    "$MIGRATION_BACKEND_IMAGE_ID" heads 2>/dev/null)" || fatal "迁移镜像无法读取 Alembic heads，拒绝停止服务"
  MIGRATION_ALEMBIC_HEAD="$(printf '%s\n' "$heads" | awk '$2 == "(head)" {print $1}')"
  [[ "$MIGRATION_ALEMBIC_HEAD" =~ ^[A-Za-z0-9_]+$ ]] || fatal "迁移镜像必须包含唯一 Alembic head"
}

run_admin_alembic() {
  local env_file="${DEPLOY_ENV_FILE:-.env}"
  local mysql_container backend_image
  [[ -f "$env_file" ]] || fatal "缺少部署环境文件: $env_file"
  [[ "${MIGRATION_BACKEND_IMAGE_ID:-}" =~ ^sha256:[0-9a-f]{64}$ \
    && -n "${MIGRATION_MYSQL_CONTAINER_ID:-}" ]] || fatal "尚未完成迁移镜像预检，拒绝执行迁移"
  mysql_container="$MIGRATION_MYSQL_CONTAINER_ID"
  backend_image="$MIGRATION_BACKEND_IMAGE_ID"
  docker image inspect "$backend_image" >/dev/null 2>&1 || fatal "Backend 迁移镜像不存在: $backend_image"

  # 生产开启 binary log 时，创建触发器需要数据库管理权限。
  # 共享 MySQL 网络命名空间并连接 127.0.0.1；生产 skip_name_resolve=1
  # 时实际匹配 root@%。该凭据仅交给一次性迁移容器，不进入长期 Backend。
  docker run --rm \
    --network "container:$mysql_container" \
    --env-file "$env_file" \
    -e "APP_RELEASE=$APP_RELEASE" \
    -e "APP_VERSION=$APP_VERSION" \
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
  heads="$(run_admin_alembic heads 2>/dev/null | awk '{print $1}' | sed '/^$/d')"
  if [[ "$(printf '%s\n' "$heads" | wc -l | tr -d ' ')" != "1" ]]; then
    log_warn "Alembic 必须只有一个 head，实际: ${heads:-none}"
    return 1
  fi
  current="$(run_admin_alembic current 2>/dev/null | awk '{print $1}' | tail -n 1)"
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

validate_app_version() {
  [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fatal "APP_VERSION 必须是可验证的 x.y.z 版本"
}

release_image_id() {
  local image_id
  image_id="$(docker image inspect --format '{{.Id}}' "$1" 2>/dev/null)" \
    || fatal "发布镜像不存在: $1"
  [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fatal "发布镜像摘要无效: $1"
  printf '%s\n' "$image_id"
}

resolve_release_version() {
  local release_sha="$1" backend_release="$2" frontend_release="$3" version="${4:-}"
  local image tag label_revision label_version source_version
  [[ "$release_sha" =~ ^[0-9a-f]{40}$ ]] || fatal "发布账本必须包含完整提交 SHA"
  [[ -z "$version" ]] || validate_app_version "$version"
  for image in "prism-backend:$backend_release" "prism-frontend:$frontend_release"; do
    release_image_id "$image" >/dev/null || return 1
    tag="${image#*:}"
    label_revision="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image" 2>/dev/null)" \
      || fatal "无法读取发布镜像来源"
    label_version="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$image" 2>/dev/null)" \
      || fatal "无法读取发布镜像版本"
    [[ "$label_revision" != '<no value>' ]] || label_revision=""
    [[ "$label_version" != '<no value>' ]] || label_version=""
    [[ -z "$label_revision" || "$label_revision" == "$release_sha" ]] || fatal "发布镜像来源与账本 SHA 冲突"
    [[ "$tag" == "$release_sha" || "$label_revision" == "$release_sha" ]] || fatal "镜像标签缺少可验证的提交来源"
    if [[ -n "$label_version" ]]; then
      validate_app_version "$label_version"
      [[ -z "$version" || "$version" == "$label_version" ]] || fatal "发布账本与镜像版本证据冲突"
      version="$label_version"
    fi
  done
  source_version="$(git -C .. show "$release_sha:VERSION" 2>/dev/null)" || source_version=""
  source_version="$(printf '%s' "$source_version" | tr -d '\r\n')"
  if [[ -n "$source_version" ]]; then
    validate_app_version "$source_version"
    [[ -z "$version" || "$version" == "$source_version" ]] || fatal "发布版本与提交 VERSION 冲突"
    version="$source_version"
  fi
  [[ -n "$version" ]] || fatal "历史账本缺少 APP_VERSION 且无可验证的镜像或源码版本证据"
  printf '%s\n' "$version"
}

assert_bound_release_images() {
  [[ "$(release_image_id "prism-backend:$BACKEND_RELEASE")" == "$BOUND_BACKEND_IMAGE_ID" ]] \
    || fatal "Backend 镜像标签已漂移"
  [[ "$(release_image_id "prism-frontend:$FRONTEND_RELEASE")" == "$BOUND_FRONTEND_IMAGE_ID" ]] \
    || fatal "Frontend 镜像标签已漂移"
}

assert_compose_release_environment() (
  local mode="${1:-bound}" env_file="${DEPLOY_ENV_FILE:-.env}"
  local expected_release="$APP_RELEASE" expected_version="$APP_VERSION"
  local expected_backend="$BACKEND_RELEASE" expected_frontend="$FRONTEND_RELEASE"
  local configuration images key expected actual
  if [[ "$mode" == default ]]; then unset APP_RELEASE APP_VERSION BACKEND_RELEASE FRONTEND_RELEASE; fi
  configuration="$(compose --env-file "$env_file" config --environment 2>/dev/null)" \
    || fatal "无法解析发布环境"
  for key in APP_RELEASE APP_VERSION BACKEND_RELEASE FRONTEND_RELEASE; do
    case "$key" in
      APP_RELEASE) expected="$expected_release" ;;
      APP_VERSION) expected="$expected_version" ;;
      BACKEND_RELEASE) expected="$expected_backend" ;;
      FRONTEND_RELEASE) expected="$expected_frontend" ;;
    esac
    actual="$(printf '%s\n' "$configuration" | awk -F= -v key="$key" '$1 == key {value=substr($0,index($0,"=")+1)} END {print value}')"
    [[ "$actual" == "$expected" ]] || fatal "Compose $mode 发布环境漂移: $key"
  done
  images="$(compose --env-file "$env_file" config --images 2>/dev/null)" || fatal "无法解析发布镜像"
  printf '%s\n' "$images" | grep -Fxq "prism-backend:$expected_backend" || fatal "Compose $mode Backend 镜像漂移"
  printf '%s\n' "$images" | grep -Fxq "prism-frontend:$expected_frontend" || fatal "Compose $mode Frontend 镜像漂移"
)

assert_running_release_environment() {
  local service container_id expected_id actual_id environment runtime_release runtime_version
  for service in backend frontend; do
    # 只选择常驻服务容器。`compose run` 产生的一次性容器可能仍处于
    # ps --all 结果中，不能让并行的迁移/巡检把多个 ID 传给 docker inspect。
    container_id="$(service_container_id "$service")"
    [[ -n "$container_id" ]] || fatal "无法识别发布容器: $service"
    expected_id="$BOUND_BACKEND_IMAGE_ID"
    [[ "$service" != frontend ]] || expected_id="$BOUND_FRONTEND_IMAGE_ID"
    actual_id="$(docker inspect --format '{{.Image}}' "$container_id")"
    [[ "$actual_id" == "$expected_id" ]] || fatal "运行容器镜像与发布账本不一致: $service"
    if [[ "$service" == backend ]]; then
      environment="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container_id")"
      runtime_release="$(printf '%s\n' "$environment" | awk -F= '$1 == "APP_RELEASE" {print $2}')"
      runtime_version="$(printf '%s\n' "$environment" | awk -F= '$1 == "APP_VERSION" {print $2}')"
      [[ "$runtime_release" == "$APP_RELEASE" && "$runtime_version" == "$APP_VERSION" ]] \
        || fatal "运行容器版本与发布账本不一致"
    fi
  done
}

# 把默认 Compose 环境文件(.env)校准为与本次发布一致。
# 背景: ops-check 对「运行镜像 ↔ .env ↔ 发布账本」做三方一致性检查,发布后
# 若 .env 仍指旧版本,校准前窗口期每次巡检都判发布环境漂移失败。
# 发布账本落盘后调用;先按惯例备份 .env.before-<短SHA>-<UTC时间>,再原位
# 更新(cat 回写保留原文件权限)。四个键存在则替换、缺失则追加。
# 参数: $1 目标 SHA；$2 Backend tag；$3 Frontend tag；$4 版本号。
# 返回: 校准成功 0;失败非 0(调用方仅告警,不回滚已验收发布)。
calibrate_default_env_file() {
  local target_sha="$1" backend_release="$2" frontend_release="$3" app_version="$4"
  local env_file="${DEPLOY_ENV_FILE:-.env}" backup_file stamp tmp_file key value
  [[ -f "$env_file" ]] || return 0
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_file="${env_file}.before-$(printf '%s' "$target_sha" | cut -c1-9)-${stamp}"
  cp "$env_file" "$backup_file" || return 1
  tmp_file="${env_file}.calibrating.$$"
  sed \
    -e "s|^APP_RELEASE=.*|APP_RELEASE=${target_sha}|" \
    -e "s|^APP_VERSION=.*|APP_VERSION=${app_version}|" \
    -e "s|^BACKEND_RELEASE=.*|BACKEND_RELEASE=${backend_release}|" \
    -e "s|^FRONTEND_RELEASE=.*|FRONTEND_RELEASE=${frontend_release}|" \
    "$env_file" > "$tmp_file" || { rm -f "$tmp_file"; return 1; }
  for key in APP_RELEASE APP_VERSION BACKEND_RELEASE FRONTEND_RELEASE; do
    case "$key" in
      APP_RELEASE) value="$target_sha" ;;
      APP_VERSION) value="$app_version" ;;
      BACKEND_RELEASE) value="$backend_release" ;;
      FRONTEND_RELEASE) value="$frontend_release" ;;
    esac
    grep -q "^${key}=" "$tmp_file" || printf '%s=%s\n' "$key" "$value" >> "$tmp_file"
  done
  cat "$tmp_file" > "$env_file" && rm -f "$tmp_file" || { rm -f "$tmp_file"; return 1; }
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
  local app_version="${8:-}"
  local backend_image_id frontend_image_id
  local temp_file="${state_file}.tmp"
  validate_release_token "$release_sha" RELEASE_SHA
  validate_release_token "$backend_release" BACKEND_RELEASE
  validate_release_token "$frontend_release" FRONTEND_RELEASE
  validate_release_token "$target" TARGET
  validate_app_version "$app_version"
  backend_image_id="$(docker image inspect --format '{{.Id}}' "prism-backend:$backend_release" 2>/dev/null || true)"
  frontend_image_id="$(docker image inspect --format '{{.Id}}' "prism-frontend:$frontend_release" 2>/dev/null || true)"
  [[ -z "$backend_image_id" || "$backend_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fatal "Backend 镜像摘要无效"
  [[ -z "$frontend_image_id" || "$frontend_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fatal "Frontend 镜像摘要无效"
  mkdir -p "$(dirname "$state_file")"
  umask 077
  cat > "$temp_file" <<STATE
RELEASE_SHA=$release_sha
APP_VERSION=$app_version
BACKEND_RELEASE=$backend_release
FRONTEND_RELEASE=$frontend_release
BACKEND_IMAGE_ID=$backend_image_id
FRONTEND_IMAGE_ID=$frontend_image_id
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
  local release_sha backend_release frontend_release app_version recorded_id
  [[ -f "$state_file" ]] || fatal "发布状态不存在: $state_file"
  release_sha="$(read_release_value "$state_file" RELEASE_SHA)" || fatal "状态缺少 RELEASE_SHA"
  backend_release="$(read_release_value "$state_file" BACKEND_RELEASE)" || fatal "状态缺少 BACKEND_RELEASE"
  frontend_release="$(read_release_value "$state_file" FRONTEND_RELEASE)" || fatal "状态缺少 FRONTEND_RELEASE"
  validate_release_token "$release_sha" RELEASE_SHA
  validate_release_token "$backend_release" BACKEND_RELEASE
  validate_release_token "$frontend_release" FRONTEND_RELEASE
  app_version="$(resolve_release_version "$release_sha" "$backend_release" "$frontend_release" \
    "$(read_release_value "$state_file" APP_VERSION || true)")" || return 1
  BOUND_BACKEND_IMAGE_ID="$(release_image_id "prism-backend:$backend_release")" || return 1
  BOUND_FRONTEND_IMAGE_ID="$(release_image_id "prism-frontend:$frontend_release")" || return 1
  recorded_id="$(read_release_value "$state_file" BACKEND_IMAGE_ID || true)"
  [[ -z "$recorded_id" || "$recorded_id" == "$BOUND_BACKEND_IMAGE_ID" ]] || fatal "Backend 镜像摘要与发布账本不一致"
  recorded_id="$(read_release_value "$state_file" FRONTEND_IMAGE_ID || true)"
  [[ -z "$recorded_id" || "$recorded_id" == "$BOUND_FRONTEND_IMAGE_ID" ]] || fatal "Frontend 镜像摘要与发布账本不一致"
  export APP_RELEASE="$release_sha"
  export APP_VERSION="$app_version"
  export BACKEND_RELEASE="$backend_release"
  export FRONTEND_RELEASE="$frontend_release"
}

write_bound_release_state() {
  write_release_state "$1" "$APP_RELEASE" "$BACKEND_RELEASE" "$FRONTEND_RELEASE" all \
    "$(read_release_value "$2" BACKUP_FILE || printf 'none')" \
    "$(read_release_value "$2" ALEMBIC_REVISION || printf 'unknown')" "$APP_VERSION"
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
  local expected_version="${APP_VERSION:-unknown}"
  local base_url="${BACKEND_SMOKE_URL:-http://127.0.0.1:8000}"
  local health ready
  health="$(curl --fail --silent --show-error --max-time 15 "$base_url/healthz")" || return 1
  printf '%s' "$health" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' || return 1
  if [[ "$expected_release" != "unknown" ]]; then
    printf '%s' "$health" | grep -Eq "\"release\"[[:space:]]*:[[:space:]]*\"$expected_release\"" || return 1
  fi
  if [[ "$expected_version" != "unknown" ]]; then
    printf '%s' "$health" | grep -Eq "\"version\"[[:space:]]*:[[:space:]]*\"$expected_version\"" || return 1
  fi
  ready="$(curl --fail --silent --show-error --max-time 15 "$base_url/readyz")" || return 1
  printf '%s' "$ready" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ready"' || return 1
  log_info "Backend API 冒烟通过(version=$expected_version, release=$expected_release)"
}

# 验证 HTTP 308、HTTPS 首页和同源 Backend 健康端点。
# 参数: $1 预期 Backend release。
# 返回: 全部通过时 0；显式 SKIP_HTTPS_SMOKE=1 时直接成功。
smoke_https() {
  local expected_release="${1:-unknown}"
  local expected_version="${APP_VERSION:-unknown}"
  # 兼容旧发布检查器的契约文本: {\"status\":\"ready\",\"release\":\"$expected_release\"}
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
  if [[ "$expected_version" != "unknown" ]]; then
    printf '%s' "$health" | grep -Eq "\"version\"[[:space:]]*:[[:space:]]*\"$expected_version\"" || return 1
  fi
  ready="$(curl --fail --silent --show-error --max-time 20 \
    --resolve "$domain:443:127.0.0.1" "https://$domain/readyz")" || return 1
  if [[ "$expected_release" == "unknown" ]]; then
    printf '%s' "$ready" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ready"' || return 1
  else
    printf '%s' "$ready" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ready"' || return 1
    printf '%s' "$ready" | grep -Eq "\"release\"[[:space:]]*:[[:space:]]*\"$expected_release\"" || return 1
    if [[ "$expected_version" != "unknown" ]]; then
      printf '%s' "$ready" | grep -Eq "\"version\"[[:space:]]*:[[:space:]]*\"$expected_version\"" || return 1
    fi
  fi
  log_info "HTTP→HTTPS 与同源 health/ready 冒烟通过(domain=$domain)"
}
