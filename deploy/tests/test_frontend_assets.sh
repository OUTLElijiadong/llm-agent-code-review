#!/usr/bin/env bash
# 在已有本地镜像的隔离容器中验证真实入口脚本，不拉镜像、不挂载生产卷。
set -Eeuo pipefail
repo_dir="$(cd "$(dirname "$0")/../.." && pwd)"
image="${FRONTEND_ASSET_TEST_IMAGE:-prism-frontend:local}"
docker image inspect "$image" >/dev/null

docker run --rm --pull never --network none --entrypoint sh -i \
  -v "$repo_dir/frontend/docker-entrypoint.d/30-retain-versioned-assets.sh:/asset-entrypoint.sh:ro" \
  "$image" -s <<'TEST'
set -eu
root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
export PRISM_DIST_DIR="$root/new-dist" PRISM_HTML_DIR="$root/html"
mkdir -p "$PRISM_DIST_DIR/assets/nested" "$PRISM_HTML_DIR/assets"
printf 'current-index\n' > "$PRISM_DIST_DIR/assets/index-new.js"
printf 'current-css\n' > "$PRISM_DIST_DIR/assets/nested/new.css"
printf 'previous-index\n' > "$PRISM_HTML_DIR/assets/index-old.js"
printf 'expired-index\n' > "$PRISM_HTML_DIR/assets/index-expired.js"
touch -t 202001010000 "$PRISM_HTML_DIR/assets/index-expired.js"
# 窗口内旧哈希保留原始时间，而当前镜像资源更新时间。
touch -t 202001010000 "$PRISM_DIST_DIR/assets/index-new.js"
old_mtime=$(stat -c %Y "$PRISM_HTML_DIR/assets/index-old.js")
source_mtime=$(stat -c %Y "$PRISM_DIST_DIR/assets/index-new.js")

# 真实同步命令先完成，再运行真实容器入口；禁止同时写目标卷。
cp -a "$PRISM_DIST_DIR/assets/." "$PRISM_HTML_DIR/assets/"
sh /asset-entrypoint.sh
cmp "$PRISM_DIST_DIR/assets/index-new.js" "$PRISM_HTML_DIR/assets/index-new.js"
cmp "$PRISM_DIST_DIR/assets/nested/new.css" "$PRISM_HTML_DIR/assets/nested/new.css"
[ -f "$PRISM_HTML_DIR/assets/index-old.js" ]
[ ! -e "$PRISM_HTML_DIR/assets/index-expired.js" ]
[ "$(stat -c %Y "$PRISM_HTML_DIR/assets/index-old.js")" = "$old_mtime" ]
[ "$(stat -c %Y "$PRISM_HTML_DIR/assets/index-new.js")" -gt "$source_mtime" ]
printf '%s\n' 'sequential_sync_and_entrypoint=PASS current_hashes=complete previous_hash=retained expired_hash=removed'

# 再次启动及旧镜像回滚都仍由原入口恢复当前镜像自己的文件。
sh /asset-entrypoint.sh
mkdir -p "$root/old-dist/assets"
printf 'previous-index\n' > "$root/old-dist/assets/index-old.js"
PRISM_DIST_DIR="$root/old-dist" sh /asset-entrypoint.sh
cmp "$root/old-dist/assets/index-old.js" "$PRISM_HTML_DIR/assets/index-old.js"
[ -f "$PRISM_HTML_DIR/assets/index-new.js" ]
printf '%s\n' 'restart_and_rollback=PASS previous_hash=complete new_hash=retained'

# 非法保留窗和丢失镜像资源仍返回失败，不因避免竞态而放宽门禁。
for days in 0 31 invalid; do
  rc=0
  FRONTEND_ASSET_RETENTION_DAYS="$days" sh /asset-entrypoint.sh > "$root/error.log" 2>&1 || rc=$?
  [ "$rc" -eq 64 ]
done
rc=0
PRISM_DIST_DIR="$root/missing" sh /asset-entrypoint.sh > "$root/error.log" 2>&1 || rc=$?
[ "$rc" -eq 66 ]
printf '%s\n' 'invalid_retention_and_missing_source=PASS errors_preserved'
TEST
