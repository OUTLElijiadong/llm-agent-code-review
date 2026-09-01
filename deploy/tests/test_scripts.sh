#!/usr/bin/env bash
# 对部署 Shell、Compose、Nginx、运维巡检与受控清理执行只读回归测试。
set -Eeuo pipefail

cd "$(dirname "$0")/.."

# 断言文件包含指定固定文本。
# 参数: $1 文件；$2 固定文本。
# 返回: 命中时 0，否则退出 1。
assert_contains() {
  local file="$1"
  local expected="$2"
  grep -Fq -- "$expected" "$file" || {
    printf '缺少预期文本: %s -> %s\n' "$file" "$expected" >&2
    exit 1
  }
}

# 断言文件不包含指定固定文本。
# 参数: $1 文件；$2 不应出现的固定文本。
# 返回: 未命中时 0，否则退出 1。
assert_not_contains() {
  local file="$1"
  local unexpected="$2"
  if grep -Fq -- "$unexpected" "$file"; then
    printf '发现禁止文本: %s -> %s\n' "$file" "$unexpected" >&2
    exit 1
  fi
}

# 输出文件 SHA-256，兼容 GNU/Linux 和 macOS。
# 参数: $1 文件。
# 返回: 摘要计算成功时返回 0。
file_sha256() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | cut -d' ' -f1
  else
    shasum -a 256 "$file" | cut -d' ' -f1
  fi
}

# 写入通过生产强度门禁的独立 MySQL 测试凭据。
# 参数: $1 目标 dotenv 路径。
# 返回: 写入成功时返回 0。
write_strong_database_test_env() {
  local target="$1"
  cat > "$target" <<'ENV'
APP_DOMAIN=example.test
MYSQL_ROOT_PASSWORD=RootCredentialForTests2026Alpha01
MYSQL_PASSWORD=AppCredentialForTests2026Beta002
ENV
}

# 动态验证数据库凭据门禁的通过与拒绝分支。
# 参数: $1 测试根目录。
# 返回: 所有边界断言符合预期时返回 0。
run_database_credential_validation() {
  local workspace="$1/database-credentials"
  local env_file="$workspace/deploy.env"
  local output_file="$workspace/output.log"

  mkdir -p "$workspace"
  write_strong_database_test_env "$env_file"
  bash -c 'source ./lib/common.sh; validate_database_credentials "$1"' _ \
    "$env_file" > "$output_file" 2>&1

  cat > "$env_file" <<'ENV'
MYSQL_ROOT_PASSWORD=ShortRoot1A
MYSQL_PASSWORD=AppCredentialForTests2026Beta002
ENV
  if bash -c 'source ./lib/common.sh; validate_database_credentials "$1"' _ \
    "$env_file" > "$output_file" 2>&1; then
    printf '数据库凭据门禁未拒绝短 root 密码\n' >&2
    exit 1
  fi
  assert_contains "$output_file" 'MYSQL_ROOT_PASSWORD 必须至少 32 个字符'

  cat > "$env_file" <<'ENV'
MYSQL_ROOT_PASSWORD=ShortRoot1A # ThisCommentCannotMakeTheResolvedCredentialStrong2026
MYSQL_PASSWORD=AppCredentialForTests2026Beta002
ENV
  if bash -c 'source ./lib/common.sh; validate_database_credentials "$1"' _ \
    "$env_file" > "$output_file" 2>&1; then
    printf '数据库凭据门禁接受了用行尾注释伪装的短 root 密码\n' >&2
    exit 1
  fi
  assert_contains "$output_file" 'MYSQL_ROOT_PASSWORD 必须至少 32 个字符'

  cat > "$env_file" <<'ENV'
MYSQL_ROOT_PASSWORD=ShortRoot1A${PRISM_TEST_CREDENTIAL_PADDING_NOT_SET:-}
MYSQL_PASSWORD=AppCredentialForTests2026Beta002
ENV
  if bash -c 'source ./lib/common.sh; validate_database_credentials "$1"' _ \
    "$env_file" > "$output_file" 2>&1; then
    printf '数据库凭据门禁接受了用未设置变量插值伪装的短 root 密码\n' >&2
    exit 1
  fi
  assert_contains "$output_file" 'MYSQL_ROOT_PASSWORD 必须至少 32 个字符'

  cat > "$env_file" <<'ENV'
MYSQL_ROOT_PASSWORD=ROOTCREDENTIALFORTESTS2026ALPHA0001
MYSQL_PASSWORD=AppCredentialForTests2026Beta002
ENV
  if bash -c 'source ./lib/common.sh; validate_database_credentials "$1"' _ \
    "$env_file" > "$output_file" 2>&1; then
    printf '数据库凭据门禁未拒绝字符类别不足的 root 密码\n' >&2
    exit 1
  fi
  assert_contains "$output_file" 'MYSQL_ROOT_PASSWORD 必须包含至少三类字符'

  cat > "$env_file" <<'ENV'
MYSQL_ROOT_PASSWORD=RootCredentialForTests2026Alpha01
MYSQL_PASSWORD=ShortApp1A
ENV
  if bash -c 'source ./lib/common.sh; validate_database_credentials "$1"' _ \
    "$env_file" > "$output_file" 2>&1; then
    printf '数据库凭据门禁未拒绝短应用密码\n' >&2
    exit 1
  fi
  assert_contains "$output_file" 'MYSQL_PASSWORD 必须至少 32 个字符'

  cat > "$env_file" <<'ENV'
MYSQL_ROOT_PASSWORD=RootCredentialForTests2026Alpha01
MYSQL_PASSWORD=APPCREDENTIALFORTESTS2026BETA00002
ENV
  if bash -c 'source ./lib/common.sh; validate_database_credentials "$1"' _ \
    "$env_file" > "$output_file" 2>&1; then
    printf '数据库凭据门禁未拒绝字符类别不足的应用密码\n' >&2
    exit 1
  fi
  assert_contains "$output_file" 'MYSQL_PASSWORD 必须包含至少三类字符'

  cat > "$env_file" <<'ENV'
MYSQL_PASSWORD=AppCredentialForTests2026Beta002
ENV
  if bash -c 'source ./lib/common.sh; validate_database_credentials "$1"' _ \
    "$env_file" > "$output_file" 2>&1; then
    printf '数据库凭据门禁未拒绝缺失的 root 密码\n' >&2
    exit 1
  fi
  assert_contains "$output_file" 'MYSQL_ROOT_PASSWORD 必须至少 32 个字符'

  cat > "$env_file" <<'ENV'
MYSQL_ROOT_PASSWORD=RootCredentialForTests2026Alpha01
ENV
  if bash -c 'source ./lib/common.sh; validate_database_credentials "$1"' _ \
    "$env_file" > "$output_file" 2>&1; then
    printf '数据库凭据门禁未拒绝缺失的应用密码\n' >&2
    exit 1
  fi
  assert_contains "$output_file" 'MYSQL_PASSWORD 必须至少 32 个字符'

  cat > "$env_file" <<'ENV'
MYSQL_ROOT_PASSWORD=SharedCredentialForTests2026Alpha01
MYSQL_PASSWORD=SharedCredentialForTests2026Alpha01
ENV
  if bash -c 'source ./lib/common.sh; validate_database_credentials "$1"' _ \
    "$env_file" > "$output_file" 2>&1; then
    printf '数据库凭据门禁未拒绝 root 与应用账号共用密码\n' >&2
    exit 1
  fi
  assert_contains "$output_file" 'MySQL root 与应用账号不得共用密码'
}

# 清理测试临时目录。
# 参数: 无。
# 返回: 始终返回 0。
cleanup_test_workspace() {
  [[ -z "${test_root:-}" ]] || rm -rf "$test_root"
}

