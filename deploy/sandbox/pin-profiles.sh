#!/usr/bin/env bash
# Pin the five trusted local sandbox tags to immutable image IDs.
# Dry-run by default; only --apply replaces profiles.json atomically.
set -Eeuo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd -P)"
profiles_file="$script_dir/profiles.json"
apply=0

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) apply=1; shift ;;
    --profiles) [[ $# -ge 2 ]] || fail '--profiles 需要文件路径'; profiles_file="$2"; shift 2 ;;
    *) fail "未知参数: $1" ;;
  esac
done

command -v docker >/dev/null 2>&1 || fail '缺少命令: docker'
command -v python3 >/dev/null 2>&1 || fail '缺少命令: python3'

python3 - "$profiles_file" "$apply" <<'PY'
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

profiles_path = Path(sys.argv[1]).expanduser()
apply = sys.argv[2] == "1"
languages = ("python", "node", "java", "go", "php")
digest_pattern = re.compile(r"^sha256:[0-9a-f]{64}$")


def reject(message: str) -> "NoReturn":
    raise SystemExit(f"profile 固化失败：{message}")


try:
    file_stat = profiles_path.lstat()
except OSError:
    reject("profiles.json 不存在")
if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
    reject("profiles.json 必须是普通文件")

try:
    document = json.loads(profiles_path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError):
    reject("profiles.json 格式无效")
if not isinstance(document, dict) or not isinstance(document.get("profiles"), dict):
    reject("profiles.json 缺少 profiles 对象")
profiles = document["profiles"]
if set(profiles) != set(languages):
    reject("profiles.json 必须完整包含 python、node、java、go、php")

updates: dict[str, str] = {}
for language in languages:
    profile = profiles[language]
    if not isinstance(profile, dict):
        reject(f"{language} profile 格式无效")
    image = str(profile.get("image", "")).strip()
    if not image:
        reject(f"{language} 未配置受信任本地镜像标签")
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{json .}}", image],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        reject(f"{language} 本地镜像不可用")
    try:
        inspected = json.loads(result.stdout)
        if isinstance(inspected, list):
            inspected = inspected[0]
        image_id = str(inspected["Id"])
    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
        reject(f"{language} 本地镜像 ID 无效")
    if not digest_pattern.fullmatch(image_id):
        reject(f"{language} 本地镜像 ID 不是 sha256 digest")
    updates[language] = image_id

for language in languages:
    print(f"{language}: {profiles[language]['image']} -> {updates[language]}")

if not apply:
    print("dry-run：未写入 profiles.json；使用 --apply 才会原子更新")
    raise SystemExit(0)

for language, digest in updates.items():
    profiles[language]["digest"] = digest

mode = stat.S_IMODE(file_stat.st_mode)
descriptor, temporary = tempfile.mkstemp(prefix=f".{profiles_path.name}.", dir=str(profiles_path.parent))
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    if os.geteuid() == 0:
        os.chown(temporary, file_stat.st_uid, file_stat.st_gid)
    os.chmod(temporary, mode)
    os.replace(temporary, profiles_path)
    directory_fd = os.open(profiles_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
except OSError as exc:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    reject(f"原子写入失败：{exc.__class__.__name__}")

print("已原子更新 profiles.json")
PY
