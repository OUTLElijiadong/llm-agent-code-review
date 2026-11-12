#!/usr/bin/env bash
# 被 test_scripts.sh source：真实发布/回滚脚本配隔离命令替身，无生产连接。
run_deploy_failure_matrix() {
  local test_root="$1" scenario workspace rc expected_rollback expected_rc ledger_schema ledger_reference probe_failure chat_revision_rows
  local member_capability member_probe_failure
  local old_sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  local new_sha=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
  for scenario in tls backup verify backend_build migration backend_up backend_health backend_smoke frontend_build frontend_up assets frontend_health https ops_check rollback_failed ledger_empty_rollback_refused ledger_written_rollback_refused ledger_probe_failed member_empty_old_refused member_unknown_refused member_probe_failed legacy_head_partial_schema legacy_head_schema_mismatch legacy_head_database_ahead chat_multiple_revisions chat_confirmation_missing chat_partial_preexisting chat_database_ahead chat_complete_unversioned chat_revision_unknown chat_schema_query_failure chat_migration_graph_invalid chat_stop_failure chat_stop_verification_failure chat_partial_migration chat_migration_success chat_already_migrated_success chat_future_head_success chat_post_migration_health chat_post_migration_health_compatible; do
    workspace="$test_root/failure-matrix-$scenario"
    mkdir -p "$workspace/repo/deploy/lib" "$workspace/repo/backend/alembic" "$workspace/bin" "$workspace/releases"
    cp deploy.sh rollback.sh "$workspace/repo/deploy/"
    if [[ "$scenario" == ops_check ]]; then
      # 仅在隔离夹具中替换平台判定，实际巡检失败/日志/回滚分支不替换。
      python3 - "$workspace/repo/deploy/deploy.sh" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
source = p.read_text()
platform = 'if [[ "$EUID" == 0 && "$(uname -s)" == "Linux" && -d /run/systemd/system ]]; then'
assert source.count(platform) == 1
p.write_text(source.replace(platform, 'if true; then'))
PY
      mkdir -p "$workspace/repo/deploy/systemd"
      printf '#!/usr/bin/env bash\nexit 0\n' > "$workspace/repo/deploy/systemd/install.sh"
      cat > "$workspace/repo/deploy/ops-check.sh" <<'SCRIPT'
#!/usr/bin/env bash
printf '{"status":"error","blocking_checks":["https"],"checks":{"https":{"ok":false}}}\n'
exit 1
SCRIPT
      chmod +x "$workspace/repo/deploy/systemd/install.sh" "$workspace/repo/deploy/ops-check.sh"
    fi
    cp lib/common.sh "$workspace/repo/deploy/lib/"
    cp lib/checkpoint_compatibility.py "$workspace/repo/deploy/lib/"
    cp lib/project_member_compatibility.py "$workspace/repo/deploy/lib/"
    cp lib/chat_authority_compatibility.py "$workspace/repo/deploy/lib/"
    cp lib/chat_authority_schema_compatibility.py "$workspace/repo/deploy/lib/"
    cp lib/alembic_revision_compatibility.py "$workspace/repo/deploy/lib/"
    cp -R ../backend/alembic/versions "$workspace/repo/backend/alembic/"
    python3 "$workspace/repo/deploy/lib/chat_authority_schema_compatibility.py" \
      "$workspace/repo/backend/alembic/versions/119_chat_message_authority.py" --fixture \
      > "$workspace/chat-authority-complete-schema.json"
    printf '3.8.4\n' > "$workspace/repo/VERSION"
    printf 'isolated geolite fixture\n' > "$workspace/repo/backend/GeoLite2-City.mmdb"
    write_strong_database_test_env "$workspace/repo/deploy/.env"
    write_frontend_tls_test_assets "$workspace/repo/deploy"
    cat >> "$workspace/repo/deploy/.env" <<ENV
GEOLITE_DB_HOST_PATH=$workspace/repo/backend/GeoLite2-City.mmdb
APP_RELEASE=$old_sha
APP_VERSION=3.8.2
BACKEND_RELEASE=$old_sha
FRONTEND_RELEASE=$old_sha
ENV
    if [[ "$scenario" == tls ]]; then
      printf 'CERTBOT_CONF_DIR=%s/missing-certificates\n' "$workspace" >> "$workspace/repo/deploy/.env"
    fi
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
printf 'sync-assets | release=%s\n' "$APP_RELEASE" >> "$FAKE_DOCKER_LOG"
[[ "$FAIL_SCENARIO" != assets ]] || exit 43
SCRIPT
    write_release_fake_docker "$workspace/bin/docker-base"
    cat > "$workspace/bin/docker" <<'SCRIPT'
#!/usr/bin/env bash
set -eu
stage=''
case "$*" in
  'compose stop backend') stage=chat_stop_failure ;;
  'compose ps --status running --quiet backend') stage=chat_stop_verification_failure ;;
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
if [[ "${APP_RELEASE:-}" == bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb && "$FAIL_SCENARIO" == chat_post_migration_health && "$stage" == backend_health ]]; then
  printf 'injected:post_migration_backend_health\n' >> "$FAKE_DOCKER_LOG"
  printf 'unhealthy\n'
  exit 0
