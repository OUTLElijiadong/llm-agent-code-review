#!/usr/bin/env bash
set -Eeuo pipefail
sha="$1"
expected_digest="$2"
expected_size="$3"
[[ "$expected_digest" =~ ^[0-9a-f]{64}$ ]]
[[ "$expected_size" =~ ^[0-9]+$ ]]
[[ "$sha" =~ ^[0-9a-f]{40}$ ]]
base=/opt/prism-releases/732f48f6929ceda1585bcae2489a0b01f0f2c5fb
bundle="/tmp/prism-v4.0.38-${sha}.bundle"
dest="/opt/prism-releases/${sha}"
[[ -f "$bundle" && ! -e "$dest" ]]
[[ "$(stat -c %s "$bundle")" == "$expected_size" ]]
[[ "$(sha256sum "$bundle" | cut -d ' ' -f 1)" == "$expected_digest" ]]
[[ "$(git -C "$base" rev-parse HEAD)" == 732f48f6929ceda1585bcae2489a0b01f0f2c5fb ]]
git -C "$base" bundle verify "$bundle"
git clone --no-hardlinks --no-checkout "$base" "$dest"
git -C "$dest" fetch --no-tags "$bundle" HEAD
git -C "$dest" checkout --detach FETCH_HEAD
[[ "$(git -C "$dest" rev-parse HEAD)" == "$sha" ]]
[[ "$(tr -d '[:space:]' < "$dest/VERSION")" == 4.0.38 ]]
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
