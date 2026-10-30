"""ops-check：纳入维护任务与恢复验证的最近一次执行结果（unit_health.sh）。

背景：生产 prism-maintenance.service 从 2026-10-24 起连续 7 天失败，恢复验证 10-25 失败，
2026-10-30 的巡检仍输出“全部生产关键检查通过”。约束：deploy.sh 发布后会运行 ops-check，
status=error 会让发布失败，所以维护/恢复验证的历史失败只能降级（degraded），不能阻断发布。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

DEPLOY_DIR = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="需要 bash")

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
# 用法: systemctl show <unit> --property=<名> --value ；数据来自 $FAKE_UNITS_DIR/<unit>.<名>
[[ "$1" == show ]] || exit 0
unit="$2"; prop="${3#--property=}"
dir="${FAKE_UNITS_DIR:?}"
if [[ "$prop" == LoadState ]]; then
  [[ -f "$dir/$unit.LoadState" ]] && cat "$dir/$unit.LoadState" || echo not-found
  exit 0
fi
[[ -f "$dir/$unit.$prop" ]] && cat "$dir/$unit.$prop" || true
exit 0
"""

FAKE_JOURNALCTL = r"""#!/usr/bin/env bash
cat "${FAKE_JOURNAL_FILE:-/dev/null}" 2>/dev/null
exit 0
"""


def write_unit(units: Path, name: str, *, active: str, result: str, status: str, exit_at: str) -> None:
    props = {"LoadState": "loaded", "ActiveState": active, "Result": result,
             "ExecMainStatus": status, "ExecMainExitTimestamp": exit_at, "StateChangeTimestamp": exit_at}
    for key, value in props.items():
        (units / f"{name}.{key}").write_text(value + "\n", encoding="utf-8")


def collect(unit: str, env: dict[str, str], *, max_age: int = 30, log_dir: str = "", log_prefix: str = "") -> dict:
    """调用 collect_unit_health + unit_health_json，返回解析后的 JSON。"""
    script = (f'collect_unit_health u "{unit}" {max_age} "{log_dir}" "{log_prefix}"; '
              f'unit_health_json u "{unit}" {max_age}')
    return json.loads(bash_source(script, env))


def bash_source(script: str, env: dict[str, str]) -> str:
    out = subprocess.run(
        ["bash", "-c", f'cd "{DEPLOY_DIR}"; source lib/common.sh; json_escape() {{ printf "%s" "$1"; }}; '
         f'source lib/unit_health.sh; {script}'],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("systemctl", FAKE_SYSTEMCTL), ("journalctl", FAKE_JOURNALCTL)):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    units = tmp_path / "units"
    units.mkdir()
    merged = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_UNITS_DIR": str(units), "LC_ALL": "C"}
    return merged


def parse(text: str) -> dict:
    return json.loads(text)


def date_text(epoch: int) -> str:
    # 与生产 systemctl 输出同形：Sun 2026-10-25 03:18:29 CST
    return time.strftime("%a %Y-%m-%d %H:%M:%S %Z", time.localtime(epoch))


def test_failed_unit_is_degraded_with_exit_age_and_reason(env: dict[str, str]) -> None:
    units = Path(env["FAKE_UNITS_DIR"])
    write_unit(units, "prism-maintenance.service", active="failed", result="exit-code", status="1",
               exit_at=date_text(int(time.time()) - 5 * 3600))
    data = collect("prism-maintenance.service", env)
    assert data["job_state"] == "failed"
    assert data["status"] == "degraded" and data["ok"] is False
    assert data["result"] == "exit-code" and data["exit_status"] == "1"
    assert 4 <= data["age_hours"] <= 6
    assert "result=exit-code" in data["reason"]


def test_failure_reason_comes_from_service_log_directory(env: dict[str, str], tmp_path: Path) -> None:
    """错误原文只在维护日志目录里，journal 只有 status=1/FAILURE。"""
    units = Path(env["FAKE_UNITS_DIR"])
    write_unit(units, "prism-maintenance.service", active="failed", result="exit-code", status="1",
               exit_at=date_text(int(time.time()) - 3600))
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "cleanup-20261029T203000Z-1.log").write_text(
        "[t] mode=apply\n[t] Pruning reclaimable Docker builder cache older than 168h\n"
        "ERROR: mkdir /root/.docker: read-only file system\n", encoding="utf-8",
    )
    data = collect("prism-maintenance.service", env, log_dir=str(logs), log_prefix="cleanup-")
    assert "mkdir /root/.docker: read-only file system" in data["reason"]


def test_failure_reason_falls_back_to_journal_when_no_log_dir(env: dict[str, str], tmp_path: Path) -> None:
    units = Path(env["FAKE_UNITS_DIR"])
    write_unit(units, "prism-verify-backup.service", active="failed", result="exit-code", status="1",
               exit_at=date_text(int(time.time()) - 3600))
    journal = tmp_path / "journal.txt"
    journal.write_text("[t] INFO started\n[t] ERROR 检测到并发任务或遗留锁: .releases/.maintenance.lock\n", encoding="utf-8")
    env["FAKE_JOURNAL_FILE"] = str(journal)
    data = collect("prism-verify-backup.service", env, max_age=216)
    assert "并发任务或遗留锁" in data["reason"]


