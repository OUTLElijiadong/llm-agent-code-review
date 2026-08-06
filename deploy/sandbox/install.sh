#!/usr/bin/env bash
# Render and install the isolated sandbox executor. Dry-run unless --apply.
set -Eeuo pipefail

apply=0
deploy_dir="$(cd "$(dirname "$0")/.." && pwd -P)"
unit_dir="/etc/systemd/system"
service_name="prism-sandbox-executor.service"
temp_unit=""
verify_error=""
health_response=""
health_config=""
unit_backup=""
installed_unit=""
had_existing_unit=0
was_active=0
was_enabled=0
unit_changed=0

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

cleanup() {
  [[ -z "$temp_unit" ]] || rm -f -- "$temp_unit"
  [[ -z "$verify_error" ]] || rm -f -- "$verify_error"
  [[ -z "$health_response" ]] || rm -f -- "$health_response"
  [[ -z "$health_config" ]] || rm -f -- "$health_config"
  [[ -z "$unit_backup" ]] || rm -f -- "$unit_backup"
}

rollback_unit() {
  local rollback_failed=0

  [[ "$unit_changed" == "1" ]] || return 0
  systemctl stop "$service_name" >/dev/null 2>&1 || rollback_failed=1
  if [[ "$had_existing_unit" == "1" ]]; then
    cp -p -- "$unit_backup" "$installed_unit" || rollback_failed=1
  else
    rm -f -- "$installed_unit" || rollback_failed=1
  fi
  systemctl daemon-reload >/dev/null 2>&1 || rollback_failed=1
  if [[ "$was_enabled" == "1" ]]; then
    systemctl enable "$service_name" >/dev/null 2>&1 || rollback_failed=1
  else
    systemctl disable "$service_name" >/dev/null 2>&1 || true
  fi
  if [[ "$was_active" == "1" ]]; then
    systemctl start "$service_name" >/dev/null 2>&1 || rollback_failed=1
  else
    systemctl stop "$service_name" >/dev/null 2>&1 || true
  fi
  unit_changed=0
  return "$rollback_failed"
}

trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) apply=1; shift ;;
    --deploy-dir) [[ $# -ge 2 ]] || exit 2; deploy_dir="$2"; shift 2 ;;
    --unit-dir) [[ $# -ge 2 ]] || exit 2; unit_dir="$2"; shift 2 ;;
    *) printf '未知参数: %s\n' "$1" >&2; exit 2 ;;
  esac
done
deploy_dir="$(cd "$deploy_dir" && pwd -P)"
template="$deploy_dir/prism-sandbox-executor.service"
executor_script="$deploy_dir/prism_sandbox_executor.py"
environment_file="$deploy_dir/sandbox/.env"
main_environment_file="$deploy_dir/.env"

if [[ "$apply" != "1" ]]; then
  printf 'DRY-RUN deploy_dir=%s unit_dir=%s\n' "$deploy_dir" "$unit_dir"
  printf '仅展示计划，未执行生产预检；--apply 才会预检、安装并启动 %s\n' "$service_name"
  exit 0
fi

[[ "$EUID" -eq 0 ]] || fail '--apply 必须以 root 执行'
for command_name in chmod cp curl cut docker getent groupadd install mktemp python3 rm runsc sed systemctl systemd-analyze useradd; do
  command -v "$command_name" >/dev/null 2>&1 || fail "缺少命令: $command_name"
done
runsc --version >/dev/null 2>&1 || fail 'runsc 自检失败'
getent group docker >/dev/null 2>&1 || fail 'Docker 用户组不存在'
[[ -x /usr/bin/python3 ]] || fail 'service 所需的 /usr/bin/python3 不可执行'
[[ -f "$template" && ! -L "$template" ]] || fail '缺少普通文件形式的 service 模板'
[[ -f "$executor_script" && ! -L "$executor_script" ]] || fail '缺少普通文件形式的沙箱执行器脚本'
[[ -f "$environment_file" && ! -L "$environment_file" ]] || fail '缺少普通文件形式的 sandbox/.env'
[[ -f "$main_environment_file" && ! -L "$main_environment_file" ]] || fail '缺少普通文件形式的 deploy/.env'

python3 - "$environment_file" "$main_environment_file" "$deploy_dir" "$template" <<'PY'
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

sandbox_path = Path(sys.argv[1])
main_path = Path(sys.argv[2])
deploy_path = Path(sys.argv[3])
template_path = Path(sys.argv[4])


def reject(message: str) -> "NoReturn":
    raise SystemExit(f"安装预检失败：{message}")


def regular_root_secret(path: Path) -> None:
    try:
        current = path.lstat()
    except OSError:
        reject("环境文件不存在")
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
        reject("环境文件必须是普通文件")
    if current.st_uid != 0 or stat.S_IMODE(current.st_mode) != 0o600:
        reject("环境文件必须由 root 拥有且权限为 0600")


def regular_root_service_template(path: Path) -> None:
    try:
        current = path.lstat()
    except OSError:
        reject("service 模板不存在")
    mode = stat.S_IMODE(current.st_mode)
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
        reject("service 模板必须是普通文件")
    if current.st_uid != 0 or mode not in {0o444, 0o644} or mode & 0o022:
        reject("service 模板必须由 root 拥有且权限只能为 0444 或 0644")


def regular_root_runtime_file(path: Path, label: str) -> None:
    try:
        current = path.lstat()
    except OSError:
        reject(f"{label} 不存在")
    mode = stat.S_IMODE(current.st_mode)
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
        reject(f"{label} 必须是普通文件")
    if current.st_uid != 0 or mode & 0o022:
        reject(f"{label} 必须由 root 拥有且不可被组或其他用户写入")


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        reject("环境文件不可读")
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            reject("环境文件包含无效行")
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            reject("环境变量名无效")
        if key in values:
            reject("环境文件包含重复变量")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


regular_root_secret(sandbox_path)
regular_root_secret(main_path)
regular_root_service_template(template_path)
sandbox = load_env(sandbox_path)
main = load_env(main_path)

browser_script = deploy_path / "sandbox" / "browser_blackbox.js"
browser_proxy_script = deploy_path / "sandbox" / "browser_target_proxy.py"
regular_root_runtime_file(browser_script, "Playwright 固定脚本")
regular_root_runtime_file(browser_proxy_script, "Playwright 固定代理脚本")
try:
    compile(browser_proxy_script.read_bytes(), str(browser_proxy_script), "exec")
except (OSError, SyntaxError, ValueError):
    reject("Playwright 固定代理脚本无法通过 Python 编译检查")

for language in ("python", "node", "java", "go", "php"):
    dockerfile_path = deploy_path / "sandbox" / f"Dockerfile.{language}"
    try:
        dockerfile_stat = dockerfile_path.lstat()
        dockerfile_lines = dockerfile_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        reject(f"Dockerfile.{language} 不存在或不可读")
    if stat.S_ISLNK(dockerfile_stat.st_mode) or not stat.S_ISREG(dockerfile_stat.st_mode):
        reject(f"Dockerfile.{language} 必须是普通文件")
    expected_prefix = ["ARG BASE_IMAGE", "FROM ${BASE_IMAGE}", "ARG BASE_IMAGE"]
    required_digest_guards = {
        'image_digest="${BASE_IMAGE##*@sha256:}"',
        '[ "${#image_digest}" -eq 64 ]',
        "*[!0-9a-f]*) exit 1",
    }
    if (
        dockerfile_lines[:3] != expected_prefix
        or any(line.startswith("ARG BASE_IMAGE=") for line in dockerfile_lines)
        or not all(marker in "\n".join(dockerfile_lines) for marker in required_digest_guards)
    ):
        reject(f"Dockerfile.{language} 必须禁用 BASE_IMAGE 默认值并校验 name@sha256 摘要")

token = sandbox.get("SANDBOX_EXECUTOR_TOKEN", "")
main_token = main.get("SANDBOX_EXECUTOR_TOKEN", "")
known_placeholders = {
    "change_me_to_an_independent_64_character_random_token",
    "replace-with-at-least-32-random-characters",
}
if len(token) < 32 or len(main_token) < 32 or token != main_token or token in known_placeholders:
    reject("主 deploy/.env 与 sandbox/.env 的沙箱令牌不一致、长度不足或仍为示例值")
if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{32,500}", token):
    reject("沙箱令牌包含不适合安全传输的字符")

if sandbox.get("SANDBOX_EXECUTOR_MODE") != "strict":
    reject("SANDBOX_EXECUTOR_MODE 必须为 strict")
if sandbox.get("SANDBOX_RUNTIME") != "runsc":
    reject("SANDBOX_RUNTIME 必须为 runsc")
if sandbox.get("SANDBOX_ALLOW_RUNC_LOCAL_DEVELOPMENT", "").lower() != "false":
    reject("SANDBOX_ALLOW_RUNC_LOCAL_DEVELOPMENT 必须为 false")
if main.get("SANDBOX_MODE") != "strict":
    reject("主 deploy/.env 的 SANDBOX_MODE 必须为 strict")
if main.get("SANDBOX_REQUIRED_RUNTIME") != "runsc":
    reject("主 deploy/.env 的 SANDBOX_REQUIRED_RUNTIME 必须为 runsc")
if main.get("SANDBOX_ALLOW_RUNC", "").lower() != "false":
    reject("主 deploy/.env 的 SANDBOX_ALLOW_RUNC 必须为 false")
if main.get("SANDBOX_ENABLED", "").lower() != "true":
    reject("主 deploy/.env 的 SANDBOX_ENABLED 必须为 true")
if sandbox.get("SANDBOX_EXECUTOR_SOCKET") != "/var/lib/prism-sandbox/agent.sock":
    reject("sandbox/.env 的 SANDBOX_EXECUTOR_SOCKET 必须使用生产 UDS")
if main.get("SANDBOX_EXECUTOR_SOCKET") != sandbox.get("SANDBOX_EXECUTOR_SOCKET"):
    reject("主 deploy/.env 与 sandbox/.env 的 UDS 路径不一致")

try:
    default_seconds = int(sandbox["SANDBOX_DEFAULT_TTL_SECONDS"])
    max_seconds = int(sandbox["SANDBOX_MAX_TTL_SECONDS"])
    default_hours = int(main["SANDBOX_DEFAULT_TTL_HOURS"])
    max_hours = int(main["SANDBOX_MAX_TTL_HOURS"])
    sandbox_concurrency = int(sandbox["SANDBOX_MAX_CONCURRENCY"])
    main_concurrency = int(main["SANDBOX_MAX_CONCURRENCY"])
except (KeyError, ValueError):
    reject("TTL 或并发配置不是整数")
if default_seconds != default_hours * 3600 or max_seconds != max_hours * 3600:
    reject("主 deploy/.env 与 sandbox/.env 的 TTL 不一致")
if sandbox_concurrency != main_concurrency:
    reject("主 deploy/.env 与 sandbox/.env 的并发上限不一致")
if not 60 <= default_seconds <= max_seconds <= 30 * 24 * 60 * 60:
    reject("TTL 必须满足 60 秒 <= 默认值 <= 最大值 <= 30 天")
if not 1 <= sandbox_concurrency <= 8:
    reject("并发上限必须在 1 到 8 之间")

profile_value = sandbox.get("SANDBOX_PROFILE_FILE", "")
if not profile_value:
    reject("SANDBOX_PROFILE_FILE 未配置")
profile_path = Path(profile_value)
if not profile_path.is_absolute():
    profile_path = deploy_path / profile_path
try:
    profile_stat = profile_path.lstat()
except OSError:
    reject("profiles.json 不存在")
if stat.S_ISLNK(profile_stat.st_mode) or not stat.S_ISREG(profile_stat.st_mode):
    reject("profiles.json 必须是普通文件")
if profile_stat.st_uid != 0 or stat.S_IMODE(profile_stat.st_mode) & 0o022:
    reject("profiles.json 必须由 root 拥有且不可被组/其他用户写入")

executor_path = deploy_path / "prism_sandbox_executor.py"
try:
    executor_stat = executor_path.lstat()
except OSError:
    reject("沙箱执行器脚本不存在")
if stat.S_ISLNK(executor_stat.st_mode) or not stat.S_ISREG(executor_stat.st_mode):
    reject("沙箱执行器脚本必须是普通文件")
if executor_stat.st_uid != 0 or stat.S_IMODE(executor_stat.st_mode) & 0o022:
    reject("沙箱执行器脚本必须由 root 拥有且不可被组/其他用户写入")
try:
    compile(executor_path.read_bytes(), str(executor_path), "exec")
except (OSError, SyntaxError, ValueError):
    reject("沙箱执行器脚本无法通过 Python 编译检查")
try:
    profiles_document = json.loads(profile_path.read_text(encoding="utf-8"))
    profiles = profiles_document["profiles"]
except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
    reject("profiles.json 格式无效")
if profiles_document.get("schema_version") != 1 or not isinstance(profiles, dict):
    reject("profiles.json schema_version 必须为 1")
required_languages = {"python", "node", "java", "go", "php"}
if set(profiles) != required_languages:
    reject("profiles.json 必须完整包含五种受支持语言")
digest_pattern = re.compile(r"^sha256:[0-9a-f]{64}$")
playwright_image = sandbox.get("PLAYWRIGHT_IMAGE", "")
playwright_digest = sandbox.get("PLAYWRIGHT_IMAGE_DIGEST", "")
if not digest_pattern.fullmatch(playwright_digest):
    reject("PLAYWRIGHT_IMAGE_DIGEST 必须是 sha256 摘要")
if not playwright_image.endswith(f"@{playwright_digest}"):
    reject("PLAYWRIGHT_IMAGE 必须以同一不可变摘要固定")
try:
    playwright_timeout = int(sandbox["PLAYWRIGHT_TIMEOUT_SECONDS"])
except (KeyError, ValueError):
    reject("PLAYWRIGHT_TIMEOUT_SECONDS 必须是整数")
if not 30 <= playwright_timeout <= 180:
    reject("PLAYWRIGHT_TIMEOUT_SECONDS 必须在 30 到 180 秒之间")
inspect = subprocess.run(
    ["docker", "image", "inspect", "--format", "{{json .}}", playwright_image],
    capture_output=True,
    text=True,
    check=False,
)
if inspect.returncode != 0:
    reject("Playwright 固定镜像不可用")
try:
    document = json.loads(inspect.stdout)
    playwright_local_id = str(document["Id"])
    playwright_repo_digests = [str(item) for item in (document.get("RepoDigests") or [])]
except (json.JSONDecodeError, KeyError, TypeError):
    reject("Playwright 镜像 inspect 结果无效")
if not digest_pattern.fullmatch(playwright_local_id):
    reject("Playwright 本地镜像 ID 无效")
if playwright_local_id != playwright_digest and not any(
    item.endswith(f"@{playwright_digest}") for item in playwright_repo_digests
):
    reject("Playwright 本地镜像摘要与配置不一致")
for language in sorted(required_languages):
    profile = profiles[language]
    if not isinstance(profile, dict):
        reject(f"{language} profile 格式无效")
    image = str(profile.get("image", ""))
    digest = str(profile.get("digest", ""))
    if not image or not digest_pattern.fullmatch(digest):
        reject(f"{language} profile 必须配置不可变 digest")
    inspect = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{json .}}", image],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect.returncode != 0:
        reject(f"{language} 本地镜像不可用")
    try:
        document = json.loads(inspect.stdout)
        local_id = str(document["Id"])
    except (json.JSONDecodeError, KeyError, TypeError):
        reject(f"{language} 本地镜像 inspect 结果无效")
    if local_id != digest:
        reject(f"{language} 本地镜像 ID 与 profile digest 不一致")

runtime_check = subprocess.run(
    ["docker", "info", "--format", "{{json .Runtimes}}"],
    capture_output=True,
    text=True,
    check=False,
)
if runtime_check.returncode != 0:
    reject("Docker daemon 不可用")
try:
    runtimes = json.loads(runtime_check.stdout)
except json.JSONDecodeError:
    reject("Docker runtime 列表无效")
if not isinstance(runtimes, dict) or "runsc" not in runtimes:
    reject("Docker 未注册 runsc runtime")

PY

temp_unit="$(mktemp "${TMPDIR:-/tmp}/prism-sandbox-executor.XXXXXX.service")"
verify_error="$(mktemp)"
escaped="${deploy_dir//\\/\\\\}"
escaped="${escaped//&/\\&}"
escaped="${escaped//|/\\|}"
sed "s|@DEPLOY_DIR@|$escaped|g" "$template" > "$temp_unit"
if ! systemd-analyze verify "$temp_unit" >/dev/null 2>"$verify_error"; then
  fail 'systemd 单元预检失败'
fi

health_response="$(mktemp)"
health_config="$(mktemp)"
chmod 0600 "$health_config"
sandbox_token="$(python3 - "$environment_file" <<'PY'
from pathlib import Path
import sys

for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    stripped = line.strip()
    if stripped.startswith("SANDBOX_EXECUTOR_TOKEN="):
        print(stripped.split("=", 1)[1].strip().strip("\"'"))
        break
else:
    raise SystemExit(1)
PY
)"
printf 'header = "Authorization: Bearer %s"\n' "$sandbox_token" > "$health_config"

