"""deploy/maintenance/cleanup.sh：阶段独立、Docker 配置目录、safe.directory、遗留锁。

背景（生产 2026-10-24 至 10-30 连续 7 次失败，已用 systemd-run 在生产复现）：
prism-maintenance.service 带 ProtectHome=true，docker buildx 创建 /root/.docker 失败，
脚本在 `set -e` 下立即退出，后面的 journal 与日志清理从未执行。
全部使用假 docker / journalctl / fuser / flock 与临时目录，不触碰真实系统。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

DEPLOY_DIR = Path(__file__).resolve().parents[1]
CLEANUP = DEPLOY_DIR / "maintenance" / "cleanup.sh"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="需要 bash")

FAKE_DOCKER = r"""#!/usr/bin/env bash
printf 'docker %s | DOCKER_CONFIG=%s HOME=%s\n' "$*" "${DOCKER_CONFIG:-unset}" "${HOME:-unset}" >> "${FAKE_LOG:?}"
case "$1 $2" in
  "version --format") echo "27.0.0-fake"; exit 0 ;;
  "builder prune")
    if [ "${FAKE_DOCKER_PRUNE:-ok}" = fail ]; then
      echo "ERROR: mkdir /root/.docker: read-only file system" >&2
      echo "exit status 1" >&2
      exit 1
    fi
    echo "Total reclaimed space: 0B"; exit 0 ;;