# 写入可预测的 Docker 命令替身。
# 参数: $1 目标可执行文件路径。
# 返回: 写入和 chmod 成功时返回 0。
write_fake_docker() {
  local target="$1"
  cat > "$target" <<'SCRIPT'
#!/usr/bin/env bash
set -eu
log_file="${FAKE_DOCKER_LOG:?FAKE_DOCKER_LOG is required}"
printf '%s\n' "$*" >> "$log_file"

case "${1:-}" in
  compose)
    shift
    case "${1:-}" in
      version)
        printf '%s\n' 'Docker Compose version v2.test'
        ;;
      --env-file)
        env_file="${2:?env file is required}"
        shift 2
        if [[ "${1:-}" == "config" && "${2:-}" == "--environment" ]]; then
          awk '/^[[:space:]]*(MYSQL_ROOT_PASSWORD|MYSQL_PASSWORD)[[:space:]]*=/ {
            line=$0
            sub(/^[[:space:]]*/, "", line)
            print line
          }' "$env_file"
        fi
        ;;
      ps)
        [[ "${2:-}" == "-q" ]] && printf 'cid-%s\n' "${3:-unknown}"
        ;;
      exec)
        if [[ "${FAKE_DOCKER_SCENARIO:-}" == "verify_missing_024" ]]; then
          case "$*" in
            *'table_name='*'project_source_archive'*) printf '%s\n' '0' ;;
            *'SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='*)
              printf '%s\n' '25'
              ;;
            *'SELECT version_num FROM alembic_version'*) printf '%s\n' '024' ;;
            *'CREATE DATABASE'*|*'DROP DATABASE'*) ;;
            *) cat >/dev/null ;;
          esac
        elif [[ "${FAKE_DOCKER_SCENARIO:-}" == "backup_archive_drift" ]]; then
          case "$*" in
            *'project_source_archive'*)
              state_file="${FAKE_BACKUP_STATE:?FAKE_BACKUP_STATE is required}"
              stats_count=0
              [[ ! -f "$state_file" ]] || read -r stats_count < "$state_file"
              stats_count=$((stats_count + 1))
              printf '%s\n' "$stats_count" > "$state_file"
              if [[ "$stats_count" == "1" ]]; then
                printf '1\t100\t100\n'
              else
                printf '1\t101\t101\n'
              fi
              ;;
            *'mysqldump'*) printf '%s\n' 'CREATE TABLE backup_test(id INT);' ;;
          esac
        else
          case "$*" in
            *"backend alembic heads"*) printf '%s\n' '009 (head)' ;;
            *" mysql "*) printf '%s\n' '009' ;;
          esac
        fi
        ;;
    esac
    ;;
  inspect)
    last=""
    for last in "$@"; do :; done
    case "$last" in
      cid-*) printf '%s\n' 'healthy' ;;
      cr_backend) printf '%s\n' 'sha256:running-backend' ;;
      cr_frontend) printf '%s\n' 'sha256:running-frontend' ;;
      *) printf '%s\n' 'unknown' ;;
    esac
    ;;
  image)
    case "${2:-}" in
      ls)
        case "${3:-}" in
          prism-backend)
            printf '%s\n' be-current be-previous be-running be-new be-old
            ;;
          prism-frontend)
            printf '%s\n' fe-current fe-previous fe-running fe-new fe-old
            ;;
        esac
        ;;
      inspect)
        last=""
        for last in "$@"; do :; done
        if [[ "${FAKE_DOCKER_SCENARIO:-}" == "playwright_stateful" ]]; then
          state_file="${FAKE_PLAYWRIGHT_STATE_FILE:?FAKE_PLAYWRIGHT_STATE_FILE is required}"
          case "$last" in
            *@sha256:*) digest="${last##*@}" ;;
            prism-sandbox-playwright:protected-*)
              digest="$(awk -v tag="$last" '$1 == "tag" && $2 == tag { print $3; exit }' "$state_file")"
              ;;
            *) digest="" ;;
          esac
          [[ -n "$digest" ]] && grep -Fqx -- "image $digest" "$state_file" || exit 1
          printf '%s\n' "$digest"
          exit 0
        fi
        if [[ "${FAKE_DOCKER_SCENARIO:-}" == "missing_playwright" \
          && "$last" == *"sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc" ]]; then
          exit 1
        fi
        case "$last" in
          prism-backend:be-running) printf '%s\n' 'sha256:running-backend' ;;
          prism-frontend:fe-running) printf '%s\n' 'sha256:running-frontend' ;;
          prism-sandbox-python:3.11) printf '%s\n' '{"Id":"sha256:1111111111111111111111111111111111111111111111111111111111111111"}' ;;
          prism-sandbox-node:20) printf '%s\n' '{"Id":"sha256:2222222222222222222222222222222222222222222222222222222222222222"}' ;;
          prism-sandbox-java:17) printf '%s\n' '{"Id":"sha256:3333333333333333333333333333333333333333333333333333333333333333"}' ;;
          prism-sandbox-go:1.23) printf '%s\n' '{"Id":"sha256:4444444444444444444444444444444444444444444444444444444444444444"}' ;;
          prism-sandbox-php:8.3) printf '%s\n' '{"Id":"sha256:5555555555555555555555555555555555555555555555555555555555555555"}' ;;
          *) printf 'sha256:%s\n' "$(printf '%s' "$last" | tr ':/' '--')" ;;
        esac
        ;;
      tag)
        if [[ "${FAKE_DOCKER_SCENARIO:-}" == "playwright_stateful" ]]; then
          state_file="${FAKE_PLAYWRIGHT_STATE_FILE:?FAKE_PLAYWRIGHT_STATE_FILE is required}"
          source_ref="${3:?source image is required}"
          target_tag="${4:?target tag is required}"
          digest="${source_ref##*@}"
          grep -Fqx -- "image $digest" "$state_file" || exit 1
          awk -v tag="$target_tag" '!($1 == "tag" && $2 == tag)' "$state_file" > "$state_file.tmp"
          printf 'tag %s %s\n' "$target_tag" "$digest" >> "$state_file.tmp"
          mv "$state_file.tmp" "$state_file"
        fi
        ;;
      prune)
        if [[ "${FAKE_DOCKER_SCENARIO:-}" == "playwright_stateful" ]]; then
          state_file="${FAKE_PLAYWRIGHT_STATE_FILE:?FAKE_PLAYWRIGHT_STATE_FILE is required}"
          awk '
            $1 == "tag" { tags[$3]=1; lines[NR]=$0; next }
            $1 == "image" { images[NR]=$2; next }
            { lines[NR]=$0 }
            END {
              for (i=1; i<=NR; i++) {
                if (i in images) { if (images[i] in tags) print "image " images[i] }
                else if (i in lines) print lines[i]
              }
            }
          ' "$state_file" > "$state_file.tmp"
          mv "$state_file.tmp" "$state_file"
        fi
        ;;
      rm)
        ;;
    esac
    ;;
  builder)
    ;;
esac
SCRIPT
  chmod +x "$target"
}

# 写入恢复失败模拟专用的 Docker 命令替身。
# 参数: $1 目标可执行文件路径。
# 返回: 写入和 chmod 成功时返回 0。
write_restore_fake_docker() {
  local target="$1"
  cat > "$target" <<'SCRIPT'
#!/usr/bin/env bash
set -eu
log_file="${FAKE_DOCKER_LOG:?FAKE_DOCKER_LOG is required}"
state_file="${FAKE_RESTORE_STATE:?FAKE_RESTORE_STATE is required}"
printf '%s\n' "$*" >> "$log_file"

case "${1:-}" in
  compose)
    shift
    case "${1:-}" in
      --env-file)
        env_file="${2:?env file is required}"
        shift 2
        if [[ "${1:-}" == "config" && "${2:-}" == "--environment" ]]; then
          awk '/^[[:space:]]*(MYSQL_ROOT_PASSWORD|MYSQL_PASSWORD)[[:space:]]*=/ {
            line=$0
            sub(/^[[:space:]]*/, "", line)
            print line
          }' "$env_file"
        fi
        ;;
      exec)
        case "$*" in
          *'printf "%s" "$MYSQL_DATABASE"'*)
            printf '%s' 'code_review'
            ;;
          *'DROP DATABASE IF EXISTS'*)
            ;;
          *'--max-allowed-packet=64M "$MYSQL_DATABASE"'*)
            cat >/dev/null
            import_count=0
            [[ ! -f "$state_file" ]] || read -r import_count < "$state_file"
            import_count=$((import_count + 1))
            printf '%s\n' "$import_count" > "$state_file"
            if [[ "$import_count" == "1" ]]; then
              exit 42
            fi
            ;;
        esac
        ;;
      stop|up)
        ;;
      ps)
        [[ "${2:-}" == "-q" ]] && printf 'cid-%s\n' "${3:-unknown}"
        ;;
    esac
    ;;
  inspect)
    printf '%s\n' 'healthy'
    ;;
esac
SCRIPT
  chmod +x "$target"
}

# 写入可预测的 curl 命令替身。
# 参数: $1 目标可执行文件路径。
# 返回: 写入和 chmod 成功时返回 0。
write_fake_curl() {
  local target="$1"
  cat > "$target" <<'SCRIPT'
#!/usr/bin/env bash
set -eu
case " $* " in
  *"/healthz"*) printf '%s' '{"status":"ok","release":"test"}' ;;
  *" http://"*) printf '%s' '308' ;;
  *) ;;
esac
SCRIPT
  chmod +x "$target"
}

# 写入可预测的 df 命令替身，覆盖资源告警与临界分支。
# 参数: $1 目标可执行文件路径。
# 返回: 写入和 chmod 成功时返回 0。
write_fake_df() {
  local target="$1"
  cat > "$target" <<'SCRIPT'
#!/usr/bin/env bash
set -eu
percent="${FAKE_DF_PERCENT:-50}"
printf '%s\n' 'Filesystem 1024-blocks Used Available Capacity Mounted on'
printf '/dev/test 100 50 50 %s%% /\n' "$percent"
SCRIPT
  chmod +x "$target"
}

