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

# 统一解析生产备份目录：进程环境显式值优先，其次读取部署 dotenv，最后使用旧默认值。
# 参数: 无。
# 返回: stdout 输出相对 deploy 目录的路径或配置的绝对路径。
configured_backup_dir() {
  local configured="${BACKUP_DIR:-}"
  if [[ -z "$configured" ]]; then
    configured="$(read_env_value BACKUP_DIR "${DEPLOY_ENV_FILE:-.env}" || true)"
  fi
  printf '%s\n' "${configured:-../backups}"
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

# 读取账本对应的常驻服务容器；允许发布前一轮安全停机留下的 exited 容器。
# Compose 的 `ps --all` 也可能列出 `run` 一次性容器，因此只接受服务标签匹配且
# oneoff=False 的唯一容器。不能仅凭容器名或最近创建时间猜测当前发布身份。
# 参数: $1 为服务名。
# 返回: stdout 输出唯一运行/已退出的常驻容器 ID；状态未知或容器不唯一时返回 1。
release_service_container_id() {
  local service="$1" container_id candidates candidate service_label oneoff_label

  container_id="$(service_container_id "$service" || true)"
  if [[ -z "$container_id" ]]; then
    candidates="$(compose ps --all --quiet --status exited "$service" 2>/dev/null)" || return 1
    container_id=""
    while IFS= read -r candidate; do
      [[ -n "$candidate" ]] || continue
      service_label="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.service" }}' "$candidate" 2>/dev/null)" \
        || return 1
      oneoff_label="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.oneoff" }}' "$candidate" 2>/dev/null)" \
        || return 1
      case "$service_label:$oneoff_label" in
        "$service:False"|"$service:false"|"$service:FALSE")
          [[ -z "$container_id" ]] || return 1
          container_id="$candidate"
          ;;
      esac
    done <<< "$candidates"
  fi

  [[ -n "$container_id" ]] || return 1
  service_label="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.service" }}' "$container_id" 2>/dev/null)" \
    || return 1
  oneoff_label="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.oneoff" }}' "$container_id" 2>/dev/null)" \
    || return 1
  case "$service_label:$oneoff_label" in
    "$service:False"|"$service:false"|"$service:FALSE") ;;
    *) return 1 ;;
  esac
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
      -e "SELECT version_num FROM alembic_version"
  ' 2>/dev/null | tr -d '\r' || true)"
  if [[ "$revision" =~ ^[A-Za-z0-9_]+$ ]]; then
    printf '%s\n' "$revision"
  elif [[ "$revision" == *$'\n'* ]]; then
    printf 'ambiguous\n'
  else
    printf 'unknown\n'
  fi
}

# 只读查询当前数据库，不向宿主日志/新镜像传递数据库凭据。
# 参数: $1 固定查询；stdout 原样返回，查询故障返回非零。
checkpoint_database_query() {
  compose exec -T mysql sh -ec '
    MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql \
      --protocol=TCP -h 127.0.0.1 -uroot "$MYSQL_DATABASE" \
      --default-character-set=utf8mb4 --connect-timeout=10 \
      --batch --raw --skip-column-names -e "$1"
  ' _ "$1" 2>/dev/null
}

# 检查迁移 119 的物理结构和三项保护触发器。Alembic 版本号本身不能证明
# MySQL DDL 已全部提交；MySQL 的 DDL 可能在迁移失败时留下部分对象。
# 参数: 无。返回 absent / complete / partial；无法查询时返回非零。
chat_authority_database_state() {
  local result schema_state migration_file schema_probe
  result="$(checkpoint_database_query "$(cat <<'SQL'
    SELECT /*+ MAX_EXECUTION_TIME(15000) */ /* prism_chat_authority_schema */
      JSON_OBJECT(
        'column_count',
        (SELECT COUNT(*) FROM information_schema.columns
         WHERE table_schema = DATABASE() AND table_name = 'enterprise_chat_message'
           AND column_name = 'sender_authority_rank'),
        'triggers',
        COALESCE((SELECT JSON_ARRAYAGG(JSON_OBJECT(
                    'trigger_name', trigger_name,
                    'action_timing', action_timing,
                    'event_manipulation', event_manipulation,
                    'action_statement', action_statement
                  ))
                  FROM information_schema.triggers
                  WHERE trigger_schema = DATABASE() AND event_object_table = 'enterprise_chat_message'),
                 JSON_ARRAY())
      )
SQL
  )" | tr -d '\r')" || return 1
  migration_file="$(dirname "${BASH_SOURCE[0]}")/../../backend/alembic/versions/119_chat_message_authority.py"
  schema_probe="$(dirname "${BASH_SOURCE[0]}")/chat_authority_schema_compatibility.py"
  [[ -r "$migration_file" && -r "$schema_probe" ]] || return 1
  schema_state="$(printf '%s' "$result" | python3 "$schema_probe" "$migration_file")" || return 1
  case "$schema_state" in
    absent|complete|partial) printf '%s\n' "$schema_state" ;;
    *) return 1 ;;
  esac
}