esac
exit 0
"""

FAKE_JOURNALCTL = r"""#!/usr/bin/env bash
printf 'journalctl %s\n' "$*" >> "${FAKE_LOG:?}"
[ "${FAKE_JOURNAL:-ok}" = fail ] && { echo "Failed to vacuum" >&2; exit 1; }
exit 0
"""

FAKE_FLOCK = r"""#!/usr/bin/env bash
exit "${FAKE_FLOCK_RC:-0}"
"""

FAKE_FUSER = r"""#!/usr/bin/env bash
exit 1
"""


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    """假命令放在 PATH 最前；日志、锁、proc 全部指向临时目录。"""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("journalctl", FAKE_JOURNALCTL),
                       ("flock", FAKE_FLOCK), ("fuser", FAKE_FUSER)):
        path = fake_bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    logs = tmp_path / "logs"
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text("MemAvailable: 1000 kB\nSwapFree: 2000 kB\n", encoding="utf-8")
    sensitive_markers = ("_TOKEN", "_KEY", "_SECRET", "_PASSWORD", "_CREDENTIAL")
    base = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DOCKER_CONFIG", "PRISM_LOCK_WAIT_SECONDS"}
        and not any(marker in key.upper() for marker in sensitive_markers)
    }
    base.update({
        "PATH": f"{fake_bin}:{base['PATH']}",
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "PRISM_MAINTENANCE_LOG_DIR": str(logs),
        "PRISM_PROC_ROOT": str(proc),
        "PRISM_MAINTENANCE_FLOCK_FILE": str(tmp_path / "flock"),
        "PRISM_BUNDLE_SEARCH_DIRS_FIRST": str(tmp_path / "bundles1"),
        "PRISM_BUNDLE_SEARCH_DIRS_SECOND": str(tmp_path / "bundles2"),
        "TMPDIR": str(tmp_path),
        "LC_ALL": "C",
    })
    (tmp_path / "bundles1").mkdir()
    (tmp_path / "bundles2").mkdir()
    return base


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """复制 deploy 的最小副本，让 cleanup.sh 的 repo_dir/.releases 落在临时目录。"""
    root = tmp_path / "repo"
    (root / "deploy" / "maintenance").mkdir(parents=True)
    (root / "deploy" / "lib").mkdir()
    (root / "deploy" / ".releases").mkdir()
    shutil.copy2(CLEANUP, root / "deploy" / "maintenance" / "cleanup.sh")
    shutil.copy2(DEPLOY_DIR / "lib" / "common.sh", root / "deploy" / "lib" / "common.sh")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def run_cleanup(repo: Path, env: dict[str, str], *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    merged = {**env, **extra}
    return subprocess.run(
        ["bash", str(repo / "deploy" / "maintenance" / "cleanup.sh"), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=merged, timeout=60,
    )


def latest_log(env: dict[str, str]) -> str:
    logs = sorted(Path(env["PRISM_MAINTENANCE_LOG_DIR"]).glob("cleanup-*.log"))
    assert logs, "没有生成维护日志"
    return logs[-1].read_text(encoding="utf-8")


def calls(env: dict[str, str]) -> str:
    path = Path(env["FAKE_LOG"])
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_apply_success_runs_every_stage_and_exits_zero(repo: Path, env: dict[str, str]) -> None:
    result = run_cleanup(repo, env, "--apply")
    assert result.returncode == 0, result.stderr
    log = latest_log(env)
    for stage in ("transfer_bundles", "docker_builder_cache", "journal_vacuum", "maintenance_logs"):
        assert f"STAGE ok: {stage}" in log
    assert "SUMMARY ok=4 failed=0 skipped=0" in log
    assert "Maintenance cleanup finished" in log


def test_docker_failure_does_not_stop_later_stages_and_exits_nonzero(repo: Path, env: dict[str, str]) -> None:
    """这是生产故障的直接回归：docker 失败后 journal 清理与日志清理必须仍然执行。"""
    result = run_cleanup(repo, env, "--apply", FAKE_DOCKER_PRUNE="fail")
    assert result.returncode == 1
    log = latest_log(env)
    assert "STAGE FAILED: docker_builder_cache exit=1" in log
    assert "mkdir /root/.docker: read-only file system" in log, "错误原文必须进维护日志"
    assert "STAGE ok: journal_vacuum" in log
    assert "STAGE ok: maintenance_logs" in log
    assert "journalctl --vacuum-time=7d" in calls(env)
    assert "SUMMARY ok=3 failed=1 skipped=0" in log
    assert "SUMMARY failed stages: docker_builder_cache(exit=1)" in log
    assert "Maintenance cleanup finished with failures" in log


def test_journal_failure_is_reported_but_logs_still_cleaned(repo: Path, env: dict[str, str]) -> None:
    result = run_cleanup(repo, env, "--apply", FAKE_JOURNAL="fail")
    assert result.returncode == 1
    log = latest_log(env)
    assert "STAGE FAILED: journal_vacuum exit=1" in log
    assert "STAGE ok: docker_builder_cache" in log
    assert "STAGE ok: maintenance_logs" in log


def test_expired_logs_removed_and_current_log_kept(repo: Path, env: dict[str, str]) -> None:
    logs = Path(env["PRISM_MAINTENANCE_LOG_DIR"])
    logs.mkdir(parents=True)
    old = logs / "cleanup-20200101T000000Z-1.log"
    old.write_text("old", encoding="utf-8")
    ancient = time.time() - 30 * 86400
    os.utime(old, (ancient, ancient))
    fresh = logs / "cleanup-29990101T000000Z-2.log"
    fresh.write_text("fresh", encoding="utf-8")
    result = run_cleanup(repo, env, "--apply", FAKE_DOCKER_PRUNE="fail")
    assert result.returncode == 1
    assert not old.exists(), "docker 失败后过期日志仍应被清理"
    assert fresh.exists()
    assert len(list(logs.glob("cleanup-*.log"))) >= 2, "本次运行日志不得被删除"


def test_docker_config_is_set_and_writable_when_unset(repo: Path, env: dict[str, str]) -> None:
    result = run_cleanup(repo, env, "--apply")
    assert result.returncode == 0, result.stderr
    prune_call = next(line for line in calls(env).splitlines() if "builder prune" in line)
    assert "DOCKER_CONFIG=unset" not in prune_call
    config_dir = prune_call.split("DOCKER_CONFIG=", 1)[1].split(" HOME=", 1)[0]
    assert config_dir.startswith(env["TMPDIR"]), "手工运行时应落在私有临时目录"
    assert not Path(config_dir).exists(), "退出后必须清理临时配置目录"


def test_docker_config_from_unit_is_respected_and_not_deleted(repo: Path, env: dict[str, str], tmp_path: Path) -> None:
    runtime = tmp_path / "runtime" / "docker"
    result = run_cleanup(repo, env, "--apply", DOCKER_CONFIG=str(runtime))
    assert result.returncode == 0, result.stderr
    prune_call = next(line for line in calls(env).splitlines() if "builder prune" in line)
    assert f"DOCKER_CONFIG={runtime}" in prune_call
    assert runtime.is_dir(), "unit 提供的目录由 systemd 管理，脚本不得删除"


def test_diagnostics_are_logged(repo: Path, env: dict[str, str]) -> None:
    run_cleanup(repo, env, "--apply")
    log = latest_log(env)
    assert "diagnostics: user=" in log
    assert "docker_client=27.0.0-fake" in log


def test_dry_run_changes_nothing_and_does_not_hold_lock(repo: Path, env: dict[str, str]) -> None:
    result = run_cleanup(repo, env)
    assert result.returncode == 0, result.stderr
    assert "builder prune" not in calls(env)
    assert "journalctl" not in calls(env)
    assert not (repo / "deploy" / ".releases" / ".maintenance.lock").exists()
    assert "mode=dry-run" in latest_log(env)


def test_lock_is_released_after_failure(repo: Path, env: dict[str, str]) -> None:
    run_cleanup(repo, env, "--apply", FAKE_DOCKER_PRUNE="fail")
    lock = repo / "deploy" / ".releases" / ".maintenance.lock"
    assert not lock.exists()
    assert not Path(f"{lock}.owner").exists()


def test_shared_lock_held_by_live_process_is_a_normal_skip(repo: Path, env: dict[str, str]) -> None:
    lock = repo / "deploy" / ".releases" / ".maintenance.lock"
    proc_root = Path(env["PRISM_PROC_ROOT"])
    boot_id = "01234567-89ab-cdef-0123-456789abcdef"
    boot_id_file = proc_root / "sys" / "kernel" / "random" / "boot_id"
    boot_id_file.parent.mkdir(parents=True)
    boot_id_file.write_text(f"{boot_id}\n", encoding="utf-8")
    holder = subprocess.Popen(
        ["sleep", "30"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        holder_proc = proc_root / str(holder.pid)
        holder_proc.mkdir()
        # linux_process_identity reads state (field 3) and start ticks (field 22).
        stat_fields = ["S", *("0" for _ in range(18)), "12345"]
        (holder_proc / "stat").write_text(
            f"{holder.pid} (bash) {' '.join(stat_fields)}\n", encoding="utf-8",
        )
        lock.mkdir()
        Path(f"{lock}.owner").write_text(
            f"pid={holder.pid}\nstarted_epoch={int(time.time())}\ncommand=sleep\n"
            f"process_start_ticks=12345\nboot_id={boot_id}\n",
            encoding="utf-8",
        )
        result = run_cleanup(repo, env, "--apply")
        assert result.returncode == 0, latest_log(env)
        log = latest_log(env)
        assert "readonly variable" not in log
        assert "SKIP: deployment, backup, rollback, or another maintenance task holds the shared lock" in log
        assert f"pid={holder.pid}" in log
        assert "builder prune" not in calls(env)
        assert lock.is_dir(), "持有者存活时不得动它的锁"
    finally:
        import signal
        os.killpg(holder.pid, signal.SIGKILL)
        holder.wait(timeout=5)


def test_shared_lock_with_dead_owner_fails_visibly_with_manual_command(repo: Path, env: dict[str, str]) -> None:
    lock = repo / "deploy" / ".releases" / ".maintenance.lock"
    dead = subprocess.Popen(["bash", "-c", "exit 0"])
    dead.wait()
    lock.mkdir()
    Path(f"{lock}.owner").write_text(
        f"pid={dead.pid}\nstarted_epoch={int(time.time()) - 60}\ncommand=verify-backup.sh\n", encoding="utf-8",
    )
    result = run_cleanup(repo, env, "--apply")
    assert result.returncode == 1
    log = latest_log(env)
    assert "ERROR: shared maintenance lock is a leftover" in log
    assert f"rmdir '{lock}'" in log
    assert lock.is_dir(), "遗留锁不自动删除"
    assert "builder prune" not in calls(env)


def test_shared_lock_without_owner_is_skipped_until_stale_then_fails(repo: Path, env: dict[str, str]) -> None:
    """无法确认持有者：先正常跳过（可能是旧版本脚本的合法持有者），超过阈值才算可疑遗留锁。"""
    lock = repo / "deploy" / ".releases" / ".maintenance.lock"
    lock.mkdir()
    fresh = run_cleanup(repo, env, "--apply")
    assert fresh.returncode == 0
    assert "SKIP: deployment, backup, rollback" in latest_log(env)
    old = time.time() - 8 * 3600
    os.utime(lock, (old, old))
    for stale_log in Path(env["PRISM_MAINTENANCE_LOG_DIR"]).glob("cleanup-*.log"):
        stale_log.unlink()
    stale = run_cleanup(repo, env, "--apply")
    assert stale.returncode == 1
    assert "cannot be confirmed" in latest_log(env)


def test_unreadable_repository_is_reported_distinctly_from_shallow(repo: Path, env: dict[str, str]) -> None:
    """git 因属主检查等原因读不了仓库时，不能再冒充“历史不完整”（生产误导日志的根因）。"""
    fake_git_dir = Path(env["TMPDIR"]) / "gitbin"
    fake_git_dir.mkdir()
    fake_git = fake_git_dir / "git"
    fake_git.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"rev-parse --is-shallow-repository"* ]]; then\n'
        '  echo "fatal: detected dubious ownership in repository at \'/opt/code-review\'" >&2; exit 128\n'
        "fi\nexec /usr/bin/git \"$@\"\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    result = run_cleanup(repo, env, "--apply", PATH=f"{fake_git_dir}:{env['PATH']}")
    assert result.returncode == 0, result.stderr
    log = latest_log(env)
    assert "cannot read repository state" in log
    assert "dubious ownership" in log
    assert "history is incomplete" not in log


def test_git_calls_pass_safe_directory(repo: Path, env: dict[str, str]) -> None:
    fake_git_dir = Path(env["TMPDIR"]) / "gitbin2"
    fake_git_dir.mkdir()
    fake_git = fake_git_dir / "git"
    fake_git.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "${FAKE_LOG:?}"\nexec /usr/bin/git "$@"\n', encoding="utf-8",
    )
    fake_git.chmod(0o755)
    run_cleanup(repo, env, "--apply", PATH=f"{fake_git_dir}:{env['PATH']}")
    git_calls = [line for line in calls(env).splitlines() if "is-shallow-repository" in line]
    assert git_calls, "应当调用过 git rev-parse --is-shallow-repository"
    assert all(f"safe.directory={repo}" in line or f"safe.directory={repo.resolve()}" in line for line in git_calls)


def test_non_utf8_locale_does_not_garble_messages(repo: Path, env: dict[str, str]) -> None:
    result = run_cleanup(repo, env, "--apply", LC_ALL="C", LANG="C")
    assert result.returncode == 0
    latest_log(env).encode("utf-8")


def test_unknown_argument_and_help(repo: Path, env: dict[str, str]) -> None:
    assert run_cleanup(repo, env, "--bogus").returncode == 2
    assert run_cleanup(repo, env, "--help").returncode == 0