# 执行 ops-check 的全绿和参数错误模拟。
# 参数: $1 fake bin 目录；$2 测试根目录。
# 返回: 所有断言通过时返回 0。
run_ops_check_simulation() {
  local fake_bin="$1"
  local workspace="$2"
  local backup_dir="$workspace/backups"
  local persistent_backup_dir="$workspace/persistent-backups"
  local env_file="$workspace/ops.env"
  local backup_file="$persistent_backup_dir/code_review_20990101_000000.sql.gz"
  local stale_lexical_backup="$persistent_backup_dir/code_review_pre018_20990101.sql.gz"
  local output_file="$workspace/ops.json"
  local error_file="$workspace/ops-error.json"
  local degraded_file="$workspace/ops-degraded.json"
  local exit_code

  mkdir -p "$persistent_backup_dir"
  ln -s "$persistent_backup_dir" "$backup_dir"
  cat > "$env_file" <<'ENV'
APP_DOMAIN=example.test
MYSQL_ROOT_PASSWORD=RootCredentialForTests2026Alpha01
MYSQL_PASSWORD=AppCredentialForTests2026Beta002
ENV
  printf '%s\n' 'CREATE TABLE healthcheck(id INT);' | gzip -c > "$backup_file"
  printf '%s\n' 'stale backup without checksum' | gzip -c > "$stale_lexical_backup"
  touch -t 202001010000 "$stale_lexical_backup"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$backup_file" > "$backup_file.sha256"
  else
    shasum -a 256 "$backup_file" > "$backup_file.sha256"
  fi

  : > "$workspace/docker-ops.log"
  PATH="$fake_bin:$PATH" \
    FAKE_DOCKER_LOG="$workspace/docker-ops.log" \
    FAKE_DF_PERCENT=50 \
    DEPLOY_ENV_FILE="$env_file" \
    BACKUP_DIR="$backup_dir" \
    BACKUP_MAX_AGE_HOURS=48 \
    OPS_DISK_MAX_PERCENT=100 \
    OPS_MEMORY_MAX_PERCENT=100 \
    OPS_HTTPS_REQUIRED=TRUE \
    ./ops-check.sh > "$output_file"

  python3 - "$output_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    payload = json.load(source)
assert payload["schema_version"] == 1
assert payload["status"] == "ok"
expected = {"containers", "disk", "memory", "backup", "alembic", "https"}
assert set(payload["checks"]) == expected
assert all(payload["checks"][name]["ok"] is True for name in expected)
assert payload["checks"]["containers"]["services"]["redis"] in {"healthy", "running"}
assert payload["checks"]["https"]["http_redirect_code"] == "308"
assert payload["checks"]["alembic"]["current"] == "009"
assert payload["checks"]["alembic"]["head"] == "009"
assert payload["checks"]["backup"]["file"] == "code_review_20990101_000000.sql.gz"
assert payload["can_continue"] is True
assert payload["checks"]["disk"]["status"] == "ok"
PY

  PATH="$fake_bin:$PATH" \
    FAKE_DOCKER_LOG="$workspace/docker-ops.log" \
    FAKE_DF_PERCENT=87 \
    DEPLOY_ENV_FILE="$env_file" \
    BACKUP_DIR="$backup_dir" \
    BACKUP_MAX_AGE_HOURS=48 \
    OPS_DISK_MAX_PERCENT=85 \
    OPS_DISK_CRITICAL_PERCENT=95 \
    OPS_MEMORY_MAX_PERCENT=100 \
    OPS_MEMORY_CRITICAL_PERCENT=100 \
    OPS_HTTPS_REQUIRED=FALSE \
    ./ops-check.sh > "$workspace/ops-resource-degraded.json"
  python3 - "$workspace/ops-resource-degraded.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    payload = json.load(source)
assert payload["status"] == "degraded"
assert payload["can_continue"] is True
assert payload["checks"]["disk"]["ok"] is False
assert payload["checks"]["disk"]["status"] == "degraded"
assert any(item["code"] == "disk_cleanup_review" for item in payload["actions"])
PY

  PATH="$fake_bin:$PATH" \
    FAKE_DOCKER_LOG="$workspace/docker-ops.log" \
    FAKE_DF_PERCENT=85 \
    DEPLOY_ENV_FILE="$env_file" \
    BACKUP_DIR="$backup_dir" \
    BACKUP_MAX_AGE_HOURS=48 \
    OPS_DISK_MAX_PERCENT=85 \
    OPS_DISK_CRITICAL_PERCENT=95 \
    OPS_MEMORY_MAX_PERCENT=100 \
    OPS_MEMORY_CRITICAL_PERCENT=100 \
    OPS_HTTPS_REQUIRED=FALSE \
    ./ops-check.sh > "$workspace/ops-resource-warning-boundary.json"
  python3 - "$workspace/ops-resource-warning-boundary.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    payload = json.load(source)
assert payload["status"] == "degraded"
assert payload["can_continue"] is True
assert payload["checks"]["disk"]["status"] == "degraded"
PY

  set +e
  PATH="$fake_bin:$PATH" \
    FAKE_DOCKER_LOG="$workspace/docker-ops.log" \
    FAKE_DF_PERCENT=95 \
    DEPLOY_ENV_FILE="$env_file" \
    BACKUP_DIR="$backup_dir" \
    BACKUP_MAX_AGE_HOURS=48 \
    OPS_DISK_MAX_PERCENT=85 \
    OPS_DISK_CRITICAL_PERCENT=95 \
    OPS_MEMORY_MAX_PERCENT=100 \
    OPS_MEMORY_CRITICAL_PERCENT=100 \
    OPS_HTTPS_REQUIRED=FALSE \
    ./ops-check.sh > "$workspace/ops-resource-critical.json"
  exit_code=$?
  set -e
  [[ "$exit_code" -eq 1 ]] || {
    printf 'ops-check 临界磁盘退出码错误: %s\n' "$exit_code" >&2
    exit 1
  }
  python3 - "$workspace/ops-resource-critical.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    payload = json.load(source)
assert payload["status"] == "error"
assert payload["can_continue"] is False
assert payload["checks"]["disk"]["status"] == "error"
PY

  set +e
  PATH="$fake_bin:$PATH" \
    FAKE_DOCKER_LOG="$workspace/docker-ops.log" \
    DEPLOY_ENV_FILE="$env_file" \
    BACKUP_DIR="$workspace/missing-backups" \
    OPS_DISK_MAX_PERCENT=100 \
    OPS_MEMORY_MAX_PERCENT=100 \
    OPS_HTTPS_REQUIRED=FALSE \
    ./ops-check.sh > "$degraded_file"
  exit_code=$?
  set -e
  [[ "$exit_code" -eq 1 ]] || {
    printf 'ops-check 缺失备份退出码错误: %s\n' "$exit_code" >&2
    exit 1
  }
  python3 - "$degraded_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    payload = json.load(source)
assert payload["status"] == "error"
assert payload["checks"]["backup"]["ok"] is False
assert payload["checks"]["https"]["mode"] == "skipped"
assert payload["checks"]["https"]["ok"] is True
PY

  set +e
  ./ops-check.sh --unexpected > "$error_file"
  exit_code=$?
  set -e
  [[ "$exit_code" -eq 2 ]] || {
    printf 'ops-check 未知参数退出码错误: %s\n' "$exit_code" >&2
    exit 1
  }
  python3 - "$error_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    payload = json.load(source)
assert payload["status"] == "error"
assert payload["checks"]["preflight"]["ok"] is False
PY
}

# 执行 cleanup 默认 dry-run 与显式 apply 的保护模拟。
# 参数: $1 fake bin 目录；$2 测试根目录。
# 返回: 所有断言通过时返回 0。
run_cleanup_simulation() {
  local fake_bin="$1"
  local workspace="$2"
  local release_dir="$workspace/releases"
  local docker_log="$workspace/docker-cleanup.log"
  local dry_output="$workspace/cleanup-dry-run.log"
  local apply_output="$workspace/cleanup-apply.log"
  local invalid_output="$workspace/cleanup-invalid.log"
  local sandbox_env="$workspace/sandbox.env"
  local next_sandbox_env="$workspace/sandbox-next.env"
  local half_sandbox_env="$workspace/sandbox-half.env"
  local missing_sandbox_env="$workspace/sandbox-missing.env"
  local stateful_image_state="$workspace/playwright-images.state"
  local process_root="$workspace/proc"
  local process_pid=4242
  local state_count
  local tag_line
  local prune_line

  mkdir -p "$release_dir"
  cat > "$release_dir/current.env" <<'STATE'
BACKEND_RELEASE=be-current
FRONTEND_RELEASE=fe-current
STATE
  cat > "$release_dir/previous.env" <<'STATE'
BACKEND_RELEASE=be-previous
FRONTEND_RELEASE=fe-previous
STATE
  : > "$release_dir/rollback-from-20260708.env"
  : > "$release_dir/rollback-from-20260709.env"
  : > "$release_dir/rollback-from-20260710.env"
  cat > "$sandbox_env" <<'ENV'
PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright/mcp@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
PLAYWRIGHT_IMAGE_DIGEST=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
ENV
  cat > "$next_sandbox_env" <<'ENV'
PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright/mcp@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
PLAYWRIGHT_IMAGE_DIGEST=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
ENV
  cat > "$half_sandbox_env" <<'ENV'
PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright/mcp@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
ENV
  cat > "$missing_sandbox_env" <<'ENV'
PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright/mcp@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
PLAYWRIGHT_IMAGE_DIGEST=sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
ENV
  cat > "$fake_bin/systemctl" <<'SCRIPT'
#!/usr/bin/env bash
set -eu
[[ "$*" == *'show prism-sandbox-executor.service'* ]]
case "$*" in
  *MainPID*) printf '%s\n' "${FAKE_SANDBOX_PID:-0}" ;;
  *EnvironmentFiles*) printf '%s (ignore_errors=no)\n' "${FAKE_SANDBOX_ENV_FILE:?}" ;;