# 检查目标 Backend 镜像是否实现迁移 119 的消息写入和权限快照读取契约。
# 参数: $1 已按 digest 绑定的 Backend 镜像。
# 返回: 完整实现时 0；未知、旧版本或 probe 故障时终止发布/回滚。
assert_chat_authority_image_capability() {
  local image_id="$1" expected="$2" capability failure_message
  local probe_file
  case "$expected" in
    supported)
      failure_message="目标镜像不支持 119 小菱消息权限快照契约，拒绝启动；保留数据库并前向修复" ;;
    unsupported)
      failure_message="目标镜像需要 119 小菱消息权限快照结构，但数据库尚未具备该结构；拒绝启动并保留当前版本" ;;
    *) fatal "小菱权限快照能力检查参数无效" ;;
  esac
  probe_file="$(dirname "${BASH_SOURCE[0]}")/chat_authority_compatibility.py"
  [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fatal "小菱权限快照兼容门缺少已绑定 Backend 镜像 ID"
  [[ -r "$probe_file" ]] || fatal "小菱权限快照能力 probe 不可读，拒绝切换镜像"
  capability="$(docker run --rm -i --memory 128m --pids-limit 32 \
    --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --entrypoint python "$image_id" - < "$probe_file")" \
    || fatal "目标镜像小菱权限快照能力验证失败，拒绝启动；保留数据库并前向修复"
  [[ "$capability" == supported || "$capability" == unsupported ]] \
    || fatal "目标镜像小菱权限快照能力结果未知，拒绝启动并保留当前版本"
  [[ "$capability" == "$expected" ]] || fatal "$failure_message"
  log_info "小菱权限快照镜像能力符合(schema_expected=$expected, image=$image_id)"
}

assert_chat_authority_image_compatible() {
  assert_chat_authority_image_capability "$1" supported
}

assert_chat_authority_image_requires_absent_schema() {
  assert_chat_authority_image_capability "$1" unsupported
}

# 发布/回滚前确保没有本 Compose 项目的 Backend 写入者仍在运行。
# 外部 Worker 不受 Compose 管理，迁移 119 时还需要操作者单独确认已停写。
# 参数: 无。返回: 已确认全部 Compose Backend 容器停止时 0，否则终止事务。
stop_chat_authority_writers() {
  local running
  chat_authority_stop_attempted=1
  compose stop backend || fatal "停止 Compose Backend 写入者失败；不得执行聊天权限迁移"
  running="$(compose ps --status running --quiet backend 2>/dev/null)" \
    || fatal "无法确认 Compose Backend 写入者已停止；不得执行聊天权限迁移"
  [[ -z "${running//[$' \t\r\n']/}" ]] \
    || fatal "仍有 Compose Backend 写入者运行；不得执行聊天权限迁移"
  deployment_mutated=1
  log_info "Compose Backend 写入者已全部停止"
}

