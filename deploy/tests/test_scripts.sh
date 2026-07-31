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
        ;;
      ps)
        [[ "${2:-}" == "-q" ]] && printf 'cid-%s\n' "${3:-unknown}"
        ;;
      exec)
        case "$*" in
          *"backend alembic heads"*) printf '%s\n' '009 (head)' ;;
          *" mysql "*) printf '%s\n' '009' ;;
        esac
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
        case "$last" in
          prism-backend:be-running) printf '%s\n' 'sha256:running-backend' ;;
          prism-frontend:fe-running) printf '%s\n' 'sha256:running-frontend' ;;
          *) printf 'sha256:%s\n' "$(printf '%s' "$last" | tr ':/' '--')" ;;
        esac
        ;;
      rm|prune)
        ;;
    esac
    ;;
  builder)
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
  printf '%s\n' 'APP_DOMAIN=example.test' > "$env_file"
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
  local state_count

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

  : > "$docker_log"
  PATH="$fake_bin:$PATH" FAKE_DOCKER_LOG="$docker_log" RELEASE_STATE_DIR="$release_dir" \
    ./cleanup.sh --keep-release-images 1 --keep-release-states 1 > "$dry_output"
  assert_contains "$dry_output" 'DRY-RUN docker image rm prism-backend:be-old'
  assert_contains "$dry_output" 'DRY-RUN docker image rm prism-frontend:fe-old'
  assert_contains "$dry_output" 'dry-run 完成'
  assert_not_contains "$docker_log" 'image rm'
  assert_not_contains "$docker_log" 'image prune'
  assert_not_contains "$docker_log" 'builder prune'
  state_count="$(find "$release_dir" -maxdepth 1 -type f -name 'rollback-from-*.env' | wc -l | tr -d ' ')"
  [[ "$state_count" == "3" ]] || {
    printf 'cleanup dry-run 意外删除历史状态\n' >&2
    exit 1
  }

  : > "$docker_log"
  PATH="$fake_bin:$PATH" FAKE_DOCKER_LOG="$docker_log" RELEASE_STATE_DIR="$release_dir" \
    ./cleanup.sh --apply --keep-release-images 1 --keep-release-states 1 > "$apply_output"
  assert_contains "$docker_log" 'image rm prism-backend:be-old'
  assert_contains "$docker_log" 'image rm prism-frontend:fe-old'
  assert_contains "$docker_log" 'image prune --force --filter until=168h'
  assert_contains "$docker_log" 'builder prune --force --filter until=168h'
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
  [[ ! -e "$release_dir/.deploy.lock" ]] || {
    printf 'cleanup 未释放发布互斥锁\n' >&2
    exit 1
  }
  assert_contains "$apply_output" '受控清理完成'
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
  cleanup.sh ops-check.sh issue-cert.sh renew-cert.sh systemd/install.sh; do
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

assert_contains backup.sh '--single-transaction'
assert_contains backup.sh 'sha256_file'
assert_contains verify-backup.sh 'prism_verify_'
assert_contains restore.sh 'RESTORE_PRODUCTION'
assert_contains restore.sh 'alembic upgrade head'
assert_contains deploy.sh '--revision'
assert_contains deploy.sh 'assert_alembic_at_head'
assert_contains deploy.sh 'backup.sh --reason pre_deploy'
assert_contains deploy.sh 'smoke_backend'
assert_contains deploy.sh 'smoke_https "$desired_backend"'
assert_contains deploy.sh 'rollback.sh'
assert_not_contains deploy.sh 'reset --hard'
assert_not_contains deploy.sh 'docker image prune'
assert_contains rollback.sh 'ROLLBACK_APPLICATION'
assert_contains cleanup.sh '默认 dry-run'
assert_contains cleanup.sh 'run_mutation docker image prune'
assert_contains cleanup.sh 'run_mutation docker builder prune'
assert_contains cleanup.sh '.deploy.lock'
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
assert_contains docker-compose.yml 'prism-backend:${BACKEND_RELEASE:-local}'
assert_contains docker-compose.yml 'prism-frontend:${FRONTEND_RELEASE:-local}'
assert_contains docker-compose.yml 'expose:'
assert_contains docker-compose.yml 'CLAMAV_HOST: clamav'
assert_not_contains docker-compose.yml '"3310:3310"'
assert_contains ../backend/Dockerfile 'install -d -o 10001 -g 991 -m 0750 /app/logs'
assert_contains ../frontend/nginx.conf.template 'return 308 https://$host$request_uri;'
assert_contains ../frontend/nginx.conf.template 'location = /openapi.json'
assert_contains ../frontend/nginx.conf.template 'location = /metrics'

DEPLOY_ENV_FILE=.env.example \
  docker compose --env-file .env.example config --quiet

test_root="$(mktemp -d "${TMPDIR:-/tmp}/prism-deploy-tests.XXXXXX")"
trap cleanup_test_workspace EXIT
fake_bin="$test_root/bin"
mkdir -p "$fake_bin"
write_fake_docker "$fake_bin/docker"
write_fake_curl "$fake_bin/curl"
run_ops_check_simulation "$fake_bin" "$test_root"
run_cleanup_simulation "$fake_bin" "$test_root"
verify_systemd_templates "$test_root/rendered-units"

printf 'deploy shell and operations tests: PASS\n'