if ! getent group prism-sandbox >/dev/null 2>&1; then
  groupadd --system --gid 992 prism-sandbox
fi
[[ "$(getent group prism-sandbox | cut -d: -f3)" == "992" ]] || {
  printf '%s\n' 'prism-sandbox GID 必须是 992，以便 Backend 只访问沙箱 Unix Socket' >&2
  exit 1
}
if ! getent passwd prism-sandbox >/dev/null 2>&1; then
  useradd --system --gid prism-sandbox --groups docker --home-dir /var/lib/prism-sandbox --shell /usr/sbin/nologin prism-sandbox
fi
[[ "$(getent passwd prism-sandbox | cut -d: -f4)" == "992" ]] || {
  printf '%s\n' 'prism-sandbox 用户主组不是 992，拒绝覆盖现有账户' >&2
  exit 1
}

# StateDirectory 是 Backend 容器挂载源，socket 直接落在这里。systemd 先以
# 0770 创建目录；执行器启动后收紧为 0750，仍保留 GID 992 的只读访问。
install -d -m 0770 -o prism-sandbox -g prism-sandbox /var/lib/prism-sandbox

install -d -m 0755 "$unit_dir"
installed_unit="$unit_dir/$service_name"
if [[ -e "$installed_unit" || -L "$installed_unit" ]]; then
  [[ -f "$installed_unit" && ! -L "$installed_unit" ]] || fail '目标 systemd 单元不是普通文件，拒绝覆盖'
  had_existing_unit=1
  unit_backup="$(mktemp "${TMPDIR:-/tmp}/prism-sandbox-previous.XXXXXX.service")"
  cp -p -- "$installed_unit" "$unit_backup"