def test_recent_success_is_ok(env: dict[str, str]) -> None:
    units = Path(env["FAKE_UNITS_DIR"])
    write_unit(units, "prism-backup.service", active="inactive", result="success", status="0",
               exit_at=date_text(int(time.time()) - 3600))
    data = collect("prism-backup.service", env)
    assert data["job_state"] == "ok" and data["status"] == "ok" and data["ok"] is True
    assert data["stale"] is False and data["reason"] == ""


def test_old_success_is_stale_and_degraded(env: dict[str, str]) -> None:
    """定时器停了没人知道：最近一次成功已远超预期间隔，也要降级提示。"""
    units = Path(env["FAKE_UNITS_DIR"])
    write_unit(units, "prism-verify-backup.service", active="inactive", result="success", status="0",
               exit_at=date_text(int(time.time()) - 400 * 3600))
    data = collect("prism-verify-backup.service", env, max_age=216)
    assert data["job_state"] == "ok" and data["stale"] is True
    assert data["status"] == "degraded"
    assert "超过 216 小时" in data["reason"]


def test_running_unit_is_not_judged(env: dict[str, str]) -> None:
    units = Path(env["FAKE_UNITS_DIR"])
    write_unit(units, "prism-maintenance.service", active="activating", result="success", status="0", exit_at="")
    data = collect("prism-maintenance.service", env)
    assert data["job_state"] == "running" and data["status"] == "ok"


def test_missing_unit_is_not_installed_and_ok(env: dict[str, str]) -> None:
    data = collect("prism-nosuch.service", env)
    assert data["job_state"] == "not_installed" and data["status"] == "ok"


def test_never_run_is_reported_but_not_degraded(env: dict[str, str]) -> None:
    """systemd 重启会丢失单元历史；没有记录既不是失败也不是成功，如实报告但不制造告警。"""
    units = Path(env["FAKE_UNITS_DIR"])
    write_unit(units, "prism-verify-backup.service", active="inactive", result="success", status="0", exit_at="")
    data = collect("prism-verify-backup.service", env, max_age=216)
    assert data["job_state"] == "never_run" and data["status"] == "ok"
    assert "没有执行记录" in data["reason"]


def test_systemctl_unavailable_reports_unknown_and_ok(env: dict[str, str], tmp_path: Path) -> None:
    empty = tmp_path / "emptybin"
    empty.mkdir()
    for tool in ("bash", "date", "sed", "head", "tr", "cut", "tail", "grep"):
        found = shutil.which(tool)
        if found:
            (empty / tool).symlink_to(found)
    isolated = {**env, "PATH": str(empty)}
    out = subprocess.run(
        [shutil.which("bash") or "bash", "-c",
         f'cd "{DEPLOY_DIR}"; source lib/common.sh; json_escape() {{ printf "%s" "$1"; }}; source lib/unit_health.sh; '
         f'collect_unit_health u prism-x.service 30; unit_health_json u prism-x.service 30'],
        capture_output=True, text=True, env=isolated, timeout=30,
    )
    data = json.loads(out.stdout)
    assert data["job_state"] == "unknown" and data["status"] == "ok"


def test_output_is_valid_json_with_special_characters(env: dict[str, str], tmp_path: Path) -> None:
    """日志里的引号、反斜杠、制表符不能破坏 ops-check 的 JSON（用 ops-check.sh 里真实的 json_escape）。"""
    units = Path(env["FAKE_UNITS_DIR"])
    write_unit(units, "prism-maintenance.service", active="failed", result="exit-code", status="1",
               exit_at=date_text(int(time.time()) - 3600))
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "cleanup-1.log").write_text('ERROR: bad "quote" \\ backslash\ttab\n', encoding="utf-8")
    # 取 ops-check.sh 里 json_escape 的真实定义，避免测试自己再实现一份。
    source = (DEPLOY_DIR / "ops-check.sh").read_text(encoding="utf-8")
    start = source.index("json_escape() {")
    end = source.index("\n}\n", start) + 3
    definition = tmp_path / "json_escape.sh"
    definition.write_text(source[start:end] + "\n", encoding="utf-8")
    script = (f'cd "{DEPLOY_DIR}"; source lib/common.sh; source "{definition}"; source lib/unit_health.sh; '
              f'collect_unit_health u prism-maintenance.service 30 "{logs}" cleanup-; '
              f'unit_health_json u prism-maintenance.service 30')
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0, out.stderr
    assert 'bad "quote"' in json.loads(out.stdout)["reason"]


def test_ops_check_sources_every_library_it_needs() -> None:
    """ops-check.sh source 的每个 lib 文件都必须真实存在；
    发布树与测试工作区靠“复制 lib 文件”搭建，新增依赖漏拷会在运行时才炸（见 test_scripts.sh）。"""
    import re

    source = (DEPLOY_DIR / "ops-check.sh").read_text(encoding="utf-8")
    sourced = re.findall(r'^source "(lib/[^"]+)"', source, flags=re.MULTILINE)
    assert "lib/common.sh" in sourced and "lib/unit_health.sh" in sourced
    for relative in sourced:
        assert (DEPLOY_DIR / relative).is_file(), relative
    script = (DEPLOY_DIR / "tests" / "test_scripts.sh").read_text(encoding="utf-8")
    assert script.count("lib/unit_health.sh") >= 2, "test_scripts.sh 搭建 ops-check 工作区时必须复制 unit_health.sh"