# 对手动或自动回滚应用同一迁移 119 兼容门。旧库可以继续使用旧 reader；
# 只要 119 列/触发器已经出现，就必须使用完整兼容镜像，部分状态一律拒绝。
# 参数: 无。返回: 目标镜像与当前数据库都可确认兼容时 0，否则拒绝容器切换。
assert_chat_authority_rollback_compatible() {
  local schema revision relation versions_dir revision_probe
  if ! schema="$(chat_authority_database_state)"; then
    stop_chat_authority_writers
    fatal "无法只读确认小菱权限快照结构；Backend 已停，拒绝回滚并保留账本/数据"
  fi
  revision="$(current_alembic_revision)"
  if [[ "$revision" == unknown || "$revision" == ambiguous ]]; then
    stop_chat_authority_writers
    fatal "Alembic current revision 缺失或不唯一；Backend 已停，拒绝回滚并保留数据"
  fi
  versions_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../backend/alembic/versions" && pwd -P)" \
    || { stop_chat_authority_writers; fatal "当前 release 不含 Alembic revision 图；Backend 已停，拒绝确认回滚兼容性"; }
  revision_probe="$(dirname "${BASH_SOURCE[0]}")/alembic_revision_compatibility.py"
  [[ -r "$revision_probe" ]] \
    || { stop_chat_authority_writers; fatal "Alembic revision 兼容 probe 不可读；Backend 已停，拒绝回滚"; }
  if ! relation="$(python3 "$revision_probe" "$versions_dir" "$revision" "119_chat_message_authority")"; then
    stop_chat_authority_writers
    fatal "无法确认数据库是否已经跨过迁移 119；Backend 已停，拒绝回滚"
  fi
  case "$schema" in
    absent)
      if [[ "$relation" != descendant ]]; then
        stop_chat_authority_writers
        fatal "数据库已记录迁移 119 或其后继，但权限列/触发器缺失；Backend 已停，拒绝回滚并前向修复"
      fi
      # 回滚目标可能比数据库 revision 新（例如 119-aware 镜像配 118 schema）。
      # 旧库只有在目标 reader 不依赖新列时才能启动；未知或相反能力都失败关闭。
      assert_chat_authority_image_requires_absent_schema "$BOUND_BACKEND_IMAGE_ID"
      log_info "小菱权限快照回滚门通过(schema=absent, revision=$revision)"
      ;;
    complete)
      if [[ "$relation" != compatible ]]; then
        stop_chat_authority_writers
        fatal "119 权限结构已存在，但 Alembic revision 仍在迁移 119 之前或状态未知；Backend 已停，拒绝回滚"
      fi
      assert_chat_authority_image_compatible "$BOUND_BACKEND_IMAGE_ID"
      log_info "小菱权限快照回滚门通过(schema=complete, revision=$revision)"
      ;;
    partial)
      stop_chat_authority_writers
      fatal "小菱权限快照迁移结构不完整或触发器定义不符；Backend 已停，拒绝回滚并前向修复"
      ;;
    *)
      stop_chat_authority_writers
      fatal "小菱权限快照结构状态未知；Backend 已停，拒绝回滚并前向修复"
      ;;
  esac
}

