#!/usr/bin/env bash
# 被 test_scripts.sh source：真实发布/回滚脚本配隔离命令替身，无生产连接。
run_deploy_failure_matrix() {
  local test_root="$1" scenario workspace rc expected_rollback expected_rc
  local old_sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  local new_sha=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
  for scenario in backup verify backend_build migration backend_up backend_health backend_smoke frontend_build frontend_up assets frontend_health https rollback_failed; do
    workspace="$test_root/failure-matrix-$scenario"
    mkdir -p "$workspace/repo/deploy/lib" "$workspace/repo/backend" "$workspace/bin" "$workspace/releases"
    cp deploy.sh rollback.sh "$workspace/repo/deploy/"
    cp lib/common.sh "$workspace/repo/deploy/lib/"
    printf '3.8.4\n' > "$workspace/repo/VERSION"
    printf 'isolated geolite fixture\n' > "$workspace/repo/backend/GeoLite2-City.mmdb"
    write_strong_database_test_env "$workspace/repo/deploy/.env"
    cat >> "$workspace/repo/deploy/.env" <<ENV
GEOLITE_DB_HOST_PATH=$workspace/repo/backend/GeoLite2-City.mmdb
APP_RELEASE=$old_sha
APP_VERSION=3.8.2
BACKEND_RELEASE=$old_sha
FRONTEND_RELEASE=$old_sha
ENV
    printf 'services: {}\n' > "$workspace/repo/deploy/docker-compose.yml"
    printf 'fixture\n' | gzip -c > "$workspace/backup.sql.gz"
    cat > "$workspace/releases/current.env" <<STATE
RELEASE_SHA=$old_sha
APP_VERSION=3.8.2
BACKEND_RELEASE=$old_sha
FRONTEND_RELEASE=$old_sha
TARGET=all
BACKUP_FILE=none
ALEMBIC_REVISION=048_ai_usage_attribution
STATE
    cp "$workspace/releases/current.env" "$workspace/original.env"
    cat > "$workspace/repo/deploy/backup.sh" <<'SCRIPT'
#!/usr/bin/env bash
[[ "$FAIL_SCENARIO" != backup ]] || exit 41
printf '%s/backup.sql.gz\n' "$FAKE_RELEASE_WORKSPACE"
SCRIPT
    cat > "$workspace/repo/deploy/verify-backup.sh" <<'SCRIPT'
#!/usr/bin/env bash
[[ "$FAIL_SCENARIO" != verify ]] || exit 42
SCRIPT
    cat > "$workspace/repo/deploy/sync-frontend-assets.sh" <<'SCRIPT'
#!/usr/bin/env bash
[[ "$FAIL_SCENARIO" != assets ]] || exit 43
SCRIPT
    write_release_fake_docker "$workspace/bin/docker-base"
    cat > "$workspace/bin/docker" <<'SCRIPT'
#!/usr/bin/env bash
set -eu
stage=''
case "$*" in
  'compose build backend') stage=backend_build ;;
  'compose build frontend') stage=frontend_build ;;
  *'upgrade head'*) stage=migration ;;
  'compose up -d --no-deps --no-build --pull never backend') stage=backend_up ;;
  'compose up -d --no-deps --no-build --pull never frontend') stage=frontend_up ;;
  'inspect --format {{if .State.Health}}'*'cid-backend') stage=backend_health ;;
  'inspect --format {{if .State.Health}}'*'cid-frontend') stage=frontend_health ;;
esac
if [[ "${APP_RELEASE:-}" == bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb && "$stage" == "$FAIL_SCENARIO" ]]; then
  printf 'injected:%s\n' "$stage" >> "$FAKE_DOCKER_LOG"
  case "$stage" in *_health) printf 'unhealthy\n'; exit 0 ;; esac
  exit 44
fi
if [[ "${APP_RELEASE:-}" == aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa && "$stage" == backend_up && "$FAIL_SCENARIO" == rollback_failed ]]; then
  printf 'injected:rollback_failed\n' >> "$FAKE_DOCKER_LOG"
  exit 45
fi
exec "$(dirname "$0")/docker-base" "$@"
SCRIPT
    cat > "$workspace/bin/curl" <<'SCRIPT'
#!/usr/bin/env bash
set -eu
if [[ "${APP_RELEASE:-}" == bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ]]; then
  case "$FAIL_SCENARIO:$*" in backend_smoke:*healthz*|rollback_failed:*healthz*|https:*https://*) exit 22 ;; esac