fi
if [[ "${APP_RELEASE:-}" == bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb && "$FAIL_SCENARIO" == chat_post_migration_health_compatible && "$stage" == backend_health ]]; then
  printf 'injected:post_migration_backend_health\n' >> "$FAKE_DOCKER_LOG"
  printf 'unhealthy\n'
  exit 0
fi
if [[ "$FAIL_SCENARIO" == chat_partial_migration && "$stage" == migration ]]; then
  printf 'partial\n' > "${FAKE_CHAT_AUTHORITY_SCHEMA_FILE:?}"
  printf 'injected:partial_migration\n' >> "$FAKE_DOCKER_LOG"
  exit 44
fi
exec "$(dirname "$0")/docker-base" "$@"
SCRIPT
    cat > "$workspace/bin/curl" <<'SCRIPT'
#!/usr/bin/env bash
set -eu
if [[ "${APP_RELEASE:-}" == bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ]]; then
  case "$FAIL_SCENARIO:$*" in backend_smoke:*healthz*|rollback_failed:*healthz*|ledger_empty_rollback_refused:*healthz*|ledger_written_rollback_refused:*healthz*|ledger_probe_failed:*healthz*|member_empty_old_refused:*healthz*|member_unknown_refused:*healthz*|member_probe_failed:*healthz*|https:*https://*) exit 22 ;; esac
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
    db_revision_file="$workspace/db-revision"
    chat_schema_file="$workspace/chat-authority-schema"
    chat_revision=048_ai_usage_attribution
    chat_head=048_ai_usage_attribution
    chat_revision_rows=""
    chat_capability=unsupported
    printf '%s\n' "$chat_revision" > "$db_revision_file"
    printf 'absent\n' > "$chat_schema_file"
    case "$scenario" in
      chat_confirmation_missing|chat_partial_preexisting|chat_database_ahead|chat_complete_unversioned|chat_stop_failure|chat_stop_verification_failure|chat_partial_migration|chat_migration_success|chat_already_migrated_success|chat_future_head_success|chat_post_migration_health|chat_post_migration_health_compatible)
        chat_revision=118_clarification_authority
        chat_head=119_chat_message_authority
        printf '%s\n' "$chat_revision" > "$db_revision_file"
        [[ "$scenario" != chat_post_migration_health_compatible ]] || chat_capability=supported
        ;;
      chat_revision_unknown|chat_schema_query_failure|chat_migration_graph_invalid)
        chat_revision=118_clarification_authority
        chat_head=119_chat_message_authority
        printf '%s\n' "$chat_revision" > "$db_revision_file"
        [[ "$scenario" != chat_revision_unknown ]] || : > "$db_revision_file"
        [[ "$scenario" != chat_schema_query_failure ]] || printf 'invalid\n' > "$chat_schema_file"
        [[ "$scenario" != chat_migration_graph_invalid ]] || printf 'definitely invalid python syntax (\n' > \
          "$workspace/repo/backend/alembic/versions/118_broken_fixture.py"
        ;;
      chat_multiple_revisions)
        chat_revision=118_clarification_authority
        chat_head=119_chat_message_authority
        printf '%s\n' "$chat_revision" > "$db_revision_file"
        chat_revision_rows=$'118_clarification_authority\n119_chat_message_authority'
        ;;
      legacy_head_partial_schema) printf 'partial\n' > "$chat_schema_file" ;;
      legacy_head_schema_mismatch) printf 'complete\n' > "$chat_schema_file" ;;
      legacy_head_database_ahead)
        chat_revision=119_chat_message_authority
        printf '%s\n' "$chat_revision" > "$db_revision_file"
        printf 'complete\n' > "$chat_schema_file"
        ;;
    esac
    case "$scenario" in
      chat_partial_preexisting) printf 'partial\n' > "$chat_schema_file" ;;
      chat_database_ahead)
        chat_revision=120_not_in_target_chain
        printf '%s\n' "$chat_revision" > "$db_revision_file"
        printf 'complete\n' > "$chat_schema_file"
        ;;
      chat_complete_unversioned) printf 'complete\n' > "$chat_schema_file" ;;
      chat_already_migrated_success)
        chat_revision=119_chat_message_authority
        printf '%s\n' "$chat_revision" > "$db_revision_file"
        printf 'complete\n' > "$chat_schema_file"
        ;;
      chat_future_head_success)
        chat_head=120_chat_authority_fixture
        cat > "$workspace/repo/backend/alembic/versions/120_chat_authority_fixture.py" <<'PY'
