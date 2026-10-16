#!/usr/bin/env bash
# 只准备本次完整 Git 制品的独立发布树，不直接切换运行应用。
# 参数 6 可指定 bundle ref，参数 7 可指定活动生产仓库目录。
set -Eeuo pipefail

sha="${1:?完整提交 SHA 必填}"
expected_digest="${2:?Git bundle SHA-256 必填}"
expected_size="${3:?Git bundle 字节数必填}"
version="${4:?目标语义版本必填}"
base_sha="${5:?当前活动生产完整 SHA 必填}"
bundle_ref="${6:-refs/heads/codex/sandbox-php-context}"
base="${7:-/opt/prism-releases/$base_sha}"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]]
[[ "$expected_digest" =~ ^[0-9a-f]{64}$ ]]
[[ "$expected_size" =~ ^[0-9]+$ ]]
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
[[ "$base_sha" =~ ^[0-9a-f]{40}$ ]]
[[ "$bundle_ref" =~ ^refs/(heads|tags)/[A-Za-z0-9._/-]+$ ]]
[[ "$bundle_ref" != *..* && "$bundle_ref" != *//* && "$bundle_ref" != */ ]]
[[ "$base" == /* && "$base" != / ]]

bundle="/dev/shm/prism-v$version-$sha.bundle"
dest="/opt/prism-releases/$sha"
[[ -d "$base" && -f "$base/deploy/.env" && -f "$base/deploy/sandbox/.env" ]]
[[ -f "$bundle" && ! -e "$dest" ]]
[[ "$(stat -c %s "$bundle")" == "$expected_size" ]]
[[ "$(sha256sum "$bundle" | cut -d ' ' -f 1)" == "$expected_digest" ]]
[[ "$(git -C "$base" rev-parse HEAD)" == "$base_sha" ]]
[[ "$(awk -F= '$1 == "RELEASE_SHA" {print $2}' "$base/deploy/.releases/current.env")" == "$base_sha" ]]

# 继承实际活动生产的受保护 dotenv、证书挂载和账本，不读取其明文。
cp --reflink=always -a "$base" "$dest"
git -C "$dest" bundle verify "$bundle"
git -C "$dest" fetch --no-tags "$bundle" "$bundle_ref"
git -C "$dest" checkout --detach FETCH_HEAD
[[ "$(git -C "$dest" rev-parse HEAD)" == "$sha" ]]
[[ "$(tr -d '[:space:]' < "$dest/VERSION")" == "$version" ]]
[[ -f "$dest/deploy/.env" && -f "$dest/deploy/sandbox/.env" ]]
chmod 600 "$dest/deploy/.env" "$dest/deploy/sandbox/.env"
cd "$dest/deploy"
source lib/common.sh
assert_deploy_sources_clean "$dest"
assert_frontend_tls_assets .env
assert_deploy_capacity "$dest" "$(configured_backup_dir)"
printf 'staged_version=%s\nstaged_sha=%s\nstaged_path=%s\n' "$version" "$sha" "$dest"