esac
SCRIPT
  chmod +x "$fake_bin/systemctl"

  if PATH="$fake_bin:$PATH" FAKE_DOCKER_LOG="$docker_log" RELEASE_STATE_DIR="$release_dir" \
    SANDBOX_ENV_FILE="$sandbox_env" ./cleanup.sh --cache-until 7d > "$invalid_output" 2>&1; then
    printf 'cleanup 接受了 Docker 不支持的天数时长\n' >&2
    exit 1
  fi
  assert_contains "$invalid_output" '缓存年龄格式必须类似 168h'

  : > "$docker_log"
  PATH="$fake_bin:$PATH" FAKE_DOCKER_LOG="$docker_log" RELEASE_STATE_DIR="$release_dir" \
    FAKE_SANDBOX_ENV_FILE="$sandbox_env" \
    ./cleanup.sh --keep-release-images 1 --keep-release-states 1 > "$workspace/cleanup-systemd.log"
  assert_contains "$workspace/cleanup-systemd.log" "使用沙箱 systemd 单元环境文件: $sandbox_env"
  assert_contains "$workspace/cleanup-systemd.log" 'DRY-RUN docker image tag mcr.microsoft.com/playwright/mcp@sha256:'
  assert_not_contains "$docker_log" 'image tag'

  : > "$docker_log"
  PATH="$fake_bin:$PATH" FAKE_DOCKER_LOG="$docker_log" RELEASE_STATE_DIR="$release_dir" \
    SANDBOX_ENV_FILE="$sandbox_env" \
    ./cleanup.sh --keep-release-images 1 --keep-release-states 1 > "$dry_output"
  assert_contains "$dry_output" 'DRY-RUN docker image rm prism-backend:be-old'
  assert_contains "$dry_output" 'DRY-RUN docker image rm prism-frontend:fe-old'
  assert_contains "$dry_output" 'DRY-RUN docker image tag mcr.microsoft.com/playwright/mcp@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa prism-sandbox-playwright:protected-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
  assert_contains "$dry_output" 'dry-run 完成'
  assert_not_contains "$docker_log" 'image rm'
  assert_not_contains "$docker_log" 'image tag'
  assert_not_contains "$docker_log" 'image prune'
  assert_not_contains "$docker_log" 'builder prune'
  state_count="$(find "$release_dir" -maxdepth 1 -type f -name 'rollback-from-*.env' | wc -l | tr -d ' ')"
  [[ "$state_count" == "3" ]] || {
    printf 'cleanup dry-run 意外删除历史状态\n' >&2
    exit 1
  }

  : > "$docker_log"
  PATH="$fake_bin:$PATH" FAKE_DOCKER_LOG="$docker_log" RELEASE_STATE_DIR="$release_dir" \
    SANDBOX_ENV_FILE="$sandbox_env" \
    ./cleanup.sh --apply --keep-release-images 1 --keep-release-states 1 > "$apply_output"
  assert_contains "$docker_log" 'image rm prism-backend:be-old'
  assert_contains "$docker_log" 'image rm prism-frontend:fe-old'
  assert_contains "$docker_log" 'image tag mcr.microsoft.com/playwright/mcp@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa prism-sandbox-playwright:protected-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
  assert_contains "$docker_log" 'image prune --force --filter until=168h'
  assert_contains "$docker_log" 'builder prune --force --filter until=168h'
  tag_line="$(grep -Fn 'image tag mcr.microsoft.com/playwright/mcp@sha256:' "$docker_log" | cut -d: -f1)"
  prune_line="$(grep -Fn 'image prune --force' "$docker_log" | cut -d: -f1)"
  [[ -n "$tag_line" && -n "$prune_line" && "$tag_line" -lt "$prune_line" ]] || {
    printf 'cleanup 未在 image prune 之前保护 Playwright 镜像\n' >&2
    exit 1
  }
  for protected in \
    prism-backend:be-current prism-backend:be-previous prism-backend:be-running prism-backend:be-new \
    prism-frontend:fe-current prism-frontend:fe-previous prism-frontend:fe-running prism-frontend:fe-new; do
    if grep -Fq -- "image rm $protected" "$docker_log"; then
      printf 'cleanup 删除了受保护镜像: %s\n' "$protected" >&2
      exit 1
    fi
  done
  state_count="$(find "$release_dir" -maxdepth 1 -type f -name 'rollback-from-*.env' | wc -l | tr -d ' ')"
  [[ "$state_count" == "1" ]] || {
    printf 'cleanup 历史状态保留数错误: %s\n' "$state_count" >&2
    exit 1
  }
  [[ -f "$release_dir/rollback-from-20260710.env" ]] || {
    printf 'cleanup 未保留最新历史状态\n' >&2
    exit 1
  }
  [[ ! -e "$release_dir/.maintenance.lock" ]] || {
    printf 'cleanup 未释放发布互斥锁\n' >&2
    exit 1
  }
  assert_contains "$apply_output" '受控清理完成'

  : > "$docker_log"
  PATH="$fake_bin:$PATH" FAKE_DOCKER_LOG="$docker_log" RELEASE_STATE_DIR="$release_dir" \
    SANDBOX_ENV_FILE="$next_sandbox_env" \
    ./cleanup.sh --apply --keep-release-images 999 --keep-release-states 999 > "$workspace/cleanup-next.log"
  assert_contains "$docker_log" 'image tag mcr.microsoft.com/playwright/mcp@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb prism-sandbox-playwright:protected-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'

  mkdir -p "$process_root/$process_pid"
  printf 'PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright/mcp@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\0PLAYWRIGHT_IMAGE_DIGEST=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\0' \
    > "$process_root/$process_pid/environ"
  cat > "$stateful_image_state" <<'STATE'
image sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
image sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
image sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
STATE
  : > "$docker_log"
  PATH="$fake_bin:$PATH" FAKE_DOCKER_LOG="$docker_log" RELEASE_STATE_DIR="$release_dir" \
    PROC_ROOT="$process_root" FAKE_SANDBOX_PID="$process_pid" FAKE_SANDBOX_ENV_FILE="$next_sandbox_env" \
    FAKE_DOCKER_SCENARIO=playwright_stateful FAKE_PLAYWRIGHT_STATE_FILE="$stateful_image_state" \
    ./cleanup.sh --apply --keep-release-images 999 --keep-release-states 999 > "$workspace/cleanup-stateful.log"
  assert_contains "$docker_log" 'image tag mcr.microsoft.com/playwright/mcp@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa prism-sandbox-playwright:protected-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
  assert_contains "$docker_log" 'image tag mcr.microsoft.com/playwright/mcp@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb prism-sandbox-playwright:protected-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
  assert_contains "$stateful_image_state" 'image sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
  assert_contains "$stateful_image_state" 'image sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
  assert_not_contains "$stateful_image_state" 'image sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'

  : > "$docker_log"
  if PATH="$fake_bin:$PATH" FAKE_DOCKER_LOG="$docker_log" RELEASE_STATE_DIR="$release_dir" \
    PROC_ROOT="$process_root" FAKE_SANDBOX_PID=4243 FAKE_SANDBOX_ENV_FILE="$next_sandbox_env" \
    ./cleanup.sh --apply > "$workspace/cleanup-unreadable-process.log" 2>&1; then
    printf 'cleanup 接受了不可读的运行中执行器进程环境\n' >&2
    exit 1
  fi
  assert_contains "$workspace/cleanup-unreadable-process.log" '运行中沙箱执行器的进程环境不可读'
  assert_not_contains "$docker_log" 'image rm'
  assert_not_contains "$docker_log" 'image tag'
  assert_not_contains "$docker_log" 'image prune'
  assert_not_contains "$docker_log" 'builder prune'

  : > "$docker_log"
  if PATH="$fake_bin:$PATH" FAKE_DOCKER_LOG="$docker_log" RELEASE_STATE_DIR="$release_dir" \
    SANDBOX_ENV_FILE="$half_sandbox_env" \
    ./cleanup.sh --apply > "$workspace/cleanup-half.log" 2>&1; then
    printf 'cleanup 接受了半配置的 Playwright 镜像\n' >&2
    exit 1
  fi
  assert_not_contains "$docker_log" 'image rm'
  assert_not_contains "$docker_log" 'image prune'
  assert_not_contains "$docker_log" 'builder prune'

  : > "$docker_log"
  if PATH="$fake_bin:$PATH" FAKE_DOCKER_LOG="$docker_log" RELEASE_STATE_DIR="$release_dir" \
    FAKE_DOCKER_SCENARIO=missing_playwright SANDBOX_ENV_FILE="$missing_sandbox_env" \
    ./cleanup.sh --apply > "$workspace/cleanup-missing.log" 2>&1; then
    printf 'cleanup 接受了缺失的 Playwright 镜像\n' >&2
    exit 1
  fi
  assert_not_contains "$docker_log" 'image rm'
  assert_not_contains "$docker_log" 'image tag'
  assert_not_contains "$docker_log" 'image prune'
  assert_not_contains "$docker_log" 'builder prune'
}