# 在切换容器前拒绝不认识当前检查点格式的目标镜像。
# 即使新账本暂空，也不能放行旧 reader：业务写者不受发布目录锁约束。
# 无法确认镜像能力/数据库结构时拒绝；保留原数据库并使用前向修复。
assert_checkpoint_rollback_compatible() {
  local capability schema references ledger_schema run_schema checkpoint_column invalid_json has_reference
  local probe_file="$(dirname "${BASH_SOURCE[0]}")/checkpoint_compatibility.py"
  [[ "${BOUND_BACKEND_IMAGE_ID:-}" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fatal "回退检查点门缺少已绑定 Backend 镜像 ID"
  [[ -r "$probe_file" ]] || fatal "回退检查点能力 probe 不可读，拒绝切换镜像"
  capability="$(docker run --rm -i --memory 128m --pids-limit 32 \
    --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --entrypoint python "$BOUND_BACKEND_IMAGE_ID" - < "$probe_file")" \
    || fatal "目标镜像检查点能力验证失败，拒绝切换镜像；保留数据并前向修复"
  [[ "$capability" == supported || "$capability" == unsupported ]] \
    || fatal "目标镜像检查点能力未知，拒绝切换镜像"

  schema="$(checkpoint_database_query "$(cat <<'SQL'
    SELECT /*+ MAX_EXECUTION_TIME(15000) */ /* prism_checkpoint_schema */
      (SELECT COUNT(*) FROM information_schema.tables
       WHERE table_schema = DATABASE() AND table_name = 'agent_response_transcript_message'),
      (SELECT COUNT(*) FROM information_schema.tables
       WHERE table_schema = DATABASE() AND table_name = 'agent_response_run' AND table_type = 'BASE TABLE'),
      (SELECT COUNT(*) FROM information_schema.columns
       WHERE table_schema = DATABASE() AND table_name = 'agent_response_run' AND column_name = 'checkpoint_json')
SQL
  )" | tr -d '\r')" || fatal "无法只读确认当前检查点结构，拒绝切换镜像"
  [[ "$schema" =~ ^[01]$'\t'1$'\t'1$ ]] \
    || fatal "当前检查点结构缺失或未知，拒绝切换镜像"
  IFS=$'\t' read -r ledger_schema run_schema checkpoint_column <<< "$schema"
  references="$(checkpoint_database_query "$(cat <<'SQL'
    SELECT /*+ MAX_EXECUTION_TIME(15000) */ /* prism_checkpoint_references */
      EXISTS(SELECT 1 FROM agent_response_run WHERE
        CASE WHEN JSON_VALID(checkpoint_json) = 1
          THEN JSON_TYPE(checkpoint_json) <> 'OBJECT' ELSE 1 END),
      EXISTS(SELECT 1 FROM agent_response_run WHERE
        CASE WHEN JSON_VALID(checkpoint_json) = 1
          THEN JSON_CONTAINS_PATH(checkpoint_json, 'one', '$._transcript_ref') ELSE 0 END)
SQL
  )" | tr -d '\r')" || fatal "无法只读确认当前检查点引用，拒绝切换镜像"
  [[ "$references" =~ ^[01]$'\t'[01]$ ]] \
    || fatal "当前检查点引用检查结果未知，拒绝切换镜像"
  IFS=$'\t' read -r invalid_json has_reference <<< "$references"
  [[ "$invalid_json" == 0 ]] || fatal "当前检查点存在无效 JSON/非对象，拒绝切换镜像；保留原文并前向修复"
  [[ "$ledger_schema" == 1 || "$has_reference" == 0 ]] \
    || fatal "当前检查点含账本引用但缺账本结构，拒绝切换镜像；不得删除引用或恢复旧备份"
  if [[ "$ledger_schema" == 1 && "$capability" != supported ]]; then
    fatal "已迁移新对话账本，目标镜像不支持 v2 引用；即使账本暂空也拒绝回退。保留当前数据并前向修复，不恢复旧备份"
  fi
  log_info "检查点回退兼容门通过(reader=$capability, ledger_schema=$ledger_schema)"
}

# 新成员角色写者不受维护目录锁约束，暂空列表不代表可以切回旧权限语义。
# 在任何容器切换前检验绑定目标镜像的实际行为，不以版本标签或角色计数放行。
assert_project_member_rollback_compatible() {
  local capability
  local probe_file="$(dirname "${BASH_SOURCE[0]}")/project_member_compatibility.py"
  [[ "${BOUND_BACKEND_IMAGE_ID:-}" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fatal "回退成员权限门缺少已绑定 Backend 镜像 ID"
  [[ -r "$probe_file" ]] || fatal "回退成员权限 probe 不可读，拒绝切换镜像"
  capability="$(docker run --rm -i --memory 128m --pids-limit 32 \
    --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --entrypoint python "$BOUND_BACKEND_IMAGE_ID" - < "$probe_file")" \
    || fatal "目标镜像成员权限能力验证失败，拒绝切换镜像；保留数据并前向修复"
  [[ "$capability" == supported ]] \
    || fatal "目标镜像未确认支持只读成员的读、写及执行边界，拒绝回退；即使成员暂空也保留数据并前向修复"
  log_info "成员权限回退兼容门通过(reader=$capability)"
}

# 获取文件修改时间的 Unix 秒，兼容 GNU/BSD stat。
# 参数: $1 文件或目录路径。
# 返回: stdout 输出 Unix 秒；无法获取时返回 1。
path_mtime_epoch() {
  stat -c '%Y' "$1" 2>/dev/null || stat -f '%m' "$1" 2>/dev/null
}

# 持有者元数据写在锁目录旁边的 <锁目录>.owner，而不是锁目录里面：
# 释放锁仍然只靠 rmdir，旧版本脚本不会因为目录非空而留下永久锁。
# 参数: $1 为锁目录。
# 返回: stdout 输出元数据文件路径。
lock_owner_file() {
  printf '%s.owner\n' "$1"
}

# 记录当前进程为锁持有者（pid、启动时间、命令名）；写入失败不影响加锁。
# 参数: $1 为锁目录。
# 返回: 始终返回 0。
write_lock_owner() {
  local owner_file tmp_file process_identity process_start boot_id
  owner_file="$(lock_owner_file "$1")"
  tmp_file="$owner_file.$$"
  process_identity="$(linux_process_identity "$$" 2>/dev/null || true)"
  process_start="${process_identity#* }"
  [[ "$process_identity" == "$process_start" ]] && process_start=""
  boot_id="$(linux_boot_id 2>/dev/null || true)"
  {
    printf 'pid=%s\n' "$$"
    printf 'started_epoch=%s\n' "$(date -u '+%s')"
    printf 'command=%s\n' "${0##*/}"
    if [[ -n "$process_identity" && -n "$process_start" && -n "$boot_id" ]]; then
      printf 'process_start_ticks=%s\n' "$process_start"
      printf 'boot_id=%s\n' "$boot_id"
    fi
  } > "$tmp_file" 2>/dev/null && mv -f "$tmp_file" "$owner_file" 2>/dev/null || rm -f "$tmp_file" 2>/dev/null
  return 0
}

# 读取持有者元数据中的单个字段；元数据缺失或比锁目录更旧（属于更早的持有者）时视为未记录。
# 参数: $1 为锁目录；$2 为字段名。
# 返回: 找到时 0 并输出字段值，否则 1。
lock_owner_field() {
  local lock_dir="$1" field="$2" owner_file owner_mtime lock_mtime line
  owner_file="$(lock_owner_file "$lock_dir")"
  [[ -f "$owner_file" ]] || return 1
  owner_mtime="$(path_mtime_epoch "$owner_file" || true)"
  lock_mtime="$(path_mtime_epoch "$lock_dir" || true)"
  if [[ "$owner_mtime" =~ ^[0-9]+$ && "$lock_mtime" =~ ^[0-9]+$ ]] && (( owner_mtime + 5 < lock_mtime )); then
    return 1
  fi
  while IFS= read -r line; do
    if [[ "$line" == "$field="* ]]; then
      printf '%s\n' "${line#*=}"
      return 0
    fi
  done < "$owner_file"
  return 1
}

# Linux 上一次读取进程状态与启动 tick。进程 exec 成其他程序时 PID 和启动 tick 不变，
# 可识别 PID 复用，也能把 zombie 与仍在运行的进程区分开。非 Linux 环境返回失败。
# 参数: $1 为 PID。返回: stdout 输出 state 与 /proc/<pid>/stat 的第 22 字段。
linux_process_identity() {
  local pid="$1" stat_line proc_root="${PRISM_PROC_ROOT:-/proc}"
  local -a fields
  [[ "$pid" =~ ^[0-9]+$ && -r "$proc_root/$pid/stat" ]] || return 1
  IFS= read -r stat_line < "$proc_root/$pid/stat" || return 1
  # comm 可包含空格和右括号；删除到最后一个 ") " 后再按空格解析字段 3..22。
  stat_line="${stat_line##*) }"
  read -r -a fields <<< "$stat_line"
  (( ${#fields[@]} >= 20 )) || return 2
  [[ "${fields[0]}" =~ ^[[:alpha:]]$ && "${fields[19]}" =~ ^[0-9]+$ ]] || return 2
  printf '%s %s\n' "${fields[0]}" "${fields[19]}"
}

# Linux 启动标识用于避免重启后 PID 与启动 tick 恰好重合。
linux_boot_id() {
  local proc_root="${PRISM_PROC_ROOT:-/proc}" boot_id
  IFS= read -r boot_id < "$proc_root/sys/kernel/random/boot_id" || return 1
  [[ "$boot_id" =~ ^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$ ]] || return 1
  printf '%s\n' "$boot_id"
}

# 判断锁的记录持有者是否仍在运行。
# 参数: $1 为锁目录。
# 返回: 0=仍在运行；1=持有进程已不存在（含 pid 被其他程序复用）；2=无法判断。
lock_owner_alive() {
  local lock_dir="$1" pid command process_start owner_boot_id current_boot_id current_identity current_state current_start proc_root kill_error cmdline
  pid="$(lock_owner_field "$lock_dir" pid)" || return 2
  [[ "$pid" =~ ^[0-9]+$ ]] || return 2
  process_start="$(lock_owner_field "$lock_dir" process_start_ticks || true)"
  owner_boot_id="$(lock_owner_field "$lock_dir" boot_id || true)"
  if [[ -n "$process_start" || -n "$owner_boot_id" ]]; then
    [[ "$process_start" =~ ^[0-9]+$ && "$owner_boot_id" =~ ^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$ ]] || return 2
    proc_root="${PRISM_PROC_ROOT:-/proc}"
    current_boot_id="$(linux_boot_id 2>/dev/null || true)"
    if [[ -n "$current_boot_id" ]]; then
      [[ "$current_boot_id" == "$owner_boot_id" ]] || return 1
      current_identity="$(linux_process_identity "$pid" 2>/dev/null)"
      case $? in
        0) ;;
        1)
          # A present proc directory with unreadable/vanished stat is ambiguous; never call it stale.
          [[ -d "$proc_root/$pid" ]] && return 2
          ;;
        *) return 2 ;;
      esac
      [[ "$(linux_boot_id 2>/dev/null || true)" == "$current_boot_id" ]] || return 2
      if [[ -z "$current_identity" ]]; then
        if kill_error="$(kill -0 "$pid" 2>&1)"; then
          return 2
        fi
        case "$kill_error" in
          *"No such process"*) return 1 ;;
          *) return 2 ;;
        esac
      fi
      current_state="${current_identity%% *}"
      current_start="${current_identity#* }"
      [[ "$current_state" == Z || "$current_state" == X ]] && return 1
      [[ "$current_start" == "$process_start" ]] || return 1
      return 0
    fi
    # If procfs is unavailable (for example, a lock moved to macOS), retain the
    # conservative kill -0 behavior; a live PID is never declared stale by name.
    if [[ ! -d "$proc_root" ]]; then
      if kill_error="$(kill -0 "$pid" 2>&1)"; then
        return 0
      fi
      case "$kill_error" in
        *"No such process"*) return 1 ;;
        *) return 2 ;;
      esac
    fi
    return 2
  fi
  command="$(lock_owner_field "$lock_dir" command || true)"
  if [[ -r "/proc/$pid/cmdline" ]]; then
    cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    [[ -n "$cmdline" ]] || return 1
    [[ -z "$command" || "$cmdline" == *"$command"* ]] || return 1
    return 0
  fi
  # 没有 /proc（macOS）时退回 kill -0：成功=进程存在；"No such process"=确定不存在；
  # 其他失败（如无权限）无法判断，按“无法判断”处理，绝不据此抢锁。
  local kill_error
  if kill_error="$(kill -0 "$pid" 2>&1)"; then
    return 0
  fi
  case "$kill_error" in
    *"No such process"*) return 1 ;;
  esac
  return 2
}

