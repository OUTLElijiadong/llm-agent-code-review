#!/bin/sh
set -eu
root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
mkdir -p "$root/source" "$root/target"
i=0
while [ "$i" -lt 5000 ]; do
  printf 'isolated fixture %s\n' "$i" > "$root/source/chunk-$i.css"
  i=$((i + 1))
done
attempt=1
while [ "$attempt" -le 30 ]; do
  rm -rf "$root/target" "$root/go"
  mkdir "$root/target"
  pids=''
  i=0
  while [ "$i" -lt 8 ]; do
    (while [ ! -f "$root/go" ]; do :; done; cp -a "$root/source/." "$root/target/") > "$root/$i.log" 2>&1 &
    pids="$pids $!"
    i=$((i + 1))
  done
  touch "$root/go"
  failed=0
  for pid in $pids; do wait "$pid" || failed=$((failed + 1)); done
  if [ "$failed" -ne 0 ]; then
    printf 'concurrent_copy attempt=%s failed_writers=%s\n' "$attempt" "$failed"
    cat "$root/"*.log
    grep -q 'File exists' "$root/"*.log
    exit 0
  fi
  attempt=$((attempt + 1))
done
printf '%s\n' 'No collision observed in this run' >&2
exit 1