# 真实运行备份验证脚本，覆盖维护锁、强制边车文件和 024 缺表拒绝路径。
# 参数: $1 fake bin 目录；$2 测试根目录。
# 返回: 所有 fail-closed 断言通过时返回 0。
run_verify_backup_guard_simulation() {
  local fake_bin="$1"
  local workspace="$2/verify-backup"
  local env_file="$workspace/deploy.env"
  local backup_file="$workspace/code_review_20990102_000000.sql.gz"
  local checksum_file="$backup_file.sha256"
  local metadata_file="$backup_file.meta"
  local lock_dir="$workspace/.maintenance.lock"
  local docker_log="$workspace/docker.log"
  local output_file="$workspace/output.log"
  local checksum

  mkdir -p "$workspace"
  cat > "$env_file" <<'ENV'
APP_DOMAIN=example.test
MYSQL_ROOT_PASSWORD=RootCredentialForTests2026Alpha01
MYSQL_PASSWORD=AppCredentialForTests2026Beta002
ENV
  printf '%s\n' 'CREATE TABLE healthcheck(id INT);' | gzip -c > "$backup_file"
  : > "$docker_log"

  mkdir "$lock_dir"
  if env PATH="$fake_bin:$PATH" FAKE_DOCKER_LOG="$docker_log" \
    DEPLOY_ENV_FILE="$env_file" MAINTENANCE_LOCK_DIR="$lock_dir" \
    ./verify-backup.sh "$backup_file" > "$output_file" 2>&1; then
    printf 'verify-backup 未拒绝已占用的共享维护锁\n' >&2
    exit 1
  fi
  assert_contains "$output_file" '检测到并发任务或遗留锁'
  rmdir "$lock_dir"

  if env PATH="$fake_bin:$PATH" FAKE_DOCKER_LOG="$docker_log" \
    DEPLOY_ENV_FILE="$env_file" MAINTENANCE_LOCK_DIR="$lock_dir" \
    ./verify-backup.sh "$backup_file" > "$output_file" 2>&1; then
    printf 'verify-backup 未拒绝缺少 checksum 的备份\n' >&2
    exit 1
  fi
  assert_contains "$output_file" '备份缺少校验和文件'
  [[ ! -e "$lock_dir" ]] || {
    printf 'verify-backup checksum 失败后未释放维护锁\n' >&2
    exit 1
  }

  checksum="$(file_sha256 "$backup_file")"
  printf '%s  %s\n' "$checksum" "$(basename "$backup_file")" > "$checksum_file"
  if env PATH="$fake_bin:$PATH" FAKE_DOCKER_LOG="$docker_log" \
    DEPLOY_ENV_FILE="$env_file" MAINTENANCE_LOCK_DIR="$lock_dir" \
    ./verify-backup.sh "$backup_file" > "$output_file" 2>&1; then
    printf 'verify-backup 未拒绝缺少 meta 的备份\n' >&2
    exit 1
  fi
  assert_contains "$output_file" '备份缺少元数据文件'
  [[ ! -e "$lock_dir" ]] || {
    printf 'verify-backup meta 失败后未释放维护锁\n' >&2
    exit 1
  }

  cat > "$metadata_file" <<META
format_version=2
created_at_utc=20990102T000000Z
reason=test
git_sha=0000000000000000000000000000000000000000
alembic_revision=024
table_count=25
archive_row_count=0
archive_blob_bytes=0
sha256=$checksum
file=$(basename "$backup_file")
META
  if env PATH="$fake_bin:$PATH" FAKE_DOCKER_LOG="$docker_log" \
    FAKE_DOCKER_SCENARIO=verify_missing_024 DEPLOY_ENV_FILE="$env_file" \
    MAINTENANCE_LOCK_DIR="$lock_dir" VERIFY_MIN_TABLES=20 \
    ./verify-backup.sh "$backup_file" > "$output_file" 2>&1; then
    printf 'verify-backup 未拒绝 revision 024 缺少隔离归档表\n' >&2
    exit 1
  fi
  assert_contains "$output_file" '024 及以上备份缺少隔离源码归档表'
  assert_contains "$docker_log" 'CREATE DATABASE'
  assert_contains "$docker_log" 'DROP DATABASE'
  [[ ! -e "$lock_dir" ]] || {
    printf 'verify-backup 024 缺表失败后未释放维护锁\n' >&2
    exit 1
  }
}

# 真实运行备份脚本，注入归档统计漂移并断言不发布任何备份工件。
# 参数: $1 fake bin 目录；$2 测试根目录。
# 返回: 漂移被拒绝且临时文件、锁均清理时返回 0。
run_backup_archive_drift_simulation() {
  local fake_bin="$1"
  local workspace="$2/backup-drift"
  local output_dir="$workspace/output"
  local env_file="$workspace/deploy.env"
  local lock_dir="$workspace/.maintenance.lock"
  local docker_log="$workspace/docker.log"
  local state_file="$workspace/stats-count"
  local output_file="$workspace/output.log"

  mkdir -p "$workspace" "$output_dir"
  write_strong_database_test_env "$env_file"
  : > "$docker_log"
  if env PATH="$fake_bin:$PATH" FAKE_DOCKER_LOG="$docker_log" \
    FAKE_DOCKER_SCENARIO=backup_archive_drift FAKE_BACKUP_STATE="$state_file" \
    DEPLOY_ENV_FILE="$env_file" BACKUP_DIR="$output_dir" BACKUP_RETENTION_DAYS=0 \
    MAINTENANCE_LOCK_DIR="$lock_dir" \
    ./backup.sh --reason test-drift > "$output_file" 2>&1; then
    printf 'backup 未拒绝备份期间的隔离归档统计漂移\n' >&2
    exit 1
  fi
  assert_contains "$output_file" '隔离归档统计在备份期间发生变化'
  [[ "$(cat "$state_file")" == "2" ]] || {
    printf 'backup 未在 dump 前后各读取一次隔离归档统计\n' >&2
    exit 1
  }
  [[ -z "$(find "$output_dir" -maxdepth 1 -type f -print -quit)" ]] || {
    printf 'backup 统计漂移后发布了备份或边车文件\n' >&2
    exit 1
  }
  [[ ! -e "$lock_dir" ]] || {
    printf 'backup 统计漂移后未释放维护锁\n' >&2
    exit 1
  }
}

