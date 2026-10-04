#!/usr/bin/env bash
set -Eeuo pipefail

sha="${1:?完整提交 SHA 必填}"
expected_digest="${2:?Git bundle SHA-256 必填}"
expected_size="${3:?Git bundle 字节数必填}"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]]
[[ "$expected_digest" =~ ^[0-9a-f]{64}$ ]]
[[ "$expected_size" =~ ^[0-9]+$ ]]

base=/opt/prism-releases/c591f8141412f3c4c8d43d0ab4c50f67a17f1b49
bundle="/tmp/prism-v4.0.39-${sha}.bundle"
dest="/opt/prism-releases/${sha}"
[[ -d "$base" && -f "$base/deploy/.env" ]]
[[ -f "$bundle" && ! -e "$dest" ]]
[[ "$(stat -c %s "$bundle")" == "$expected_size" ]]
[[ "$(sha256sum "$bundle" | cut -d ' ' -f 1)" == "$expected_digest" ]]
[[ "$(git -C "$base" rev-parse HEAD)" == c591f8141412f3c4c8d43d0ab4c50f67a17f1b49 ]]

git -C "$base" bundle verify "$bundle"
git clone --no-hardlinks --no-checkout "$base" "$dest"
git -C "$dest" fetch --no-tags "$bundle" refs/heads/codex/prism-v4.0.23-context-mobile
git -C "$dest" checkout --detach FETCH_HEAD
[[ "$(git -C "$dest" rev-parse HEAD)" == "$sha" ]]
[[ "$(tr -d '[:space:]' < "$dest/VERSION")" == 4.0.39 ]]
[[ -z "$(git -C "$dest" status --porcelain)" ]]

install -m 0600 "$base/deploy/.env" "$dest/deploy/.env"
install -d -m 0700 "$dest/deploy/.releases"
for name in current.env previous.env; do
  if [[ -f "$base/deploy/.releases/$name" ]]; then
    install -m 0600 "$base/deploy/.releases/$name" "$dest/deploy/.releases/$name"
  fi
done

cd "$dest/deploy"
exec ./deploy.sh all --revision "$sha"
