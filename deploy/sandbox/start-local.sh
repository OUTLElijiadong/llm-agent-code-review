#!/usr/bin/env bash
# Start a local-development worker backed by Docker Desktop and runc.
set -Eeuo pipefail

deploy_dir="$(cd "$(dirname "$0")/.." && pwd -P)"
runtime_dir="/tmp/prism-sandbox"
mkdir -p "$runtime_dir/state" "$runtime_dir/jobs" "$runtime_dir/logs"
chmod 0700 "$runtime_dir/state" "$runtime_dir/jobs" "$runtime_dir/logs"

export SANDBOX_EXECUTOR_MODE=local_development
export SANDBOX_RUNTIME=runsc
export SANDBOX_ALLOW_RUNC_LOCAL_DEVELOPMENT=true
export SANDBOX_EXECUTOR_SOCKET="$runtime_dir/agent.sock"
export SANDBOX_STATE_DIR="$runtime_dir/state"
export SANDBOX_JOB_DIR="$runtime_dir/jobs"
export SANDBOX_AUDIT_LOG="$runtime_dir/logs/events.jsonl"
export SANDBOX_PROFILE_FILE="$deploy_dir/sandbox/profiles.json"
export SANDBOX_MAX_CONCURRENCY="${SANDBOX_MAX_CONCURRENCY:-2}"

if [[ -z "${SANDBOX_EXECUTOR_TOKEN:-}" || ${#SANDBOX_EXECUTOR_TOKEN} -lt 32 ]]; then
  printf '%s\n' '请先设置至少 32 字符的 SANDBOX_EXECUTOR_TOKEN' >&2
  exit 1
fi
python_bin="${PYTHON_BIN:-}"
if [[ -z "$python_bin" ]]; then
  for candidate in python3.13 python3.12 python3.11 python3.10; do
    if command -v "$candidate" >/dev/null 2>&1; then
      python_bin="$(command -v "$candidate")"
      break
    fi
  done
fi
[[ -n "$python_bin" ]] || { printf '%s\n' '本机需要 Python 3.10 或更高版本' >&2; exit 1; }
exec "$python_bin" "$deploy_dir/prism_sandbox_executor.py"