revision = "120_chat_authority_fixture"
down_revision = "119_chat_message_authority"
branch_labels = None
depends_on = None
PY
        ;;
    esac
    ledger_schema=0 ledger_reference=0 probe_failure=0
    member_capability=supported member_probe_failure=0
    case "$scenario" in
      ledger_empty_rollback_refused) ledger_schema=1 ;;
      ledger_written_rollback_refused) ledger_schema=1; ledger_reference=1 ;;
      ledger_probe_failed) probe_failure=1 ;;
      member_empty_old_refused) member_capability=unsupported ;;
      member_unknown_refused) member_capability=unknown ;;
      member_probe_failed) member_probe_failure=1 ;;
    esac
    set +e
    if [[ "$scenario" == chat_confirmation_missing || "$scenario" == chat_partial_preexisting \
      || "$scenario" == chat_database_ahead || "$scenario" == chat_complete_unversioned ]]; then
      env PATH="$workspace/bin:$PATH" FAIL_SCENARIO="$scenario" FAKE_RELEASE_SHA="$old_sha" \
        FAKE_RELEASE_VERSION=3.8.2 FAKE_ALEMBIC_REVISION="$chat_revision" FAKE_ALEMBIC_HEAD="$chat_head" \
        FAKE_ALEMBIC_CURRENT_ROWS="$chat_revision_rows" \
        FAKE_DB_REVISION_FILE="$db_revision_file" FAKE_CHAT_AUTHORITY_SCHEMA_FILE="$chat_schema_file" \
        FAKE_CHAT_AUTHORITY_COMPLETE_FIXTURE="$workspace/chat-authority-complete-schema.json" \
        FAKE_CHAT_OLD_CAPABILITY="$chat_capability" \
        FAKE_CHECKPOINT_SCHEMA="$ledger_schema" FAKE_CHECKPOINT_REFERENCE="$ledger_reference" \
        FAKE_CHECKPOINT_PROBE_FAILURE="$probe_failure" \
        FAKE_MEMBER_CAPABILITY="$member_capability" FAKE_MEMBER_PROBE_FAILURE="$member_probe_failure" \
        FAKE_RELEASE_WORKSPACE="$workspace" FAKE_DOCKER_LOG="$workspace/docker.log" \
        RELEASE_STATE_DIR="$workspace/releases" MAINTENANCE_LOCK_DIR="$workspace/lock" \
        DEPLOY_ENV_FILE=.env BACKEND_HEALTH_TIMEOUT=1 FRONTEND_HEALTH_TIMEOUT=1 \
        "$workspace/repo/deploy/deploy.sh" all --revision "$new_sha" > "$workspace/output.log" 2>&1
    else
      env PATH="$workspace/bin:$PATH" FAIL_SCENARIO="$scenario" FAKE_RELEASE_SHA="$old_sha" \
        FAKE_RELEASE_VERSION=3.8.2 FAKE_ALEMBIC_REVISION="$chat_revision" FAKE_ALEMBIC_HEAD="$chat_head" \
        FAKE_ALEMBIC_CURRENT_ROWS="$chat_revision_rows" \
        FAKE_DB_REVISION_FILE="$db_revision_file" FAKE_CHAT_AUTHORITY_SCHEMA_FILE="$chat_schema_file" \
        FAKE_CHAT_AUTHORITY_COMPLETE_FIXTURE="$workspace/chat-authority-complete-schema.json" \
        FAKE_CHAT_OLD_CAPABILITY="$chat_capability" \
        FAKE_CHECKPOINT_SCHEMA="$ledger_schema" FAKE_CHECKPOINT_REFERENCE="$ledger_reference" \
        FAKE_CHECKPOINT_PROBE_FAILURE="$probe_failure" \
        FAKE_MEMBER_CAPABILITY="$member_capability" FAKE_MEMBER_PROBE_FAILURE="$member_probe_failure" \
        FAKE_RELEASE_WORKSPACE="$workspace" FAKE_DOCKER_LOG="$workspace/docker.log" \
        RELEASE_STATE_DIR="$workspace/releases" MAINTENANCE_LOCK_DIR="$workspace/lock" \
        DEPLOY_ENV_FILE=.env BACKEND_HEALTH_TIMEOUT=1 FRONTEND_HEALTH_TIMEOUT=1 \
        "$workspace/repo/deploy/deploy.sh" all --revision "$new_sha" --confirm-no-external-chat-writers > "$workspace/output.log" 2>&1
    fi
    rc=$?
    set -e
    if [[ "$scenario" == chat_migration_success || "$scenario" == chat_already_migrated_success \
      || "$scenario" == chat_future_head_success ]]; then
      [[ "$rc" == 0 ]] || { cat "$workspace/output.log"; printf '119 success fixture failed: rc=%s\n' "$rc" >&2; exit 1; }
      assert_contains "$workspace/releases/current.env" "RELEASE_SHA=$new_sha"
      [[ ! -f "$workspace/releases/pending.env" ]]
      [[ "$(cat "$db_revision_file")" == "$chat_head" ]]
      [[ "$(cat "$chat_schema_file")" == complete ]]
      python3 - "$workspace/docker.log" <<'PY'