# 生成一行可读的锁持有者描述，供日志和巡检使用。
# 参数: $1 为锁目录。
# 返回: stdout 输出描述。
describe_lock_owner() {
  local lock_dir="$1" pid command started now held alive_rc=0
  if ! pid="$(lock_owner_field "$lock_dir" pid)"; then
    printf '持有者未记录'
    return 0
  fi
  command="$(lock_owner_field "$lock_dir" command || true)"
  started="$(lock_owner_field "$lock_dir" started_epoch || true)"
  now="$(date -u '+%s')"
  held=""
  if [[ "$started" =~ ^[0-9]+$ ]] && (( now >= started )); then
    held="，已持有 $((now - started)) 秒"
  fi
  lock_owner_alive "$lock_dir" || alive_rc=$?
  case "$alive_rc" in
    0) printf '持有者 pid=%s 命令=%s 仍在运行%s' "$pid" "${command:-未知}" "$held" ;;
    1) printf '持有者 pid=%s 命令=%s 已不存在，属遗留锁%s' "$pid" "${command:-未知}" "$held" ;;
    *) printf '持有者 pid=%s 命令=%s 状态无法判断%s' "$pid" "${command:-未知}" "$held" ;;
  esac
}

# 使用 mkdir 原子获取目录锁，防止并发备份或发布。
# 持有者仍在运行时可有限等待；持有者已不存在时不自动抢锁（无法原子地安全接管），
# 而是立即失败并给出确认后的手工处理命令。
# 参数: $1 为锁目录；$2 为最长等待秒数（默认取 PRISM_LOCK_WAIT_SECONDS，缺省 0 即不等待）。
# 返回: 成功时 0；无法取得锁时终止脚本。
acquire_directory_lock() {
  local lock_dir="$1"
  local wait_seconds="${2:-${PRISM_LOCK_WAIT_SECONDS:-0}}"
  local poll_seconds="${PRISM_LOCK_POLL_SECONDS:-5}"
  local waited=0 next_notice=0 step owner_text alive_rc
  [[ "$wait_seconds" =~ ^[0-9]+$ ]] || fatal "锁等待秒数必须是非负整数: $wait_seconds"
  [[ "$poll_seconds" =~ ^[1-9][0-9]*$ ]] || fatal "锁轮询秒数必须是正整数: $poll_seconds"
  # 用“已睡眠秒数”累计而不是 $SECONDS：$SECONDS 是整秒计数，起点不在整秒边界时
  # 实际等待会比请求值少将近 1 秒。
  while true; do
    if mkdir "$lock_dir" 2>/dev/null; then
      write_lock_owner "$lock_dir"
      return 0
    fi
    owner_text="$(describe_lock_owner "$lock_dir")"
    alive_rc=0
    lock_owner_alive "$lock_dir" || alive_rc=$?
    if (( alive_rc == 1 )); then
      fatal "检测到并发任务或遗留锁: ${lock_dir}（${owner_text}）。确认没有发布、备份、恢复或维护任务在运行后，执行 rmdir '$lock_dir' 再重试"
    fi
    if (( waited >= wait_seconds )); then
      if (( wait_seconds > 0 )); then
        fatal "检测到并发任务或遗留锁: ${lock_dir}（${owner_text}），已等待 ${wait_seconds} 秒"
      fi
      fatal "检测到并发任务或遗留锁: ${lock_dir}（${owner_text}）"
    fi
    if (( waited >= next_notice )); then
      log_info "等待维护锁 ${lock_dir}（${owner_text}）"
      next_notice=$((waited + 60))
    fi
    step=$((wait_seconds - waited))
    (( step <= poll_seconds )) || step=$poll_seconds
    sleep "$step"
    waited=$((waited + step))
  done
}