# 在临时副本中注入首次导入失败，验证 restore EXIT trap 自动回填安全备份。
# 参数: $1 测试根目录。
# 返回: 回填、服务恢复、嵌套锁和锁释放断言全部通过时返回 0。
run_restore_failure_simulation() {
  local workspace="$1/restore-failure"
  local fake_bin="$workspace/bin"
  local target_backup="$workspace/target.sql.gz"
  local lock_dir="$workspace/.maintenance.lock"
  local docker_log="$workspace/docker.log"
  local child_log="$workspace/children.log"
  local state_file="$workspace/import-count"
  local output_file="$workspace/output.log"
  local exit_code

  mkdir -p "$workspace/lib" "$fake_bin"
  cp restore.sh "$workspace/restore.sh"
  cp lib/common.sh "$workspace/lib/common.sh"
  write_restore_fake_docker "$fake_bin/docker"
  write_strong_database_test_env "$workspace/.env"
  printf '%s\n' 'CREATE TABLE restored(id INT);' | gzip -c > "$target_backup"
  : > "$docker_log"
  : > "$child_log"

  cat > "$workspace/verify-backup.sh" <<'SCRIPT'
#!/usr/bin/env bash
set -eu
[[ "${PRISM_MAINTENANCE_LOCK_HELD:-0}" == "1" ]]
[[ -d "${MAINTENANCE_LOCK_DIR:?}" ]]
printf 'verify:%s\n' "$1" >> "${FAKE_RESTORE_CHILD_LOG:?}"
SCRIPT
  cat > "$workspace/backup.sh" <<'SCRIPT'
#!/usr/bin/env bash
set -eu
[[ "${PRISM_MAINTENANCE_LOCK_HELD:-0}" == "1" ]]
[[ -d "${MAINTENANCE_LOCK_DIR:?}" ]]
safety_backup="$PWD/safety.sql.gz"
printf '%s\n' 'CREATE TABLE safety(id INT);' | gzip -c > "$safety_backup"
printf 'backup\n' >> "${FAKE_RESTORE_CHILD_LOG:?}"
printf '%s\n' "$safety_backup"
SCRIPT
  chmod +x "$workspace/restore.sh" "$workspace/verify-backup.sh" "$workspace/backup.sh"

  set +e
  env PATH="$fake_bin:$PATH" DEPLOY_ENV_FILE=.env \
    MAINTENANCE_LOCK_DIR="$lock_dir" BACKEND_HEALTH_TIMEOUT=1 \
    FAKE_DOCKER_LOG="$docker_log" FAKE_RESTORE_STATE="$state_file" \
    FAKE_RESTORE_CHILD_LOG="$child_log" \
    "$workspace/restore.sh" "$target_backup" --confirm RESTORE_PRODUCTION \
    > "$output_file" 2>&1
  exit_code=$?
  set -e

  [[ "$exit_code" == "42" ]] || {
    printf 'restore 故障注入退出码错误: %s\n' "$exit_code" >&2
    exit 1
  }
  assert_contains "$output_file" '恢复事务失败，正在回填事前安全备份'
  assert_contains "$output_file" '已回填事前数据并恢复 Backend'
  [[ "$(cat "$state_file")" == "2" ]] || {
    printf 'restore 未执行目标导入和安全备份回填两次导入\n' >&2
    exit 1
  }
  [[ "$(grep -Fc -- 'compose stop backend' "$docker_log")" == "2" ]] || {
    printf 'restore 回填前未再次停止 Backend\n' >&2
    exit 1
  }
  assert_contains "$docker_log" 'compose up -d --no-deps backend'
  [[ "$(grep -c '^verify:' "$child_log")" == "2" ]] || {
    printf 'restore 未在共享锁内验证目标与安全备份\n' >&2
    exit 1
  }
  [[ "$(grep -c '^backup$' "$child_log")" == "1" ]] || {
    printf 'restore 未在共享锁内创建一次安全备份\n' >&2
    exit 1
  }
  [[ ! -e "$lock_dir" ]] || {
    printf 'restore 自动回填后未释放维护锁\n' >&2
    exit 1
  }
}

# 注入应用切换后的冒烟失败，验证 deploy 显式失败路径必定进入应用回滚。
# 参数: $1 测试根目录。
# 返回: 回滚被调用、pending 被保留且锁释放时返回 0。
run_deploy_failure_rollback_simulation() {
  local workspace="$1/deploy-failure"
  local repo="$workspace/repo"
  local fake_bin="$workspace/bin"
  local state_dir="$workspace/release-state"
  local lock_dir="$workspace/.maintenance.lock"
  local backup_file="$workspace/pre-deploy.sql.gz"
  local docker_log="$workspace/docker.log"
  local rollback_log="$workspace/rollback.log"
  local output_file="$workspace/output.log"
  local release_sha
  local exit_code

  mkdir -p "$repo/deploy/lib" "$repo/backend" "$fake_bin" "$state_dir"
  cp deploy.sh "$repo/deploy/deploy.sh"
  cp lib/common.sh "$repo/deploy/lib/common.sh"
  printf '%s\n' '3.7.0' > "$repo/VERSION"
  printf '%s\n' 'test database' > "$repo/backend/GeoLite2-City.mmdb"
  write_strong_database_test_env "$repo/deploy/.env"
  printf 'GEOLITE_DB_HOST_PATH=%s\n' "$repo/backend/GeoLite2-City.mmdb" >> "$repo/deploy/.env"
  printf '%s\n' 'services: {}' > "$repo/deploy/docker-compose.yml"
  printf '%s\n' 'backup' | gzip -c > "$backup_file"

  cat > "$repo/deploy/backup.sh" <<SCRIPT
#!/usr/bin/env bash
printf '%s\n' '$backup_file'
SCRIPT
  cat > "$repo/deploy/verify-backup.sh" <<'SCRIPT'
#!/usr/bin/env bash
exit 0
SCRIPT
  cat > "$repo/deploy/sync-frontend-assets.sh" <<'SCRIPT'
#!/usr/bin/env bash
exit 0
SCRIPT
  cat > "$repo/deploy/rollback.sh" <<'SCRIPT'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_ROLLBACK_LOG:?}"
exit 0
SCRIPT
  chmod +x "$repo/deploy/"*.sh

  cat > "$fake_bin/docker" <<'SCRIPT'
#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "${FAKE_DOCKER_LOG:?}"
if [[ "${1:-}" == "compose" ]]; then
  shift
  if [[ "${1:-}" == "--env-file" ]]; then
    env_file="$2"
    shift 2
    if [[ "${1:-}" == "config" && "${2:-}" == "--environment" ]]; then
      awk '/^(MYSQL_ROOT_PASSWORD|MYSQL_PASSWORD)=/ {print}' "$env_file"
    fi
    exit 0
  fi
  case "${1:-}" in
    version|build|up) exit 0 ;;
    ps) [[ "${2:-}" == "-q" ]] && printf 'cid-%s\n' "${3:-unknown}" ;;
    exec)
      case "$*" in
        *'SELECT version_num FROM alembic_version'*) printf '%s\n' '045' ;;
      esac
      ;;
    run)
      case "$*" in
        *'alembic heads'*) printf '%s\n' '045 (head)' ;;
        *'alembic current'*) printf '%s\n' '045' ;;
      esac
      ;;
  esac
  exit 0
fi
case "${1:-}" in
  inspect) printf '%s\n' 'healthy' ;;
  image) exit 0 ;;
  run) exit 0 ;;
esac
SCRIPT
  cat > "$fake_bin/curl" <<'SCRIPT'
#!/usr/bin/env bash
# 应用已切换后让 backend 冒烟失败，触发自动回滚。
exit 22
SCRIPT
  chmod +x "$fake_bin/docker" "$fake_bin/curl"

  git -C "$repo" init -q
  git -C "$repo" add VERSION backend deploy
  git -C "$repo" -c user.name=PrismTest -c user.email=prism@example.test commit -qm init
  release_sha="$(git -C "$repo" rev-parse HEAD)"
  cat > "$state_dir/current.env" <<STATE
RELEASE_SHA=$release_sha
BACKEND_RELEASE=previous-backend
FRONTEND_RELEASE=previous-frontend
TARGET=all
BACKUP_FILE=none
ALEMBIC_REVISION=045
STATE

  : > "$docker_log"
  : > "$rollback_log"
  set +e
  env PATH="$fake_bin:$PATH" DEPLOY_ENV_FILE=.env RELEASE_STATE_DIR="$state_dir" \
    MAINTENANCE_LOCK_DIR="$lock_dir" FAKE_DOCKER_LOG="$docker_log" \
    FAKE_ROLLBACK_LOG="$rollback_log" BACKEND_HEALTH_TIMEOUT=1 \
    "$repo/deploy/deploy.sh" all --revision "$release_sha" > "$output_file" 2>&1
  exit_code=$?
  set -e

  [[ "$exit_code" == "1" ]] || {
    printf 'deploy 故障注入退出码错误: %s\n' "$exit_code" >&2
    exit 1
  }
  assert_contains "$output_file" 'Backend 冒烟失败'
  assert_contains "$output_file" '应用自动回滚完成'
  assert_contains "$rollback_log" '--from-deploy-failure'
  [[ -f "$state_dir/pending.env" ]] || {
    printf 'deploy 失败后未保留 pending 状态\n' >&2
    exit 1
  }
  [[ ! -e "$lock_dir" ]] || {
    printf 'deploy 自动回滚后未释放维护锁\n' >&2
    exit 1
  }
}

