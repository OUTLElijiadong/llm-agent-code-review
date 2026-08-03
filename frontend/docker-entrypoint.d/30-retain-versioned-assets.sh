#!/bin/sh
set -eu

source_dist="${PRISM_DIST_DIR:-/opt/prism-dist}"
html_root="${PRISM_HTML_DIR:-/usr/share/nginx/html}"
retention_days="${FRONTEND_ASSET_RETENTION_DAYS:-14}"

case "$retention_days" in
  ''|*[!0-9]*)
    printf '%s\n' 'FRONTEND_ASSET_RETENTION_DAYS must be an integer' >&2
    exit 64
    ;;
esac
if [ "$retention_days" -lt 1 ] || [ "$retention_days" -gt 30 ]; then
  printf '%s\n' 'FRONTEND_ASSET_RETENTION_DAYS must be between 1 and 30' >&2
  exit 64
fi

source_assets="$source_dist/assets"
target_assets="$html_root/assets"
[ -d "$source_assets" ] || {
  printf '%s\n' 'versioned frontend assets are missing from the image' >&2
  exit 66
}

mkdir -p "$target_assets"
find "$target_assets" -type f -mtime "+$retention_days" -delete
find "$target_assets" -depth -type d -empty -delete
cp -a "$source_assets/." "$target_assets/"

# Only refresh the current release files. Older hashes keep their original
# timestamp so they are removed after the bounded compatibility window.
find "$source_assets" -type f | while IFS= read -r source_file; do
  relative_path="${source_file#"$source_assets"/}"
  touch "$target_assets/$relative_path"
done
