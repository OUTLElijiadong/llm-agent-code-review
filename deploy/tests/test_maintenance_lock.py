"""维护锁：持有者元数据、有限等待、遗留锁识别（common.sh 的 acquire_directory_lock 等）。

背景：生产 2026-10-25 03:18 的恢复验证因“检测到并发任务或遗留锁”立即失败，当时没有记录持有者，
事后无法判断是并发任务还是遗留锁。这里验证新逻辑记录持有者、区分两种情况，并且在持有进程
仍存活时绝不抢锁。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

DEPLOY_DIR = Path(__file__).resolve().parents[1]
COMMON = DEPLOY_DIR / "lib" / "common.sh"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="需要 bash")


def run_bash(script: str, *, env: dict[str, str] | None = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    merged = {**os.environ, "PRISM_LOCK_POLL_SECONDS": "1", **(env or {})}
    return subprocess.run(
        ["bash", "-c", f'source "{COMMON}"; {script}'],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=merged,
    )


def stop_holder(holder: subprocess.Popen) -> None:
    """结束持有者及其整个进程组（bash 的子进程 sleep 也一并结束）。"""
    try:
        os.killpg(holder.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    holder.wait(timeout=5)


def read_owner(lock_dir: Path) -> dict[str, str]:
    owner_file = Path(f"{lock_dir}.owner")
    return dict(line.split("=", 1) for line in owner_file.read_text(encoding="utf-8").splitlines() if "=" in line)


def test_acquire_records_owner_and_release_cleans_both(tmp_path: Path) -> None:
    lock_dir = tmp_path / "m.lock"
    result = run_bash(f'acquire_directory_lock "{lock_dir}"; echo held; sleep 0; release_directory_lock "{lock_dir}"')
    assert result.returncode == 0, result.stderr
    assert not lock_dir.exists()
    assert not Path(f"{lock_dir}.owner").exists()


def test_owner_metadata_has_pid_start_and_command(tmp_path: Path) -> None:
    lock_dir = tmp_path / "m.lock"
    holder = subprocess.Popen(
        ["bash", "-c", f'source "{COMMON}"; acquire_directory_lock "{lock_dir}"; sleep 30'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not Path(f"{lock_dir}.owner").exists():
            time.sleep(0.05)
        owner = read_owner(lock_dir)
        assert owner["pid"] == str(holder.pid)
        assert int(owner["started_epoch"]) > 1_700_000_000
        assert owner["command"]
    finally:
        stop_holder(holder)


def test_live_holder_without_wait_fails_fast_and_never_steals(tmp_path: Path) -> None:
    lock_dir = tmp_path / "m.lock"
    holder = subprocess.Popen(
        ["bash", "-c", f'source "{COMMON}"; acquire_directory_lock "{lock_dir}"; sleep 30'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not Path(f"{lock_dir}.owner").exists():
            time.sleep(0.05)
        started = time.monotonic()
        result = run_bash(f'acquire_directory_lock "{lock_dir}"')
        assert time.monotonic() - started < 5
        assert result.returncode == 1
        assert "检测到并发任务或遗留锁" in result.stderr
        assert f"pid={holder.pid}" in result.stderr
        assert "仍在运行" in result.stderr
        assert lock_dir.is_dir(), "持有进程仍存活时不得删除或接管锁"
        assert read_owner(lock_dir)["pid"] == str(holder.pid)
    finally:
        stop_holder(holder)


def test_live_holder_with_wait_times_out_with_waited_seconds(tmp_path: Path) -> None:
    lock_dir = tmp_path / "m.lock"
    holder = subprocess.Popen(
        ["bash", "-c", f'source "{COMMON}"; acquire_directory_lock "{lock_dir}"; sleep 30'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not Path(f"{lock_dir}.owner").exists():
            time.sleep(0.05)
        started = time.monotonic()
        result = run_bash(f'acquire_directory_lock "{lock_dir}" 3')
        elapsed = time.monotonic() - started
        assert 2.5 <= elapsed < 15
        assert result.returncode == 1
        assert "已等待 3 秒" in result.stderr
        assert lock_dir.is_dir()
    finally:
        stop_holder(holder)


def test_wait_succeeds_when_holder_releases(tmp_path: Path) -> None:
    lock_dir = tmp_path / "m.lock"
    holder = subprocess.Popen(
        ["bash", "-c",
         f'source "{COMMON}"; acquire_directory_lock "{lock_dir}"; sleep 2; release_directory_lock "{lock_dir}"'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not Path(f"{lock_dir}.owner").exists():
        time.sleep(0.05)
    result = run_bash(f'acquire_directory_lock "{lock_dir}" 20; echo acquired; release_directory_lock "{lock_dir}"')
    holder.wait(timeout=10)
    assert result.returncode == 0, result.stderr
    assert "acquired" in result.stdout


def test_stale_lock_with_dead_owner_is_reported_as_leftover_and_not_removed(tmp_path: Path) -> None:
    lock_dir = tmp_path / "m.lock"
    dead = subprocess.Popen(["bash", "-c", "exit 0"])
    dead.wait()
    lock_dir.mkdir()
    Path(f"{lock_dir}.owner").write_text(
        f"pid={dead.pid}\nstarted_epoch={int(time.time()) - 600}\ncommand=verify-backup.sh\n", encoding="utf-8",
    )
    result = run_bash(f'acquire_directory_lock "{lock_dir}" 30')
    assert result.returncode == 1
    assert "遗留锁" in result.stderr
    assert "已不存在" in result.stderr
    assert f"rmdir '{lock_dir}'" in result.stderr, "要给出确认后的手工处理命令"
    assert lock_dir.is_dir(), "遗留锁不自动删除，由人确认后处理"


def test_lock_without_metadata_reports_owner_unrecorded(tmp_path: Path) -> None:
    lock_dir = tmp_path / "m.lock"
    lock_dir.mkdir()
    result = run_bash(f'acquire_directory_lock "{lock_dir}"')
    assert result.returncode == 1
    assert "持有者未记录" in result.stderr


def test_metadata_older_than_lock_directory_is_ignored(tmp_path: Path) -> None:
    """锁目录被旧版本脚本重新创建后，旧的 .owner 文件不能被当成当前持有者。"""
    lock_dir = tmp_path / "m.lock"
    owner_file = Path(f"{lock_dir}.owner")
    owner_file.write_text("pid=1\nstarted_epoch=1\ncommand=old.sh\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(owner_file, (old, old))
    lock_dir.mkdir()
    result = run_bash(f'describe_lock_owner "{lock_dir}"')
    assert result.returncode == 0
    assert result.stdout.strip() == "持有者未记录"


def test_pid_reuse_by_other_program_counts_as_dead(tmp_path: Path) -> None:
    """记录的 pid 存在但命令名不同（pid 被复用）时，按遗留锁处理。"""
    lock_dir = tmp_path / "m.lock"
    reused = subprocess.Popen(["sleep", "30"])
    try:
        lock_dir.mkdir()
        Path(f"{lock_dir}.owner").write_text(
            f"pid={reused.pid}\nstarted_epoch={int(time.time())}\ncommand=verify-backup.sh\n", encoding="utf-8",
        )
        proc_cmdline = Path(f"/proc/{reused.pid}/cmdline")
        if not proc_cmdline.exists():
            pytest.skip("需要 /proc 才能区分 pid 复用（Linux 生产环境具备，macOS 不具备）")
        result = run_bash(f'acquire_directory_lock "{lock_dir}"')
        assert result.returncode == 1
        assert "遗留锁" in result.stderr
    finally:
        reused.kill()
        reused.wait(timeout=5)


@pytest.mark.parametrize("locale_name", ["C", "POSIX", "en_US.UTF-8"])
def test_messages_are_valid_utf8_under_any_locale(tmp_path: Path, locale_name: str) -> None:
    """systemd 启动的脚本没有 locale；变量紧跟全角字符时不加花括号会被吞掉并输出残缺字节。"""
    lock_dir = tmp_path / "m.lock"
    lock_dir.mkdir()
    Path(f"{lock_dir}.owner").write_text(
        f"pid=999999\nstarted_epoch={int(time.time()) - 30}\ncommand=backup.sh\n", encoding="utf-8",
    )
    merged = {**os.environ, "PRISM_LOCK_POLL_SECONDS": "1", "LC_ALL": locale_name, "LANG": locale_name}
    raw = subprocess.run(
        ["bash", "-c", f'source "{COMMON}"; acquire_directory_lock "{lock_dir}"'],
        capture_output=True, timeout=30, env=merged,
    )
    message = raw.stderr.decode("utf-8")  # 非法 UTF-8 会在这里直接抛错
    assert str(lock_dir) in message, "锁路径必须完整出现在消息里，而不是被吞掉"
    assert "（" in message and "）" in message
    assert "pid=999999" in message


def test_invalid_wait_seconds_is_rejected(tmp_path: Path) -> None:
    result = run_bash(f'acquire_directory_lock "{tmp_path / "m.lock"}" abc')
    assert result.returncode == 1
    assert "锁等待秒数必须是非负整数" in result.stderr