# 验证沙箱镜像固化脚本默认不写入，--apply 才原子更新五个 digest。
# 参数: $1 fake bin 目录；$2 测试根目录。
# 返回: dry-run 与 apply 语义正确时返回 0。
run_sandbox_pin_simulation() {
  local fake_bin="$1"
  local workspace="$2"
  local profiles_file="$workspace/sandbox-profiles.json"
  local docker_log="$workspace/docker-sandbox-pin.log"
  local before_checksum
  local after_checksum

  cp sandbox/profiles.json "$profiles_file"
  : > "$docker_log"
  before_checksum="$(file_sha256 "$profiles_file")"
  PATH="$fake_bin:$PATH" FAKE_DOCKER_LOG="$docker_log" \
    sandbox/pin-profiles.sh --profiles "$profiles_file" > "$workspace/sandbox-pin-dry.log"
  after_checksum="$(file_sha256 "$profiles_file")"
  [[ "$before_checksum" == "$after_checksum" ]] || {
    printf 'sandbox pin dry-run 意外修改 profiles.json\n' >&2
    exit 1
  }
  assert_contains "$workspace/sandbox-pin-dry.log" 'dry-run：未写入 profiles.json'

  PATH="$fake_bin:$PATH" FAKE_DOCKER_LOG="$docker_log" \
    sandbox/pin-profiles.sh --apply --profiles "$profiles_file" > "$workspace/sandbox-pin-apply.log"
  assert_contains "$workspace/sandbox-pin-apply.log" '已原子更新 profiles.json'
  python3 - "$profiles_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    profiles = json.load(source)["profiles"]
expected = {
    "python": "sha256:" + "1" * 64,
    "node": "sha256:" + "2" * 64,
    "java": "sha256:" + "3" * 64,
    "go": "sha256:" + "4" * 64,
    "php": "sha256:" + "5" * 64,
}
assert {language: profile["digest"] for language, profile in profiles.items()} == expected
PY
}