import sys
entries = open(sys.argv[1], encoding="utf-8").read().splitlines()
stop = next(i for i, row in enumerate(entries) if "compose stop backend" in row)
verify = next(i for i, row in enumerate(entries) if "compose ps --status running --quiet backend" in row)
migrate = next(i for i, row in enumerate(entries) if "upgrade head" in row)
start = next(i for i, row in enumerate(entries) if "compose up -d --no-deps --no-build --pull never backend" in row)
assert stop < verify < migrate < start, entries
PY
      printf 'failure_case=%s exit=%s recovery=published_with_119_gate PASS\n' "$scenario" "$rc"
      continue
    fi
    expected_rc=1
    case "$scenario" in
      tls) assert_contains "$workspace/output.log" 'Frontend TLS / ACME 持久化挂载未通过预检' ;;
      backup) expected_rc=41 ;;
      verify) expected_rc=42 ;;
      backend_build|migration|backend_up|frontend_build|frontend_up|chat_partial_migration) expected_rc=44 ;;
      assets) expected_rc=43 ;;
      chat_stop_failure|chat_stop_verification_failure) expected_rc=1 ;;
    esac
    [[ "$rc" == "$expected_rc" ]] || { printf '原始故障码丢失: %s expected=%s actual=%s\n' "$scenario" "$expected_rc" "$rc" >&2; exit 1; }
    [[ "$rc" != 0 ]] || { printf '故障场景误报成功: %s\n' "$scenario" >&2; exit 1; }
    [[ ! -e "$workspace/lock" ]] || { printf '维护锁未释放: %s\n' "$scenario" >&2; exit 1; }
    assert_not_contains "$workspace/output.log" '发布完成(target='
    assert_not_contains "$workspace/docker.log" 'downgrade'
    assert_not_contains "$workspace/docker.log" 'restore'
    assert_contains "$workspace/releases/current.env" "RELEASE_SHA=$old_sha"
    if [[ "$scenario" == assets ]]; then
      # 同步失败必须在新frontend启动前失败，避免与entrypoint同时写同一assets卷。
      assert_not_contains "$workspace/docker.log" "compose up -d --no-deps --no-build --pull never frontend | release=$new_sha"
    fi
    if [[ "$scenario" == frontend_health ]]; then
      local sync_line switch_line
      sync_line="$(grep -n "sync-assets | release=$new_sha" "$workspace/docker.log" | cut -d: -f1)"
      switch_line="$(grep -n "compose up -d --no-deps --no-build --pull never frontend | release=$new_sha" "$workspace/docker.log" | cut -d: -f1)"
      [[ -n "$sync_line" && -n "$switch_line" && "$sync_line" -lt "$switch_line" ]] || {
        printf '新前端必须在assets同步成功后启动\n' >&2
        exit 1
      }
    fi
    case "$scenario" in
      tls|backup|verify|backend_build|migration)
        expected_rollback=not_switched
        cmp "$workspace/original.env" "$workspace/releases/current.env"
        assert_not_contains "$workspace/docker.log" 'compose up -d --no-deps --no-build --pull never'
        assert_contains "$workspace/output.log" '应用尚未切换'
        [[ -f "$workspace/releases/pending.env" ]]
        if [[ "$scenario" == tls ]]; then
          assert_not_contains "$workspace/docker.log" 'compose build backend'
          assert_not_contains "$workspace/docker.log" 'compose build frontend'
          assert_not_contains "$workspace/docker.log" 'backup.sh'
        fi
        ;;
      chat_migration_graph_invalid)
        expected_rollback=not_switched
        cmp "$workspace/original.env" "$workspace/releases/current.env"
        [[ -f "$workspace/releases/pending.env" ]]
        assert_not_contains "$workspace/docker.log" 'compose stop backend'
        assert_not_contains "$workspace/docker.log" 'upgrade head'
        assert_not_contains "$workspace/docker.log" 'compose up -d --no-deps --no-build --pull never backend'
        assert_contains "$workspace/output.log" '无法确认目标 Alembic head 与 119 聊天权限迁移的关系'
        assert_contains "$workspace/output.log" '应用尚未切换'
        ;;
      chat_confirmation_missing|chat_partial_preexisting|chat_database_ahead|chat_complete_unversioned|chat_revision_unknown|chat_schema_query_failure|chat_stop_failure|chat_stop_verification_failure|chat_partial_migration|legacy_head_partial_schema|legacy_head_schema_mismatch|chat_multiple_revisions)
        expected_rollback=writers_stopped
        cmp "$workspace/original.env" "$workspace/releases/current.env"
        [[ -f "$workspace/releases/pending.env" ]]
        assert_not_contains "$workspace/docker.log" 'compose up -d --no-deps --no-build --pull never backend'
        if [[ "$scenario" == chat_confirmation_missing ]]; then
          assert_contains "$workspace/output.log" '--confirm-no-external-chat-writers'
          assert_not_contains "$workspace/docker.log" 'compose stop backend'
          assert_not_contains "$workspace/docker.log" 'upgrade head'
        elif [[ "$scenario" == chat_partial_preexisting || "$scenario" == chat_complete_unversioned ]]; then
          assert_contains "$workspace/docker.log" 'compose stop backend'
          assert_not_contains "$workspace/docker.log" 'upgrade head'
          [[ -f "$workspace/stopped" ]]
          assert_contains "$workspace/output.log" 'Backend 保持停止'
        elif [[ "$scenario" == chat_database_ahead ]]; then
          assert_not_contains "$workspace/docker.log" 'compose stop backend'
          assert_not_contains "$workspace/docker.log" 'upgrade head'
          assert_contains "$workspace/output.log" '不在目标迁移 head 的祖先链上'
        elif [[ "$scenario" == chat_stop_failure ]]; then
          assert_contains "$workspace/docker.log" 'injected:chat_stop_failure'
          assert_not_contains "$workspace/docker.log" 'upgrade head'
        elif [[ "$scenario" == chat_stop_verification_failure ]]; then
          assert_contains "$workspace/docker.log" 'compose stop backend'
          assert_contains "$workspace/docker.log" 'injected:chat_stop_verification_failure'
          assert_not_contains "$workspace/docker.log" 'upgrade head'
        elif [[ "$scenario" == legacy_head_partial_schema || "$scenario" == legacy_head_schema_mismatch ]]; then
          assert_contains "$workspace/docker.log" 'compose stop backend'
          assert_not_contains "$workspace/docker.log" 'upgrade head'
          [[ -f "$workspace/stopped" ]]
          assert_contains "$workspace/output.log" 'Backend 保持停止'
        elif [[ "$scenario" == chat_multiple_revisions ]]; then
          assert_contains "$workspace/docker.log" 'compose stop backend'
          assert_not_contains "$workspace/docker.log" 'upgrade head'
          [[ -f "$workspace/stopped" ]]
          assert_contains "$workspace/output.log" '多个 current revision'
        elif [[ "$scenario" == chat_revision_unknown || "$scenario" == chat_schema_query_failure ]]; then
          assert_contains "$workspace/docker.log" 'compose stop backend'
          assert_not_contains "$workspace/docker.log" 'upgrade head'
          [[ -f "$workspace/stopped" ]]
          assert_contains "$workspace/output.log" 'Backend 保持停止'
        else
          [[ -f "$workspace/stopped" ]]
          assert_contains "$workspace/docker.log" 'injected:partial_migration'
          [[ "$(cat "$chat_schema_file")" == partial ]]
          assert_contains "$workspace/output.log" '保持 Backend 停止'
        fi
        ;;
      legacy_head_database_ahead)
        expected_rollback=not_switched
        cmp "$workspace/original.env" "$workspace/releases/current.env"
        [[ -f "$workspace/releases/pending.env" ]]
        assert_not_contains "$workspace/docker.log" 'compose stop backend'
        assert_not_contains "$workspace/docker.log" 'upgrade head'
        assert_not_contains "$workspace/docker.log" 'compose up -d --no-deps --no-build --pull never backend'
        assert_contains "$workspace/output.log" '数据库 revision 已领先或偏离旧迁移目标'
        ;;
      chat_post_migration_health|chat_post_migration_health_compatible)
        expected_rollback=compatibility_gated
        assert_contains "$workspace/docker.log" 'compose stop backend'
        assert_contains "$workspace/output.log" '回滚前 Backend 写入者已全部停止'
        [[ "$(cat "$chat_schema_file")" == complete ]]
        if [[ "$scenario" == chat_post_migration_health ]]; then
          assert_contains "$workspace/output.log" '不支持 119 小菱消息权限快照契约'
          assert_not_contains "$workspace/docker.log" "compose up -d --no-deps --no-build --pull never backend | release=$old_sha"
          [[ -f "$workspace/stopped" ]]
          [[ -f "$workspace/releases/pending.env" ]]
        else
          assert_contains "$workspace/output.log" '兼容门允许的应用层回滚完成'
          assert_contains "$workspace/docker.log" "compose up -d --no-deps --no-build --pull never backend | release=$old_sha"
          [[ ! -f "$workspace/releases/pending.env" ]]
          assert_contains "$workspace/releases/current.env" "RELEASE_SHA=$old_sha"
        fi
        ;;
      rollback_failed|ledger_empty_rollback_refused|ledger_written_rollback_refused|ledger_probe_failed|member_empty_old_refused|member_unknown_refused|member_probe_failed)
        expected_rollback=failed
        assert_contains "$workspace/output.log" '应用自动回滚失败'
        assert_not_contains "$workspace/output.log" '应用自动回滚完成'
        [[ -f "$workspace/releases/pending.env" ]]
        if [[ "$scenario" == ledger_* ]]; then
          assert_not_contains "$workspace/docker.log" "compose up -d --no-deps --no-build --pull never backend | release=$old_sha"
          assert_not_contains "$workspace/docker.log" 'compose stop '
          cmp "$workspace/original.env" "$workspace/releases/current.env"
          if [[ "$scenario" == ledger_probe_failed ]]; then
            assert_contains "$workspace/output.log" '目标镜像检查点能力验证失败'
          else
            assert_contains "$workspace/output.log" '即使账本暂空也拒绝回退'
          fi
        fi
        if [[ "$scenario" == member_* ]]; then
          assert_not_contains "$workspace/docker.log" "compose up -d --no-deps --no-build --pull never backend | release=$old_sha"
          assert_not_contains "$workspace/docker.log" 'compose stop '
          cmp "$workspace/original.env" "$workspace/releases/current.env"
          if [[ "$scenario" == member_probe_failed ]]; then
            assert_contains "$workspace/output.log" '目标镜像成员权限能力验证失败'
          else
            assert_contains "$workspace/output.log" '即使成员暂空也保留数据并前向修复'
          fi
        fi
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
    if [[ "$scenario" == ops_check ]]; then
      assert_contains "$workspace/output.log" 'stage=post_release_ops_check'
      assert_contains "$workspace/output.log" '发布后巡检失败回执:'
      assert_contains "$workspace/output.log" '"blocking_checks":["https"]'
      assert_contains "$workspace/repo/deploy/.env" "APP_RELEASE=$old_sha"
    fi
    if [[ "$scenario" == migration ]]; then
      assert_contains "$workspace/output.log" '数据库迁移已尝试'
    fi
    printf 'failure_case=%s exit=%s recovery=%s PASS\n' "$scenario" "$rc" "$expected_rollback"
  done
}