fi
if systemctl is-active --quiet "$service_name"; then
  was_active=1
fi
if systemctl is-enabled --quiet "$service_name"; then
  was_enabled=1
fi
if ! install -m 0644 "$temp_unit" "$installed_unit"; then
  fail '安装 systemd 单元失败'
fi
unit_changed=1
if ! systemctl daemon-reload >/dev/null 2>&1 || \
  ! systemctl enable "$service_name" >/dev/null 2>&1 || \
  ! systemctl restart "$service_name" >/dev/null 2>&1; then
  if ! rollback_unit; then
    fail 'systemd 单元启动失败且回滚失败'
  fi
  fail 'systemd 单元启动失败，已回滚旧单元'
fi

health_ready=0
for _attempt in {1..30}; do
  if curl --silent --fail --max-time 5 --config "$health_config" \
    --unix-socket /var/lib/prism-sandbox/agent.sock http://localhost/health >"$health_response" 2>/dev/null; then
    if python3 - "$health_response" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as source:
        payload = json.load(source)
    result = payload.get("result", {})
    ready = (
        bool(payload.get("ok"))
        and bool(result.get("ready"))
        and bool(result.get("browser_blackbox", {}).get("ready"))
    )
except (OSError, TypeError, ValueError):
    ready = False
raise SystemExit(0 if ready else 1)
PY
    then
      health_ready=1
      break
    fi
  fi
  sleep 1
done
if [[ "$health_ready" != "1" ]]; then
  if ! rollback_unit; then
    fail '沙箱执行器 UDS Bearer health 或 browser_blackbox 未达到 ready，且回滚失败'
  fi
  fail '沙箱执行器 UDS Bearer health 或 browser_blackbox 未达到 ready，已回滚旧单元'
fi

unset sandbox_token
printf '安装完成：%s 已启动，UDS 与 browser_blackbox health 均 ready\n' "$service_name"