# 静态验证 systemd 模板并渲染占位符。
# 参数: $1 输出目录。
# 返回: 模板完整且渲染成功时返回 0。
verify_systemd_templates() {
  local output_dir="$1"
  local escaped_deploy_dir
  local template
  local output

  assert_contains systemd/prism-backup.service.in 'ExecStart=@DEPLOY_DIR@/backup.sh --reason scheduled'
  assert_contains systemd/prism-verify-backup.service.in 'ExecStart=@DEPLOY_DIR@/verify-backup.sh'
  assert_contains systemd/prism-ops-check.service.in 'ExecStart=@DEPLOY_DIR@/ops-check.sh'
  assert_contains systemd/prism-ops-executor.service.in 'Group=prism-ops'
  assert_contains systemd/prism-ops-executor.service.in 'RuntimeDirectoryMode=0770'
  assert_contains systemd/prism-ops-executor.service.in 'RuntimeDirectoryPreserve=restart'
  assert_contains systemd/prism-ops-executor.service.in 'UMask=0007'
  assert_contains systemd/install.sh 'groupadd --system --gid 991 prism-ops'
  assert_contains systemd/prism-backup.timer 'OnCalendar=*-*-* 02:15:00'
  assert_contains systemd/prism-verify-backup.timer 'OnCalendar=Sun *-*-* 03:15:00'
  assert_contains systemd/prism-ops-check.timer 'OnUnitActiveSec=5m'
  for template in systemd/*.timer; do
    assert_contains "$template" 'Persistent=true'
  done
  for template in systemd/*.service.in; do
    assert_contains "$template" 'NoNewPrivileges=true'
    assert_not_contains "$template" 'MYSQL_ROOT_PASSWORD'
  done

  mkdir -p "$output_dir"
  escaped_deploy_dir="$(printf '%s' "$PWD" | sed 's/[&|]/\\&/g')"
  for template in systemd/*.service.in; do
    output="$output_dir/$(basename "${template%.in}")"
    sed "s|@DEPLOY_DIR@|$escaped_deploy_dir|g" "$template" > "$output"
    assert_not_contains "$output" '@DEPLOY_DIR@'
    assert_contains "$output" "WorkingDirectory=$PWD"
  done
}

for script in \
  lib/common.sh backup.sh verify-backup.sh restore.sh deploy.sh rollback.sh \
  cleanup.sh ops-check.sh issue-cert.sh renew-cert.sh systemd/install.sh \
  sandbox/install.sh sandbox/pin-profiles.sh; do
  bash -n "$script"
done

./backup.sh --help >/dev/null
./verify-backup.sh --help >/dev/null
./restore.sh --help >/dev/null
./deploy.sh --help >/dev/null
./rollback.sh --help >/dev/null
./cleanup.sh --help >/dev/null
./ops-check.sh --help >/dev/null
./issue-cert.sh --help >/dev/null
./renew-cert.sh --help >/dev/null
./systemd/install.sh --help >/dev/null
systemd_preview="$(./systemd/install.sh --deploy-dir "$PWD" \
  --unit-dir "${TMPDIR:-/tmp}/prism-systemd-preview")"
printf '%s\n' "$systemd_preview" | grep -Fq 'DRY-RUN'
sandbox_preview="$(./sandbox/install.sh --deploy-dir "$PWD" \
  --unit-dir "${TMPDIR:-/tmp}/prism-sandbox-systemd-preview")"
printf '%s\n' "$sandbox_preview" | grep -Fq 'DRY-RUN'
printf '%s\n' "$sandbox_preview" | grep -Fq '未执行生产预检'

assert_contains backup.sh '--single-transaction'
assert_contains backup.sh 'sha256_file'
assert_contains docker-compose.yml "-e 'SELECT 1' >/dev/null"
assert_not_contains docker-compose.yml 'mysqladmin ping'
assert_contains lib/common.sh 'validate_database_credentials "$env_file"'
assert_contains lib/common.sh 'config --environment'
assert_contains lib/common.sh 'MYSQL_ROOT_PASSWORD 必须至少 32 个字符'
assert_contains backup.sh 'archive_stats_before="$(read_archive_stats)"'
assert_contains backup.sh 'archive_stats_after="$(read_archive_stats)"'
assert_contains backup.sh '隔离归档统计在备份期间发生变化'
assert_contains backup.sh '最后发布 .sql.gz 完成标志'
assert_contains verify-backup.sh 'prism_verify_'
assert_contains verify-backup.sh '备份缺少校验和文件'
assert_contains verify-backup.sh '备份缺少元数据文件'
assert_contains verify-backup.sh '024 及以上备份缺少隔离源码归档表'
assert_contains restore.sh 'RESTORE_PRODUCTION'
assert_contains restore.sh 'run_admin_alembic upgrade head'
assert_contains restore.sh 'trap on_restore_exit EXIT'
assert_contains restore.sh 'restore_database_file "$safety_backup"'
assert_contains restore.sh '生产保持维护状态'
for maintenance_script in \
  backup.sh verify-backup.sh restore.sh deploy.sh rollback.sh cleanup.sh; do
  assert_contains "$maintenance_script" 'maintenance_lock_path'
done
assert_not_contains backup.sh '.backup.lock'
assert_not_contains restore.sh '.restore.lock'
assert_contains deploy.sh '--revision'
assert_contains deploy.sh 'run_admin_alembic upgrade head'
assert_contains deploy.sh 'assert_alembic_at_head'
assert_contains deploy.sh 'backup.sh --reason pre_deploy'
assert_contains deploy.sh 'smoke_backend'
assert_contains deploy.sh "smoke_https \"\$desired_backend\""
assert_contains deploy.sh "当前生产版本必须使用 all 发布"
assert_contains deploy.sh 'rollback.sh'
assert_not_contains deploy.sh 'reset --hard'
assert_not_contains deploy.sh 'docker image prune'
assert_contains rollback.sh 'ROLLBACK_APPLICATION'
assert_contains cleanup.sh '默认 dry-run'
assert_contains cleanup.sh 'run_mutation docker image prune'
assert_contains cleanup.sh 'run_mutation docker builder prune'
assert_contains cleanup.sh 'maintenance_lock_path'
assert_not_contains cleanup.sh 'docker system prune'
assert_not_contains cleanup.sh 'docker volume rm'
assert_not_contains cleanup.sh 'down -v'
assert_contains ops-check.sh '"schema_version": 1'
assert_contains ops-check.sh 'current_alembic_revision'
assert_contains ops-check.sh 'http_redirect_code'
assert_contains prism_ops_executor.py 'certbot" / "conf" / "live"'
assert_contains RELEASE_CHECKLIST.md './deploy.sh backend --revision <FULL_COMMIT_SHA>'
assert_contains RELEASE_CHECKLIST.md './rollback.sh all --confirm ROLLBACK_APPLICATION'
assert_not_contains RELEASE_CHECKLIST.md '--target'
assert_contains docker-compose.yml "prism-backend:\${BACKEND_RELEASE:-local}"
assert_contains docker-compose.yml "prism-frontend:\${FRONTEND_RELEASE:-local}"
assert_contains docker-compose.yml 'expose:'
assert_contains lib/common.sh 'run_admin_alembic()'
assert_contains lib/common.sh "--network \"container:\$mysql_container\""
assert_contains lib/common.sh '-e DB_USER=root'
assert_not_contains lib/common.sh 'log_bin_trust_function_creators'
assert_contains docker-compose.yml 'CLAMAV_HOST: clamav'
assert_not_contains docker-compose.yml '"3310:3310"'
assert_contains sandbox/docker-compose.build.yml 'PYTHON_BASE_IMAGE_DIGEST:?PYTHON_BASE_IMAGE_DIGEST must be the reviewed 64-hex digest'
assert_contains sandbox/docker-compose.build.yml 'NODE_BASE_IMAGE_DIGEST:?NODE_BASE_IMAGE_DIGEST must be the reviewed 64-hex digest'
assert_contains sandbox/docker-compose.build.yml 'JAVA_BASE_IMAGE_DIGEST:?JAVA_BASE_IMAGE_DIGEST must be the reviewed 64-hex digest'
assert_contains sandbox/docker-compose.build.yml 'GO_BASE_IMAGE_DIGEST:?GO_BASE_IMAGE_DIGEST must be the reviewed 64-hex digest'
assert_contains sandbox/docker-compose.build.yml 'PHP_BASE_IMAGE_DIGEST:?PHP_BASE_IMAGE_DIGEST must be the reviewed 64-hex digest'
for dockerfile in \
  sandbox/Dockerfile.python sandbox/Dockerfile.node sandbox/Dockerfile.java \
  sandbox/Dockerfile.go sandbox/Dockerfile.php; do
  [[ "$(sed -n '1p' "$dockerfile")" == 'ARG BASE_IMAGE' ]] || {
    printf 'Dockerfile 未禁用 BASE_IMAGE 默认值: %s\n' "$dockerfile" >&2
    exit 1
  }
  assert_not_contains "$dockerfile" 'ARG BASE_IMAGE='
  assert_contains "$dockerfile" "image_digest=\"\${BASE_IMAGE##*@sha256:}\""
  assert_contains "$dockerfile" "[ \"\${#image_digest}\" -eq 64 ]"
  assert_contains "$dockerfile" '*[!0-9a-f]*) exit 1'
done
assert_contains sandbox/install.sh 'systemd-analyze verify'
assert_contains sandbox/install.sh 'service 模板必须由 root 拥有且权限只能为 0444 或 0644'
assert_contains sandbox/install.sh '必须禁用 BASE_IMAGE 默认值并校验 name@sha256 摘要'
assert_contains sandbox/install.sh 'systemd 单元启动失败，已回滚旧单元'
assert_contains sandbox/install.sh '沙箱执行器 UDS Bearer health 或 browser_blackbox 未达到 ready，已回滚旧单元'
assert_contains sandbox/install.sh 'SANDBOX_ALLOW_RUNC_LOCAL_DEVELOPMENT'
assert_contains sandbox/install.sh 'SANDBOX_DEFAULT_TTL_SECONDS'
assert_contains sandbox/install.sh '主 deploy/.env 的 SANDBOX_ENABLED 必须为 true'
assert_contains sandbox/install.sh '主 deploy/.env 与 sandbox/.env 的 UDS 路径不一致'
assert_contains sandbox/install.sh '沙箱执行器脚本无法通过 Python 编译检查'
assert_contains sandbox/install.sh 'PLAYWRIGHT_IMAGE 必须以同一不可变摘要固定'
assert_contains sandbox/install.sh 'Playwright 固定代理脚本无法通过 Python 编译检查'
assert_contains sandbox/install.sh 'result.get("browser_blackbox", {}).get("ready")'
assert_contains sandbox/install.sh "--config \"\$health_config\""
assert_not_contains sandbox/install.sh "-H \"Authorization: Bearer \$sandbox_token\""
assert_contains sandbox/install.sh 'UDS Bearer health 或 browser_blackbox 未达到 ready'
assert_contains sandbox/README.md '/opt/prism/runner.sh proxy'
assert_contains sandbox/README.md 'chown root:root deploy/prism-sandbox-executor.service'
assert_not_contains sandbox/README.md 'docker exec curl'
assert_contains sandbox/pin-profiles.sh 'dry-run：未写入 profiles.json'
assert_contains sandbox/pin-profiles.sh 'os.replace(temporary, profiles_path)'
assert_contains sandbox/worker-gateway.nginx.conf.example 'ssl_protocols TLSv1.2 TLSv1.3;'
assert_contains sandbox/worker-gateway.nginx.conf.example 'server unix:/var/lib/prism-sandbox/agent.sock;'
assert_contains sandbox/worker-gateway.nginx.conf.example "proxy_set_header Authorization \$http_authorization;"
# 沙箱 socket 直接落在持久 StateDirectory,执行器 unlink+重建同一路径,
# 挂载点跟随文件路径,执行器重启后容器内 socket 引用依然有效;容器内路径
# 与宿主一致(/var/lib/prism-sandbox)。
assert_not_contains docker-compose.yml '/run/prism-sandbox:/run/prism-sandbox'
assert_contains docker-compose.yml '/var/lib/prism-sandbox:/var/lib/prism-sandbox:ro'
assert_contains docker-compose.yml 'SANDBOX_EXECUTOR_SOCKET: /var/lib/prism-sandbox/agent.sock'
assert_contains prism-sandbox-executor.service 'StateDirectoryMode=0770'
assert_contains prism-sandbox-executor.service 'Environment=SANDBOX_EXECUTOR_SOCKET=/var/lib/prism-sandbox/agent.sock'
assert_not_contains prism-sandbox-executor.service 'ExecStartPost='
assert_contains sandbox/install.sh 'install -d -m 0770 -o prism-sandbox -g prism-sandbox /var/lib/prism-sandbox'
assert_contains sandbox/install.sh 'install -d -m 0770 -o prism-sandbox -g prism-sandbox /var/lib/prism-sandbox'
assert_contains ../backend/Dockerfile 'install -d -o 10001 -g 991 -m 0750 /app/logs'
assert_contains ../backend/Dockerfile.runtime 'ARG RUNTIME_BASE'
assert_contains ../backend/Dockerfile.runtime 'find /app -mindepth 1 -maxdepth 1'
assert_contains ../backend/Dockerfile.runtime 'COPY --chown=10001:991 . /app'
assert_contains ../frontend/nginx.conf.template "return 308 https://\$host\$request_uri;"
assert_contains ../frontend/nginx.conf.template 'location = /openapi.json'
assert_contains ../frontend/nginx.conf.template 'location = /metrics'
assert_contains ../frontend/nginx.conf.template 'location = /readyz'
assert_contains ../frontend/nginx.conf.template 'proxy_pass http://backend:8000/readyz;'
assert_contains ../frontend/nginx.conf.template 'proxy_cache off;'
assert_not_contains ../frontend/nginx.conf.template 'location = /readyz { return 404; }'
assert_contains ../frontend/Dockerfile.runtime 'COPY nginx.conf.template /etc/nginx/templates/default.conf.template'
assert_contains lib/common.sh '"https://$domain/readyz"'
assert_contains lib/common.sh '{\"status\":\"ready\",\"release\":\"$expected_release\"}'
assert_contains lib/common.sh '-e MALWARE_SCAN_FAIL_CLOSED=true'
assert_contains deploy.sh 'release_image_exists prism-frontend "$current_frontend"'
assert_contains docker-compose.yml '--general-log=0'
assert_not_contains docker-compose.yml '--general-log=1'

DEPLOY_ENV_FILE=.env.example \
  docker compose --env-file .env.example config --quiet

digest_placeholder='0000000000000000000000000000000000000000000000000000000000000000'
PYTHON_BASE_IMAGE='python:3.11-slim' PYTHON_BASE_IMAGE_DIGEST="$digest_placeholder" \
  NODE_BASE_IMAGE='node:20-bookworm-slim' NODE_BASE_IMAGE_DIGEST="$digest_placeholder" \
  JAVA_BASE_IMAGE='eclipse-temurin:17-jdk-jammy' JAVA_BASE_IMAGE_DIGEST="$digest_placeholder" \
  GO_BASE_IMAGE='golang:1.23-bookworm' GO_BASE_IMAGE_DIGEST="$digest_placeholder" \
  PHP_BASE_IMAGE='php:8.3-cli-bookworm' PHP_BASE_IMAGE_DIGEST="$digest_placeholder" \
  docker compose -f sandbox/docker-compose.build.yml config --quiet

test_root="$(mktemp -d "${TMPDIR:-/tmp}/prism-deploy-tests.XXXXXX")"
trap cleanup_test_workspace EXIT
fake_bin="$test_root/bin"
mkdir -p "$fake_bin"
write_fake_docker "$fake_bin/docker"
write_fake_curl "$fake_bin/curl"
write_fake_df "$fake_bin/df"
run_database_credential_validation "$test_root"
run_backup_archive_drift_simulation "$fake_bin" "$test_root"
run_verify_backup_guard_simulation "$fake_bin" "$test_root"
run_restore_failure_simulation "$test_root"
run_deploy_failure_rollback_simulation "$test_root"
run_ops_check_simulation "$fake_bin" "$test_root"
run_cleanup_simulation "$fake_bin" "$test_root"
run_sandbox_pin_simulation "$fake_bin" "$test_root"
verify_systemd_templates "$test_root/rendered-units"

printf 'deploy shell and operations tests: PASS\n'