# 删除当前脚本持有的目录锁；先删持有者元数据再 rmdir，释放期间锁始终有效。
# 参数: $1 为锁目录。
# 返回: 始终返回 0。
release_directory_lock() {
  local lock_dir="$1"
  rm -f "$(lock_owner_file "$lock_dir")" 2>/dev/null || true
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
  [[ -f "$env_file" ]] || fatal "缺少 deploy/${env_file}，请从 .env.example 创建并填写安全值"
  validate_database_credentials "$env_file"
  DEPLOY_ENV_FILE="$env_file" compose --env-file "$env_file" config --quiet \
    || fatal "docker compose 配置解析失败"
}

# 验证 Frontend TLS 与 ACME bind mount 指向可用的持久化目录。
# 参数: $1 为 dotenv 路径；相对路径按当前 deploy/ 目录解析，与 Compose 一致。
# 返回: 域名证书和 webroot 均可读写时返回 0，否则返回 1。
assert_frontend_tls_assets() {
  local env_file="${1:-${DEPLOY_ENV_FILE:-.env}}"
  local domain conf_dir webroot_dir fullchain privkey
  domain="$(read_env_value APP_DOMAIN "$env_file" 2>/dev/null || true)"
  [[ "$domain" =~ ^[A-Za-z0-9.-]+$ ]] || {
    log_warn "APP_DOMAIN 缺失或格式非法，无法验证 Frontend TLS 证书"
    return 1
  }
  conf_dir="$(read_env_value CERTBOT_CONF_DIR "$env_file" 2>/dev/null || true)"
  webroot_dir="$(read_env_value CERTBOT_WEBROOT_DIR "$env_file" 2>/dev/null || true)"
  conf_dir="${conf_dir:-./certbot/conf}"
  webroot_dir="${webroot_dir:-./certbot/www}"
  [[ "$conf_dir" == /* ]] || conf_dir="$PWD/$conf_dir"
  [[ "$webroot_dir" == /* ]] || webroot_dir="$PWD/$webroot_dir"
  fullchain="$conf_dir/live/$domain/fullchain.pem"
  privkey="$conf_dir/live/$domain/privkey.pem"
  if [[ ! -s "$fullchain" || ! -r "$fullchain" || ! -s "$privkey" || ! -r "$privkey" ]]; then
    log_warn "Frontend TLS 证书不可读或缺失(domain=$domain, cert_dir=$conf_dir)"
    return 1
  fi
  if [[ ! -d "$webroot_dir" || ! -w "$webroot_dir" ]]; then
    log_warn "ACME webroot 不存在或不可写(webroot=$webroot_dir)"
    return 1
  fi
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
  local heads current current_output current_count
  heads="$(run_admin_alembic heads 2>/dev/null | awk '{print $1}' | sed '/^$/d')"
  if [[ "$(printf '%s\n' "$heads" | wc -l | tr -d ' ')" != "1" ]]; then
    log_warn "Alembic 必须只有一个 head，实际: ${heads:-none}"
    return 1
  fi
  current_output="$(run_admin_alembic current 2>/dev/null)" || {
    log_warn "无法读取 Alembic current revision"
    return 1
  }
  current_count="$(printf '%s\n' "$current_output" | awk 'NF {count += 1} END {print count + 0}')"
  if [[ "$current_count" != "1" ]]; then
    log_warn "Alembic current 必须只有一个 revision，实际行数: $current_count"
    return 1
  fi
  current="$(printf '%s\n' "$current_output" | awk 'NF {print $1}')"
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
  [[ "$1" =~ ^(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})$ ]] \
    || fatal "应用版本必须是 x.y.z 版本，且每段为 0-999 的无前导零整数"
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

assert_recorded_release_environment() {
  local service container_id expected_id actual_id environment runtime_release runtime_version runtime_status
  for service in backend frontend; do
    # 支持从前一轮 119 安全停机继续前向发布；绝不启动数据库不兼容的旧 Backend。
    container_id="$(release_service_container_id "$service")" \
      || fatal "无法识别唯一的运行或已停止发布容器: $service"
    expected_id="$BOUND_BACKEND_IMAGE_ID"
    [[ "$service" != frontend ]] || expected_id="$BOUND_FRONTEND_IMAGE_ID"
    runtime_status="$(docker inspect --format '{{.State.Status}}' "$container_id")"
    [[ "$runtime_status" == running || "$runtime_status" == exited ]] \
      || fatal "发布容器状态不可安全续发: $service ($runtime_status)"
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

# 运维巡检只能将实际运行的服务判定为发布一致；部署预检单独使用上述函数，
# 才能安全续发账本匹配的 exited 容器。
assert_running_release_environment() {
  assert_recorded_release_environment
  local service container_id runtime_status
  for service in backend frontend; do
    container_id="$(service_container_id "$service")" \
      || fatal "巡检时发布容器未运行: $service"
    runtime_status="$(docker inspect --format '{{.State.Status}}' "$container_id")"
    [[ "$runtime_status" == running ]] || fatal "巡检时发布容器状态异常: $service ($runtime_status)"
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