fi
case "$*" in
  *healthz*) printf '{"status":"ok","version":"%s","release":"%s"}' "$APP_VERSION" "$APP_RELEASE" ;;
  *readyz*) printf '{"status":"ready","version":"%s","release":"%s"}' "$APP_VERSION" "$APP_RELEASE" ;;
  *http://*) printf 308 ;;
esac
SCRIPT
    cat > "$workspace/bin/git" <<'SCRIPT'
#!/usr/bin/env bash
set -eu
case "$*" in
  *'rev-parse --git-dir'*) printf '.git\n' ;;
  *'rev-parse'*) printf 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n' ;;
  *'status --porcelain'*) exit 0 ;;
  *'show aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:VERSION'*) printf '3.8.2\n' ;;
  *'show bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:VERSION'*) printf '3.8.4\n' ;;
  *) exit 97 ;;
esac
SCRIPT
    chmod +x "$workspace/repo/deploy/"*.sh "$workspace/bin/"*
    : > "$workspace/docker.log"
    set +e
    env PATH="$workspace/bin:$PATH" FAIL_SCENARIO="$scenario" FAKE_RELEASE_SHA="$old_sha" \
      FAKE_RELEASE_VERSION=3.8.2 FAKE_ALEMBIC_REVISION=048_ai_usage_attribution \
      FAKE_RELEASE_WORKSPACE="$workspace" FAKE_DOCKER_LOG="$workspace/docker.log" \
      RELEASE_STATE_DIR="$workspace/releases" MAINTENANCE_LOCK_DIR="$workspace/lock" \
      DEPLOY_ENV_FILE=.env BACKEND_HEALTH_TIMEOUT=1 FRONTEND_HEALTH_TIMEOUT=1 \
      "$workspace/repo/deploy/deploy.sh" all --revision "$new_sha" > "$workspace/output.log" 2>&1
    rc=$?
    set -e
    expected_rc=1
    case "$scenario" in
      backup) expected_rc=41 ;;
      verify) expected_rc=42 ;;
      backend_build|migration|backend_up|frontend_build|frontend_up) expected_rc=44 ;;
      assets) expected_rc=43 ;;
    esac
    [[ "$rc" == "$expected_rc" ]] || { printf '原始故障码丢失: %s expected=%s actual=%s\n' "$scenario" "$expected_rc" "$rc" >&2; exit 1; }
    [[ "$rc" != 0 ]] || { printf '故障场景误报成功: %s\n' "$scenario" >&2; exit 1; }
    [[ ! -e "$workspace/lock" ]] || { printf '维护锁未释放: %s\n' "$scenario" >&2; exit 1; }
    assert_not_contains "$workspace/output.log" '发布完成(target='
    assert_not_contains "$workspace/docker.log" 'downgrade'
    assert_not_contains "$workspace/docker.log" 'restore'
    assert_contains "$workspace/releases/current.env" "RELEASE_SHA=$old_sha"
    case "$scenario" in
      backup|verify|backend_build|migration)
        expected_rollback=not_switched
        cmp "$workspace/original.env" "$workspace/releases/current.env"
        assert_not_contains "$workspace/docker.log" 'compose up -d --no-deps --no-build --pull never'
        assert_contains "$workspace/output.log" '应用尚未切换'
        [[ -f "$workspace/releases/pending.env" ]]
        ;;
      rollback_failed)
        expected_rollback=failed
        assert_contains "$workspace/output.log" '应用自动回滚失败'
        assert_not_contains "$workspace/output.log" '应用自动回滚完成'
        [[ -f "$workspace/releases/pending.env" ]]
        ;;
      *)
        expected_rollback=restored
        assert_contains "$workspace/output.log" '应用自动回滚完成'
        assert_contains "$workspace/docker.log" "compose up -d --no-deps --no-build --pull never backend | release=$old_sha"
        assert_contains "$workspace/docker.log" "compose up -d --no-deps --no-build --pull never frontend | release=$old_sha"
        [[ ! -f "$workspace/releases/pending.env" ]]
        ;;
    esac
    # 所有异常都必须给出所处阶段；迁移已尝试时不能暗示数据库也已还原。
    [[ "$(grep -c '发布事务失败(rc=' "$workspace/output.log")" == 1 ]] || {
      printf '故障重复处理（子Shell不得释放主事务锁）: %s\n' "$scenario" >&2; exit 1;
    }
    assert_contains "$workspace/output.log" 'stage='
    if [[ "$scenario" == migration ]]; then
      assert_contains "$workspace/output.log" '数据库迁移已尝试'
    fi
    printf 'failure_case=%s exit=%s recovery=%s PASS\n' "$scenario" "$rc" "$expected_rollback"
  done
}
