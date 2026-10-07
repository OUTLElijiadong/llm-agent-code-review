"""独立沙箱执行器安全边界与协议回归。"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import importlib.util
import io
import json
import shutil
import stat
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.permission_codes import PermissionCode
from app.models.agent_capability import SandboxArtifact, SandboxEnvironment, SandboxEvent, SandboxWorker
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.project_source_archive import ProjectSourceArchive
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.services import sandbox_service

MODULE_PATH = Path(__file__).resolve().parents[2] / "deploy" / "prism_sandbox_executor.py"
RUNNER_PATH = Path(__file__).resolve().parents[2] / "deploy" / "sandbox" / "runner.sh"
SPEC = importlib.util.spec_from_file_location("prism_sandbox_executor", MODULE_PATH)
assert SPEC and SPEC.loader
executor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = executor
SPEC.loader.exec_module(executor)


def _archive(files: dict[str, bytes | str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, content in files.items():
            bundle.writestr(name, content.encode("utf-8") if isinstance(content, str) else content)
    return output.getvalue()


@pytest.fixture
def independent_transaction_db(tmp_path: Path):
    """独立授权事务使用不同 SQLite 连接，不回滚主事务未提交的制品。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'executor.db'}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        yield session
    engine.dispose()


def _payload(archive: bytes, **overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "request_id": "sandbox-request-01",
        "purpose": "test",
        "language": "python",
        "test_mode": "whitebox",
        "source_archive_base64": base64.b64encode(archive).decode("ascii"),
        "source_sha256": hashlib.sha256(archive).hexdigest(),
        "ttl_seconds": 3600,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(executor, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(executor, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(executor, "AUDIT_LOG", tmp_path / "audit" / "events.jsonl")
    monkeypatch.setattr(executor, "EXECUTOR_MODE", "local_development")
    monkeypatch.setattr(executor, "MAX_ARCHIVE_BYTES", 2 * 1024 * 1024)
    monkeypatch.setattr(executor, "MAX_EXPANDED_BYTES", 4 * 1024 * 1024)
    executor.MONITOR_THREADS.clear()
    with executor.STATE_CONDITION:
        executor.PENDING_SUBMISSIONS.clear()
        executor.SUBMISSIONS_INFLIGHT.clear()
        executor.STOPPING_REQUESTS.clear()
    yield tmp_path
    executor.MONITOR_THREADS.clear()
    with executor.STATE_CONDITION:
        executor.PENDING_SUBMISSIONS.clear()
        executor.SUBMISSIONS_INFLIGHT.clear()
        executor.STOPPING_REQUESTS.clear()


def test_execute_rejects_arbitrary_command_image_mount_and_environment() -> None:
    archive = _archive({"main.py": "print('ok')\n"})
    for field, value in (
        ("command", "id"),
        ("image", "alpine:latest"),
        ("mount", "/:/host"),
        ("host_path", "/etc"),
        ("env", {"TOKEN": "secret"}),
        ("docker_args", ["--privileged"]),
        ("upstream", "http://169.254.169.254"),
    ):
        with pytest.raises(ValueError, match="未允许字段"):
            executor._validate_execute(_payload(archive, **{field: value}))


@pytest.mark.parametrize(
    ("test_mode", "outcome", "expected"),
    [
        ("whitebox", "succeeded", "白盒测试完成"),
        ("whitebox", "failed", "白盒测试失败"),
        ("blackbox", "succeeded", "黑盒测试完成"),
        ("blackbox", "failed", "黑盒测试失败"),
        ("combined", "succeeded", "黑白盒测试完成"),
        ("combined", "failed", "黑白盒测试失败"),
    ],
)
def test_terminal_test_message_matches_requested_mode(
    test_mode: str,
    outcome: str,
    expected: str,
) -> None:
    assert executor._test_terminal_message(test_mode, outcome) == expected


@pytest.mark.parametrize(
    ("wait_output", "expected_outcome", "expected_message"),
    [
        ("0\n", "succeeded", "黑盒测试完成"),
        ("1\n", "failed", "黑盒测试失败"),
    ],
)
def test_monitor_test_uses_requested_mode_for_terminal_event(
    isolated_paths: Path,
    monkeypatch,
    wait_output: str,
    expected_outcome: str,
    expected_message: str,
) -> None:
    request_id = "blackbox-terminal-event"
    profile = executor._load_profiles()["python"]
    payload = _payload(
        _archive({"review_sample.py": "VALUE = 1\n"}),
        request_id=request_id,
        test_mode="blackbox",
    )
    state = executor._new_state(payload, profile, executor._request_digest(payload))
    state.update({"status": "running_whitebox", "container_name": "prism-test"})
    executor._write_state(state)

    monkeypatch.setattr(executor, "_run_command", lambda *_args, **_kwargs: {"stdout": wait_output})
    monkeypatch.setattr(executor, "_inspect_container", lambda _container: {"OOMKilled": False})
    monkeypatch.setattr(executor, "_collect_logs_safe", lambda _container: {"text": "", "bytes_seen": 0})
    terminal_events: list[dict[str, object]] = []
    monkeypatch.setattr(
        executor,
        "_queue_terminal_cleanup",
        lambda _request_id, **kwargs: terminal_events.append(kwargs) or True,
    )

    executor._monitor_test(request_id, profile)

    assert terminal_events[0]["status_value"] == expected_outcome
    assert terminal_events[0]["message"] == expected_message


def test_backend_execute_projection_matches_worker_contract_for_initial_and_repair_rounds() -> None:
    from app.services.sandbox_service import _worker_execute_payload

    archive = _archive({"main.py": "print('ok')\n"})
    encoded = base64.b64encode(archive).decode("ascii")
    source_sha256 = hashlib.sha256(archive).hexdigest()
    base = {
        "request_id": "sandbox-request-01",
        "purpose": "test",
        "language": "python",
        "test_mode": "whitebox",
        "db_type": "none",
        "source_sha256": source_sha256,
        "ttl_seconds": 3600,
        "image_digest": "",
        "source_revision_id": 18,
        "source_revision_sha256": "revision-sha",
        "repair_round": 0,
    }
    repair = {
        **base,
        "request_id": "sandbox-request-01-r1",
        "repair_round": 1,
        "repair_revision_id": 27,
    }

    for envelope in (base, repair):
        payload = _worker_execute_payload(envelope, encoded)
        normalized, decoded, _profile = executor._validate_execute(payload)
        assert normalized["request_id"] == envelope["request_id"]
        assert decoded == archive
        assert normalized["source_sha256"] == source_sha256
        assert set(payload) == executor.EXECUTE_KEYS
        assert "repair_round" not in payload
        assert "source_revision_id" not in payload
        assert "repair_revision_id" not in payload


@pytest.mark.parametrize(
    ("route_status", "expected_exit", "expected_receipt"),
    [(204, 0, '"passed":true'), (503, 1, '"passed":false')],
)
def test_real_blackbox_runner_accepts_api_only_health_route_and_records_failure(
    tmp_path: Path, route_status: int, expected_exit: int, expected_receipt: str,
) -> None:
    """Run the actual trusted runner against an app whose root is 404 but health API is known."""
    import os
    import socket

    source = tmp_path / "source"
    source.mkdir()
    # The route declaration helps the runner discover framework-specific paths
    # which are not in the common health-route list.
    (source / "wsgi.py").write_text(
        "# path('api/v1/health', health_view)\n"
        "def application(environ, start_response):\n"
        "    if environ.get('PATH_INFO') == '/api/v1/health':\n"
        f"        status = '{route_status} Test'\n"
        "        body = b''\n"
        "    else:\n"
        "        status = '404 Not Found'\n"
        "        body = b'not found'\n"
        "    start_response(status, [('Content-Length', str(len(body)))])\n"
        "    return [body]\n",
        encoding="utf-8",
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    python_bin = str(Path(sys.executable).parent)
    env.update({
        "PATH": python_bin + os.pathsep + env.get("PATH", ""),
        "PRISM_ACTION": "test",
        "PRISM_LANGUAGE": "python",
        "PRISM_TEST_MODE": "blackbox",
        "PRISM_PREVIEW_PORT": str(port),
        "PRISM_SOURCE_DIR": str(source),
        "PRISM_WORKSPACE_DIR": str(tmp_path / "workspace"),
    })
    completed = subprocess.run(
        ["bash", str(RUNNER_PATH)], cwd=tmp_path, env=env,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert completed.returncode == expected_exit, completed.stdout + completed.stderr
    output = completed.stdout + completed.stderr
    assert "blackbox loopback status=404" in output
    assert "blackbox route /api/v1/health" in output
    assert "PRISM_BLACKBOX_DONE" in output
    assert expected_receipt in output
    assert f'"status_code":{route_status}' in output
    assert '"agent_assertions_passed":null' in output
    receipt = sandbox_service._extract_blackbox_result(output)
    assert receipt is not None
    assert receipt["agent_assertions_passed"] is None
    assert receipt["basis"] == "route_smoke"
    assert receipt["status"] == ("passed" if route_status == 204 else "failed")


def test_real_blackbox_runner_marks_executed_agent_assertions_as_passed(tmp_path: Path) -> None:
    """动态断言真实执行后才标记为 true。"""
    import os
    import socket

    source = tmp_path / "source"
    source.mkdir()
    (source / "wsgi.py").write_text(
        "# path('/healthz', health_view)\n"
        "def application(environ, start_response):\n"
        "    status = '204 No Content' if environ.get('PATH_INFO') == '/healthz' else '404 Not Found'\n"
        "    start_response(status, [])\n"
        "    return []\n",
        encoding="utf-8",
    )
    tests_dir = source / "_agent_tests"
    tests_dir.mkdir()
    (tests_dir / "blackbox.py").write_text(
        "import os\n"
        "from urllib.request import urlopen\n"
        "url = 'http://127.0.0.1:' + os.environ['PRISM_PREVIEW_PORT'] + '/healthz'\n"
        "with urlopen(url, timeout=3) as response:\n"
        "    assert response.status == 204\n",
        encoding="utf-8",
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    env = os.environ.copy()
    env.update({
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}",
        "PRISM_ACTION": "test",
        "PRISM_LANGUAGE": "python",
        "PRISM_TEST_MODE": "blackbox",
        "PRISM_PREVIEW_PORT": str(port),
        "PRISM_SOURCE_DIR": str(source),
        "PRISM_WORKSPACE_DIR": str(tmp_path / "workspace"),
    })

    completed = subprocess.run(
        ["bash", str(RUNNER_PATH)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt = sandbox_service._extract_blackbox_result(completed.stdout + completed.stderr)
    assert receipt is not None
    assert receipt["status"] == "passed"
    assert receipt["route_passed"] is True
    assert receipt["agent_assertions_passed"] is True


def test_fixed_profiles_cover_five_languages_and_are_bounded() -> None:
    profiles = executor._load_profiles()
    assert set(profiles) == {"python", "node", "java", "go", "php"}
    for language, profile in profiles.items():
        assert profile.language == language
        assert 128 <= profile.memory_mb <= 2048
        assert 0.1 <= profile.cpus <= 2
        assert 16 <= profile.pids <= 512
        assert 10 <= profile.test_timeout_seconds <= 900
        assert len(profile.fingerprint) == 64


def test_strict_runtime_fails_closed_and_local_development_is_explicit(monkeypatch) -> None:
    monkeypatch.setattr(executor, "REQUIRED_RUNTIME", "runsc")
    monkeypatch.setattr(executor, "_docker_runtimes", lambda: {"runc"})

    monkeypatch.setattr(executor, "EXECUTOR_MODE", "strict")
    monkeypatch.setattr(executor, "ALLOW_RUNC_LOCAL", True)
    with pytest.raises(executor.BlockedError, match="runsc"):
        executor._resolve_runtime()

    monkeypatch.setattr(executor, "EXECUTOR_MODE", "local_development")
    monkeypatch.setattr(executor, "ALLOW_RUNC_LOCAL", False)
    with pytest.raises(executor.BlockedError, match="runsc"):
        executor._resolve_runtime()

    monkeypatch.setattr(executor, "ALLOW_RUNC_LOCAL", True)
    assert executor._resolve_runtime() == "runc"


def test_strict_image_requires_allowlisted_digest(monkeypatch) -> None:
    profile = executor._load_profiles()["python"]
    profile = dataclasses.replace(profile, digest="")  # 强制无 digest,验证 strict 拦截
    monkeypatch.setattr(executor, "EXECUTOR_MODE", "strict")
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("digest 缺失时不能访问镜像")

    monkeypatch.setattr(executor, "_run_command", fake_run)
    with pytest.raises(executor.BlockedError, match="digest"):
        executor._resolve_image(profile)
    assert called is False


def test_docker_create_uses_non_overridable_hardening(isolated_paths: Path) -> None:
    request_id = "sandbox-create-01"
    source = executor._job_path(request_id) / "source"
    source.mkdir(parents=True)
    profile = executor._load_profiles()["python"]
    image_id = "sha256:" + ("a" * 64)
    image = executor.ResolvedImage("allowlisted", image_id, image_id, image_id)

    args = executor._build_docker_create_args(
        request_id=request_id,
        purpose="test",
        test_mode="combined",
        db_type="none",
        profile=profile,
        runtime="runsc",
        image=image,
        source_dir=source,
    )

    joined = " ".join(args)
    assert args[:2] == ["docker", "create"]
    assert "--runtime runsc" in joined
    assert "--network none" in joined
    assert "--read-only" in args
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges:true" in joined
    assert "--user 65532:65532" in joined
    assert "--pids-limit" in args
    assert "--memory-swap" in args
    assert "--cpus" in args
    assert "--ipc none" in joined
    assert "max-size=1m" in args and "max-file=2" in args
    assert f"/workspace:rw,exec,nosuid,nodev,size={profile.workspace_mb}m,mode=1777,uid=65532,gid=65532" in args
    assert "PRISM_TEST_MODE=combined" in args
    assert args[-1] == image_id
    assert "--publish" not in args and "-p" not in args
    assert "--privileged" not in args
    assert "--network host" not in joined


def test_browser_blackbox_uses_private_proxy_network_and_fixed_images(monkeypatch) -> None:
    image = executor.ResolvedImage("fixed", "sha256:" + "a" * 64, "sha256:" + "b" * 64, "sha256:" + "b" * 64)
    health = {
        "ready": True,
        "image_ref": "mcr.microsoft.com/playwright/mcp@sha256:fixed",
        "image_digest": "sha256:" + "a" * 64,
        "egress_policy_fingerprint": "c" * 64,
        "resource_policy": {"network": "private_browser_to_fixed_target_proxy"},
    }
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        if args[:3] == ["docker", "inspect", "--format"]:
            return {
                "exit_code": 0,
                "stdout": "172.28.0.2\n",
                "stderr": "",
                "output_bytes": 11,
                "output_truncated": False,
            }
        if args[:4] == ["docker", "start", "--attach", executor._browser_names("browser-request-01")[0]]:
            return {
                "exit_code": 0,
                "stdout": json.dumps(
                    {
                        "protocol_version": "1.0",
                        "passed": True,
                        "status_code": 200,
                        "screenshot_base64": "",
                    }
                )
                + "\n",
                "stderr": "",
                "output_bytes": 100,
                "output_truncated": False,
            }
        return {"exit_code": 0, "stdout": "", "stderr": "", "output_bytes": 0, "output_truncated": False}

    monkeypatch.setattr(executor, "_resolve_runtime", lambda: "runsc")
    monkeypatch.setattr(executor, "_browser_health", lambda *_args: health)
    monkeypatch.setattr(executor, "_resolve_image", lambda *_args: image)
    monkeypatch.setattr(executor, "_resolve_fixed_image", lambda *_args: image)
    monkeypatch.setattr(executor, "_run_command", fake_run)
    monkeypatch.setattr(executor.time, "sleep", lambda *_args: None)

    result = executor.run_browser_blackbox(
        {
            "request_id": "browser-request-01",
            "target_url": "https://example.com/a",
            "target_ip": "93.184.216.34",
        }
    )

    assert result["passed"] is True and result["resolved_ip"] == "93.184.216.34"
    flattened = [" ".join(call) for call in calls]
    assert any("network create --driver bridge --internal prism-bbx-private" in call for call in flattened)
    assert any("network connect --alias target-proxy" in call for call in flattened)
    browser_create = next(call for call in calls if call[:3] == ["docker", "create", "--name"] and "browser" in call[3])
    assert "--runtime" in browser_create and "runsc" in browser_create
    assert "--cap-drop" in browser_create and "ALL" in browser_create
    assert "--read-only" in browser_create and "--network" in browser_create
    assert "--dns" in browser_create and "127.0.0.1" in browser_create
    assert "--add-host" in browser_create and "target-proxy:172.28.0.2" in browser_create
    assert "1000:1000" in browser_create and "/home/node:rw" in " ".join(browser_create)
    assert "host" not in browser_create and "--privileged" not in browser_create


def test_browser_self_test_script_is_valid_javascript() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for JavaScript syntax validation")

    completed = subprocess.run(
        [node, "--check", "-"],
        input=executor._browser_self_test_script(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "target_url,target_ip",
    [
        ("http://example.com/", "93.184.216.34"),
        ("https://example.com/", "127.0.0.1"),
        ("https://example.com/#fragment", "93.184.216.34"),
    ],
)
def test_browser_blackbox_rejects_unapproved_protocol_or_non_public_ip(target_url: str, target_ip: str) -> None:
    with pytest.raises(ValueError):
        executor.run_browser_blackbox(
            {
                "request_id": "browser-request-02",
                "target_url": target_url,
                "target_ip": target_ip,
            }
        )


def test_zip_extraction_rejects_traversal_symlink_and_duplicate_case(
    isolated_paths: Path,
) -> None:
    with pytest.raises(ValueError, match="越界"):
        executor._extract_archive(_archive({"../escape.py": "bad"}), "sandbox-zip-01")

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        item = zipfile.ZipInfo("link")
        item.create_system = 3
        item.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(item, "/etc/passwd")
    with pytest.raises(ValueError, match="符号链接|特殊文件"):
        executor._extract_archive(output.getvalue(), "sandbox-zip-02")

    with pytest.raises(ValueError, match="重复|冲突"):
        executor._extract_archive(
            _archive({"App.py": "a", "app.py": "b"}),
            "sandbox-zip-03",
        )


def test_zip_extraction_preserves_relative_tree_as_read_only(
    isolated_paths: Path,
) -> None:
    source = executor._extract_archive(
        _archive({"src/main.py": "print('ok')\n", "README.md": "hello\n"}),
        "sandbox-zip-safe-01",
    )
    assert (source / "src" / "main.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert (source / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert (source / "src" / "main.py").stat().st_mode & 0o777 == 0o444
    assert source.stat().st_mode & 0o777 == 0o555
    executor._remove_job_data("sandbox-zip-safe-01")
    assert not executor._job_path("sandbox-zip-safe-01").exists()


def _submit_policy_blocked_archive(
    archive: bytes,
    request_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    image_id = "sha256:" + ("e" * 64)
    monkeypatch.setattr(executor, "_resolve_runtime", lambda: "runsc")
    monkeypatch.setattr(
        executor,
        "_resolve_image",
        lambda _profile, _digest="": executor.ResolvedImage("allowed", image_id, image_id, image_id),
    )
    monkeypatch.setattr(executor, "_remove_container", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        executor,
        "_run_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("归档策略违规后不得创建容器")),
    )

    result, duplicate = executor.submit_job(_payload(archive, request_id=request_id))

    assert duplicate is False
    assert result["status"] == "blocked"
    stored = executor._read_state(request_id)
    assert stored["status"] == "blocked"
    assert stored["stage"] == "blocked"
    assert stored["result"]["outcome"] == "blocked"
    return stored


def test_worker_marks_member_over_32_mib_as_blocked(
    isolated_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(executor, "MAX_MEMBER_BYTES", 32 * 1024 * 1024)
    monkeypatch.setattr(executor, "MAX_EXPANDED_BYTES", 64 * 1024 * 1024)
    archive = _archive({"oversized.bin": b"x" * ((32 * 1024 * 1024) + 1)})

    stored = _submit_policy_blocked_archive(archive, "sandbox-member-limit", monkeypatch)

    assert "单文件超过" in stored["error"]


def test_worker_marks_compression_ratio_over_200_as_blocked(
    isolated_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(executor, "MAX_MEMBER_BYTES", 32 * 1024 * 1024)
    monkeypatch.setattr(executor, "MAX_COMPRESSION_RATIO", 200)
    archive = _archive({"high-ratio.bin": b"0" * (2 * 1024 * 1024)})

    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        info = bundle.getinfo("high-ratio.bin")
        assert info.file_size / max(1, info.compress_size) > 200

    stored = _submit_policy_blocked_archive(archive, "sandbox-ratio-limit", monkeypatch)

    assert "压缩比超过" in stored["error"]


def test_submit_structured_events_and_idempotent_status(isolated_paths: Path, monkeypatch) -> None:
    archive = _archive({"main.py": "print('ok')\n"})
    image_id = "sha256:" + ("b" * 64)
    commands: list[list[str]] = []

    monkeypatch.setattr(executor, "_resolve_runtime", lambda: "runsc")
    monkeypatch.setattr(
        executor,
        "_resolve_image",
        lambda _profile, _digest="": executor.ResolvedImage("allowed", image_id, image_id, image_id),
    )
    monkeypatch.setattr(executor, "_start_monitor", lambda *_args: None)

    def fake_run(args, **_kwargs):
        commands.append(args)
        if args[:2] == ["docker", "create"]:
            return {"exit_code": 0, "stdout": "c" * 64, "stderr": "", "output_bytes": 64, "output_truncated": False}
        return {"exit_code": 0, "stdout": "", "stderr": "", "output_bytes": 0, "output_truncated": False}

    monkeypatch.setattr(executor, "_run_command", fake_run)
    first, duplicate = executor.submit_job(_payload(archive))
    second, duplicate_second = executor.submit_job(_payload(archive))

    assert duplicate is False
    assert duplicate_second is True
    assert first["status"] == "running_whitebox"
    assert second["request_id"] == first["request_id"]
    assert [event["sequence"] for event in first["events"]] == [1, 2, 3]
    assert [event["stage"] for event in first["events"]] == ["validating", "preparing", "running_whitebox"]
    assert any(args[:2] == ["docker", "create"] for args in commands)
    assert len([args for args in commands if args[:2] == ["docker", "create"]]) == 1
    assert executor.status_job({"request_id": "sandbox-request-01", "after_sequence": 2})["events"][0]["sequence"] == 3


def test_request_id_binds_source_digest(isolated_paths: Path, monkeypatch) -> None:
    first_archive = _archive({"main.py": "print(1)"})
    second_archive = _archive({"main.py": "print(2)"})
    profile = executor._load_profiles()["python"]
    first = executor._new_state(_payload(first_archive), profile, executor._request_digest(_payload(first_archive)))
    executor._write_state(first)

    with pytest.raises(executor.ConflictError, match="绑定"):
        executor.submit_job(_payload(second_archive))


def test_capacity_gate_rejects_before_archive_decode(isolated_paths: Path, monkeypatch) -> None:
    archive = _archive({"main.py": "print('active')\n"})
    profile = executor._load_profiles()["python"]
    active_payload = _payload(archive, request_id="sandbox-active-01")
    active = executor._new_state(active_payload, profile, executor._request_digest(active_payload))
    active["status"] = "running_whitebox"
    active["stage"] = "running_whitebox"
    executor._write_state(active)
    monkeypatch.setattr(executor, "MAX_CONCURRENCY", 1)

    decode_calls = 0

    def fail_decode(_payload):
        nonlocal decode_calls
        decode_calls += 1
        raise AssertionError("容量已满时不应解码源码压缩包")

    monkeypatch.setattr(executor, "_decode_archive", fail_decode)
    with pytest.raises(executor.CapacityError, match="最大并发"):
        executor.submit_job(_payload(archive, request_id="sandbox-capacity-01"))
    assert decode_calls == 0
    assert executor._pending_submission_count() == 0


def test_pending_gate_is_observable_and_preserves_idempotency(
    isolated_paths: Path,
    monkeypatch,
) -> None:
    archive = _archive({"main.py": "print('pending')\n"})
    image_id = "sha256:" + ("c" * 64)
    monkeypatch.setattr(executor, "MAX_CONCURRENCY", 1)
    monkeypatch.setattr(executor, "_resolve_runtime", lambda: "runsc")
    monkeypatch.setattr(
        executor,
        "_resolve_image",
        lambda _profile, _digest="": executor.ResolvedImage("allowed", image_id, image_id, image_id),
    )
    monkeypatch.setattr(executor, "_start_monitor", lambda *_args: None)

    original_decode = executor._decode_archive
    decode_started = threading.Event()
    release_decode = threading.Event()
    decode_calls: list[str] = []

    def blocking_decode(payload):
        decode_calls.append(payload["request_id"])
        decode_started.set()
        assert release_decode.wait(5), "测试未释放源码解码门禁"
        return original_decode(payload)

    monkeypatch.setattr(executor, "_decode_archive", blocking_decode)

    def fake_run(args, **_kwargs):
        if args[:2] == ["docker", "create"]:
            return {"exit_code": 0, "stdout": "d" * 64, "stderr": "", "output_bytes": 64, "output_truncated": False}
        return {"exit_code": 0, "stdout": "", "stderr": "", "output_bytes": 0, "output_truncated": False}

    monkeypatch.setattr(executor, "_run_command", fake_run)
    first_payload = _payload(archive, request_id="sandbox-pending-01")
    first_result: list[tuple[dict, bool]] = []
    first_errors: list[BaseException] = []

    def submit_first() -> None:
        try:
            first_result.append(executor.submit_job(first_payload))
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            first_errors.append(exc)

    first_thread = threading.Thread(target=submit_first)
    first_thread.start()
    assert decode_started.wait(2)
    assert executor._pending_submission_count() == 1
    assert executor.health()["pending_submissions"] == 1

    with pytest.raises(executor.CapacityError, match="最大并发"):
        executor.submit_job(_payload(archive, request_id="sandbox-pending-02"))
    assert decode_calls == ["sandbox-pending-01"]

    duplicate_result: list[tuple[dict, bool]] = []
    duplicate_errors: list[BaseException] = []
    duplicate_started = threading.Event()

    def submit_duplicate() -> None:
        duplicate_started.set()
        try:
            duplicate_result.append(executor.submit_job(first_payload))
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            duplicate_errors.append(exc)

    duplicate_thread = threading.Thread(target=submit_duplicate)
    duplicate_thread.start()
    assert duplicate_started.wait(1)
    for _ in range(100):
        if duplicate_thread.is_alive() and not duplicate_result and not duplicate_errors:
            break
        time.sleep(0.005)
    assert duplicate_thread.is_alive()

    release_decode.set()
    first_thread.join(timeout=5)
    duplicate_thread.join(timeout=5)
    assert not first_thread.is_alive() and not duplicate_thread.is_alive()
    assert first_errors == [] and duplicate_errors == []
    assert sorted(item[1] for item in first_result + duplicate_result) == [False, True]
    assert decode_calls == ["sandbox-pending-01"]
    assert executor._pending_submission_count() == 0


def test_extend_only_active_deployment_and_never_exceeds_absolute_ttl(
    isolated_paths: Path,
) -> None:
    archive = _archive({"app.py": "print('app')"})
    profile = executor._load_profiles()["python"]
    payload = _payload(archive, purpose="deploy", ttl_seconds=3600)
    state = executor._new_state(payload, profile, executor._request_digest(payload))
    state["status"] = "running"
    state["stage"] = "running"
    executor._write_state(state)

    result = executor.extend_job({"request_id": "sandbox-request-01", "extend_seconds": 60})
    assert executor._parse_iso(result["expires_at"]) > executor._parse_iso(state["expires_at"])

    stored = executor._read_state("sandbox-request-01")
    stored["expires_at"] = executor._iso(
        executor._parse_iso(stored["created_at"]) + timedelta(seconds=executor.MAX_TTL_SECONDS)
    )
    executor._write_state(stored)
    with pytest.raises(executor.ConflictError, match="最大保留时间"):
        executor.extend_job({"request_id": "sandbox-request-01", "extend_seconds": 60})


def test_preview_target_is_fixed_loopback_and_rejects_traversal() -> None:
    assert executor._normalize_preview_target("/api/items", "page=1") == ("http://127.0.0.1:8080/api/items?page=1")
    assert executor._normalize_preview_target("/search", "q=hello world") == (
        "http://127.0.0.1:8080/search?q=hello%20world"
    )
    for unsafe in (
        "/../secret",
        "/%2e%2e/secret",
        "/%252e%252e/secret",
        "//169.254.169.254/latest",
        "/bad\\path",
    ):
        with pytest.raises(ValueError):
            executor._normalize_preview_target(unsafe, "")
    with pytest.raises(ValueError, match="百分号"):
        executor._normalize_preview_target("/search", "q=%zz")


@pytest.mark.parametrize(
    ("status_code", "exit_code", "expected"),
    (("200", 0, True), ("302", 0, True), ("404", 0, False), ("500", 0, False), ("000", 7, False)),
)
def test_deployment_probe_rejects_http_error_statuses(
    monkeypatch,
    status_code: str,
    exit_code: int,
    expected: bool,
) -> None:
    monkeypatch.setattr(
        executor,
        "_run_command",
        lambda *_args, **_kwargs: {
            "exit_code": exit_code,
            "stdout": status_code,
            "stderr": "",
            "output_bytes": len(status_code),
            "output_truncated": False,
        },
    )
    assert executor._probe_container("prism-sandbox-probe") is expected


def test_cleanup_failure_remains_active_until_janitor_reclaims(
    isolated_paths: Path,
    monkeypatch,
) -> None:
    archive = _archive({"main.py": "print('ok')\n"})
    profile = executor._load_profiles()["python"]
    payload = _payload(archive)
    state = executor._new_state(payload, profile, executor._request_digest(payload))
    state["status"] = "running_whitebox"
    state["stage"] = "running_whitebox"
    executor._write_state(state)

    attempts = 0

    def remove_container(_container: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("container still exists")

    monkeypatch.setattr(executor, "_remove_container", remove_container)
    monkeypatch.setattr(executor, "_remove_job_data", lambda _request_id: None)

    completed = executor._queue_terminal_cleanup(
        "sandbox-request-01",
        status_value="succeeded",
        stage="succeeded",
        message="白盒测试完成",
        error="",
        result={"outcome": "succeeded", "exit_code": 0, "timed_out": False},
    )
    pending = executor._read_state("sandbox-request-01")
    assert completed is False
    assert pending["status"] == "stopping"
    assert pending["pending_terminal"]["status"] == "succeeded"
    assert "cleanup_error" in pending["result"]
    assert executor._active_job_count() == 1

    executor._recover_jobs()
    finished = executor._read_state("sandbox-request-01")
    assert attempts == 2
    assert finished["status"] == "succeeded"
    assert "pending_terminal" not in finished
    assert "cleanup_error" not in finished["result"]
    assert executor._active_job_count() == 0


def test_evidence_artifacts_are_structured_escaped_and_integrity_checked(monkeypatch) -> None:
    environment = SimpleNamespace(
        id=101,
        public_id="sbx_artifact_01",
        source_sha256="a" * 64,
        runtime="runsc",
        agent_code="test_verifier",
    )
    conclusion = {
        "passed": False,
        "summary": "failed <script>alert(1)</script>",
        "evidence": {
            "worker_result": {
                "exit_code": 1,
                "logs": {"text": "trace <img src=x onerror=alert(1)>"},
            },
        },
    }
    documents = sandbox_service._artifact_documents(environment, conclusion)
    by_type = {artifact_type: content for artifact_type, _name, _mime, content in documents}

    assert set(by_type) == {"result", "log", "junit", "sarif", "html"}
    ET.fromstring(by_type["junit"])
    assert json.loads(by_type["sarif"])["version"] == "2.1.0"
    html_report = by_type["html"].decode("utf-8")
    assert "<script>alert(1)</script>" not in html_report
    assert "<img src=x onerror=alert(1)>" not in html_report
    assert "&lt;script&gt;" in html_report and "&lt;img" in html_report

    content = by_type["result"]
    row = SimpleNamespace(
        id=7,
        environment_id=environment.id,
        artifact_type="result",
        content_base64=base64.b64encode(content).decode("ascii"),
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        file_name="sandbox-result.json",
        mime_type="application/json",
    )
    query = MagicMock()
    query.filter.return_value.first.return_value = row
    db = MagicMock()
    db.query.return_value = query
    monkeypatch.setattr(sandbox_service, "_get_visible", lambda *_args: environment)

    downloaded, file_name, mime_type = sandbox_service.get_artifact_download(
        db,
        SimpleNamespace(id=17),
        environment.public_id,
        row.id,
    )
    assert downloaded == content
    assert file_name == "sandbox-result.json" and mime_type == "application/json"

    row.sha256 = "0" * 64
    with pytest.raises(RuntimeError, match="完整性"):
        sandbox_service.get_artifact_download(
            db,
            SimpleNamespace(id=17),
            environment.public_id,
            row.id,
        )


def test_preview_proxy_uses_exact_container_and_never_accepts_upstream(
    isolated_paths: Path,
    monkeypatch,
) -> None:
    archive = _archive({"app.py": "print('app')"})
    profile = executor._load_profiles()["python"]
    payload = _payload(archive, purpose="deploy")
    state = executor._new_state(payload, profile, executor._request_digest(payload))
    state.update(
        {
            "status": "running",
            "stage": "running",
            "container_name": executor._container_name("sandbox-request-01"),
            "container_id": "d" * 64,
        }
    )
    executor._write_state(state)
    observed: list[str] = []

    class Completed:
        returncode = 0
        stdout = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: keep-alive\r\n\r\nhello"
        stderr = b""

    def fake_run(args, **_kwargs):
        observed.extend(args)
        return Completed()

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    code, headers, body = executor._proxy_preview(
        "sandbox-request-01", "/hello", "", "GET", {"Accept": "text/plain"}, b""
    )
    assert code == 200 and body == b"hello"
    assert headers["Content-Type"] == "text/plain"
    assert "Connection" not in headers
    assert observed[:4] == ["docker", "exec", "-i", executor._container_name("sandbox-request-01")]
    assert observed[4:7] == ["/opt/prism/runner.sh", "proxy", "GET"]
    assert observed[-1] == "http://127.0.0.1:8080/hello"
    assert "--publish" not in observed


def test_preview_proxy_decodes_chunked_response(
    isolated_paths: Path,
    monkeypatch,
) -> None:
    archive = _archive({"app.py": "print('app')"})
    profile = executor._load_profiles()["python"]
    payload = _payload(archive, purpose="deploy")
    state = executor._new_state(payload, profile, executor._request_digest(payload))
    state.update(
        {
            "status": "running",
            "stage": "running",
            "container_name": executor._container_name("sandbox-request-01"),
            "container_id": "d" * 64,
        }
    )
    executor._write_state(state)

    class Completed:
        returncode = 0
        stdout = (
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n"
        )
        stderr = b""

    monkeypatch.setattr(executor.subprocess, "run", lambda *_args, **_kwargs: Completed())
    code, headers, body = executor._proxy_preview("sandbox-request-01", "/hello", "", "GET", {}, b"")
    assert code == 200 and body == b"hello"
    assert headers["Content-Length"] == "5"


def test_service_and_images_encode_required_hardening() -> None:
    root = MODULE_PATH.parent
    service = (root / "prism-sandbox-executor.service").read_text(encoding="utf-8")
    for directive in (
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "PrivateDevices=true",
        "RestrictAddressFamilies=AF_UNIX",
        "CapabilityBoundingSet=",
        "StateDirectoryMode=0770",
        "Environment=SANDBOX_EXECUTOR_SOCKET=/var/lib/prism-sandbox/agent.sock",
    ):
        assert directive in service
    assert "ExecStartPost=" not in service
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert "/var/lib/prism-sandbox:/var/lib/prism-sandbox:ro" in compose
    assert "SANDBOX_EXECUTOR_SOCKET: /var/lib/prism-sandbox/agent.sock" in compose
    executor_source = (root / "prism_sandbox_executor.py").read_text(encoding="utf-8")
    assert "_ensure_directory(SOCKET_PATH.parent, 0o750)" in executor_source
    assert "_ensure_directory(SOCKET_PATH.parent, 0o770)" not in executor_source
    assert '"/tmp:rw,noexec,nosuid,nodev' in executor_source
    assert 'f"/workspace:rw,exec,nosuid,nodev' in executor_source
    runner = (root / "sandbox" / "runner.sh").read_text(encoding="utf-8")
    assert "[1-5][0-9][0-9]" in runner  # 黑盒探活接受任何合法 HTTP 状态(5xx 属应用自身错误非服务失败)
    assert "grep -E 'Fatal error|Parse error|Errors parsing'" in runner
    assert "whitebox: php lint reported warnings" in runner
    assert 'if [ "${1:-}" = "proxy" ]' in runner
    assert 'exec 3<>"/dev/tcp/127.0.0.1/$port"' in runner
    for language in ("python", "node", "java", "go", "php"):
        dockerfile = (root / "sandbox" / f"Dockerfile.{language}").read_text(encoding="utf-8")
        assert 'ENTRYPOINT ["/opt/prism/runner.sh"]' in dockerfile
        assert "USER " not in dockerfile  # runtime UID is fixed by docker create, not image metadata


class _DormantThread:
    """Capture asynchronous dispatch without starting the service worker."""

    created: list["_DormantThread"] = []

    def __init__(self, *, target, args, name: str, daemon: bool) -> None:
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.started = False
        self.created.append(self)

    def start(self) -> None:
        self.started = True


def _grant_sandbox_globals(db, actor: User) -> None:
    """正向场景使用真实普通身份及明确权限，不替换产品授权判定。"""
    role = db.query(Role).filter_by(code=actor.role).one_or_none()
    if role is None:
        role = Role(code=actor.role, name="本地沙箱授权夹具", status="active")
        db.add(role)
        db.flush()
    if db.query(UserRole).filter_by(user_id=actor.id, role_id=role.id).first() is None:
        db.add(UserRole(user_id=actor.id, role_id=role.id))
    for code in (PermissionCode.PROJECT_VIEW, PermissionCode.FILE_VIEW):
        permission = db.query(Permission).filter_by(code=code).one_or_none()
        if permission is None:
            permission = Permission(code=code, name=code, module="local", type="api")
            db.add(permission)
            db.flush()
        if db.query(RolePermission).filter_by(role_id=role.id, permission_id=permission.id).first() is None:
            db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db.commit()


def _persist_execution_owner(db) -> User:
    actor = User(id=17, username="executor-local-owner", password="local", role="user", status=1)
    db.add(actor)
    db.flush()
    db.add(Project(id=91, user_id=actor.id, project_name="executor-local", language="python", status="active"))
    _grant_sandbox_globals(db, actor)
    return actor


def _persist_execution_worker(db) -> SandboxWorker:
    worker = SandboxWorker(
        id=8,
        code="gvisor-01",
        name="本地桩执行器",
        worker_type="local",
        transport="unix",
        endpoint="/run/prism-sandbox/never-used.sock",
        supported_languages_json='["python"]',
        supported_modes_json='["whitebox","deploy"]',
        runtime="runsc",
        status="healthy",
        enabled=1,
    )
    db.add(worker)
    db.commit()
    return worker


def _patch_environment_creation(db, monkeypatch, archive: bytes) -> User:
    actor = _persist_execution_owner(db)
    _DormantThread.created.clear()
    monkeypatch.setattr(
        sandbox_service.project_source_service,
        "build_source_archive",
        lambda *_args, **_kwargs: (archive, "source.zip"),
    )
    monkeypatch.setattr(
        sandbox_service,
        "pin_public_http_url",
        lambda url, **_kwargs: SimpleNamespace(original_url=url),
    )
    monkeypatch.setattr(sandbox_service, "_append_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sandbox_service, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sandbox_service.audit_service, "log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sandbox_service.threading, "Thread", _DormantThread)
    return actor


def test_create_environment_maps_decompilation_input_error_to_validation(db, monkeypatch) -> None:
    archive = _archive({"main.py": "print('invalid archive')\n"})
    actor = _patch_environment_creation(db, monkeypatch, archive)
    monkeypatch.setattr(
        sandbox_service.decompilation_service,
        "plan_decompilation_archive",
        MagicMock(
            side_effect=sandbox_service.decompilation_service.DecompilationError("源码归档无法读取反编译候选成员")
        ),
    )

    with pytest.raises(sandbox_service.ValidationError) as exc_info:
        sandbox_service.create_environment(
            db,
            actor,
            {
                "project_id": 91,
                "purpose": "test",
                "language": "python",
                "test_mode": "whitebox",
            },
        )

    assert exc_info.value.code == 40001
    assert "源码归档无法读取" in exc_info.value.message


def test_remote_only_blackbox_never_selects_or_dispatches_a_worker(db, monkeypatch) -> None:
    archive = _archive({"main.py": "print('remote')\n"})
    actor = _patch_environment_creation(db, monkeypatch, archive)

    def fail_select(*_args, **_kwargs):
        raise AssertionError("remote-only blackbox must not select a worker")

    monkeypatch.setattr(sandbox_service, "_select_worker", fail_select)
    environment = sandbox_service.create_environment(
        db,
        actor,
        {
            "project_id": 91,
            "purpose": "test",
            "language": "python",
            "test_mode": "blackbox",
            "remote_target_url": "https://example.test",
            "remote_target_authorized": True,
        },
    )

    config = json.loads(environment.agent_config_json)
    assert environment.worker_id is None
    assert environment.runtime == "remote_http"
    assert config["remote_only"] is True
    assert config["worker_mode"] == "blackbox"
    assert len(_DormantThread.created) == 1
    assert _DormantThread.created[0].started is True


def test_remote_combined_selects_whitebox_worker(db, monkeypatch) -> None:
    archive = _archive({"main.py": "print('combined')\n"})
    actor = _patch_environment_creation(db, monkeypatch, archive)
    worker = _persist_execution_worker(db)
    selections: list[dict[str, object]] = []

    def select_worker(_db, **kwargs):
        selections.append(kwargs)
        return worker

    monkeypatch.setattr(sandbox_service, "_select_worker", select_worker)
    environment = sandbox_service.create_environment(
        db,
        actor,
        {
            "project_id": 91,
            "purpose": "test",
            "language": "python",
            "test_mode": "combined",
            "remote_target_url": "https://example.test",
            "remote_target_authorized": True,
        },
    )

    config = json.loads(environment.agent_config_json)
    assert selections == [{"language": "python", "mode": "whitebox", "worker_code": ""}]
    assert environment.worker_id == worker.id
    assert config["remote_only"] is False
    assert config["worker_mode"] == "whitebox"


def test_deployment_uses_project_language_instead_of_stale_requested_profile(db, monkeypatch) -> None:
    owner = User(username="deploy_language_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    _grant_sandbox_globals(db, owner)
    project = Project(
        user_id=owner.id,
        project_name="php-deployment",
        language="PHP 8.3",
        status="active",
    )
    worker = SandboxWorker(
        code="deploy-language-worker",
        name="Deploy language worker",
        worker_type="local",
        transport="unix",
        endpoint="/run/prism-sandbox/executor.sock",
        supported_languages_json='["php"]',
        supported_modes_json='["deploy"]',
        runtime="runsc",
        max_concurrency=1,
        priority=10,
        status="healthy",
        enabled=1,
    )
    db.add_all([project, worker])
    db.commit()
    _DormantThread.created.clear()
    monkeypatch.setattr(
        sandbox_service.project_source_service,
        "build_source_archive",
        lambda *_args, **_kwargs: (_archive({"index.php": "<?php echo 'ready';\n"}), "source.zip"),
    )
    monkeypatch.setattr(sandbox_service.threading, "Thread", _DormantThread)
    monkeypatch.setattr(sandbox_service, "_emit", lambda *_args, **_kwargs: None)

    environment = sandbox_service.create_environment(
        db,
        owner,
        {
            "project_id": project.id,
            "purpose": "deploy",
            "language": "python",
            "test_mode": "deploy",
            "ttl_hours": 1,
        },
    )

    config = json.loads(environment.agent_config_json)
    events = db.query(SandboxEvent).filter(SandboxEvent.environment_id == environment.id).all()
    assert environment.language == "php"
    assert environment.image_ref == "prism-sandbox-php:8.3"
    assert environment.worker_id == worker.id
    assert config["requested_language"] == "python"
    assert config["resolved_language"] == "php"
    assert config["language_source"] == "project"
    assert any(event.stage == "language" and "python" in event.message and "php" in event.message for event in events)
    assert len(_DormantThread.created) == 1
    assert _DormantThread.created[0].started is True


def test_deployment_rejects_project_language_without_controlled_runtime(db) -> None:
    owner = User(username="deploy_unknown_language_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    _grant_sandbox_globals(db, owner)
    project = Project(
        user_id=owner.id,
        project_name="unknown-language-deployment",
        language="plaintext",
        status="active",
    )
    db.add(project)
    db.commit()

    with pytest.raises(sandbox_service.ValidationError) as exc_info:
        sandbox_service.create_environment(
            db,
            owner,
            {
                "project_id": project.id,
                "purpose": "deploy",
                "language": "python",
                "test_mode": "deploy",
                "ttl_hours": 1,
            },
        )

    assert exc_info.value.code == 40001
    assert "项目主语言" in exc_info.value.message


def test_fast_terminal_execute_response_persists_worker_events_before_conclusion(db, monkeypatch) -> None:
    _persist_execution_owner(db)
    _persist_execution_worker(db)
    environment = SandboxEnvironment(
        id=101,
        public_id="sbx_fast_terminal",
        project_id=91,
        owner_id=17,
        worker_id=8,
        agent_code="test_verifier",
        purpose="test",
        language="python",
        test_mode="whitebox",
        status="queued",
        runtime="runsc",
        image_ref="prism-sandbox-python:test",
        image_digest=None,
        source_sha256=hashlib.sha256(b"source").hexdigest(),
        execution_token="fast-terminal-lease",
        resource_policy_json=sandbox_service._json({"network": "none"}),
        agent_config_json=sandbox_service._json({"worker_mode": "whitebox", "remote_only": False}),
        remote_target_url=None,
        expires_at=datetime.utcnow() + timedelta(hours=1),
        executor_ref=None,
        started_at=None,
        stopped_at=None,
        preview_path=None,
        result_json=None,
        error=None,
    )
    db.add(environment)
    db.commit()
    # 包装真实工作事务只统计收尾，不改变 ORM 或授权查询。
    worker_db = MagicMock(wraps=sessionmaker(bind=db.get_bind(), expire_on_commit=False)())
    worker_calls: list[str] = []
    stored_events: list[tuple[str, str, str, dict[str, object]]] = []

    def call_worker(_worker, _method: str, path: str, payload):
        worker_calls.append(path)
        assert path == "/execute"
        assert payload["source_sha256"] == hashlib.sha256(b"source").hexdigest()
        return {
            "result": {
                "request_id": environment.public_id,
                "source_sha256": hashlib.sha256(b"source").hexdigest(),
                "status": "succeeded",
                "last_sequence": 2,
                "events": [
                    {
                        "event_type": "progress",
                        "stage": "running_whitebox",
                        "message": "白盒测试完成",
                        "payload": {"sequence": 1},
                    },
                    {
                        "event_type": "result",
                        "stage": "succeeded",
                        "message": "执行成功",
                        "payload": {"exit_code": 0},
                    },
                ],
                "result": {"exit_code": 0, "summary": "ok"},
            }
        }

    def append_event(_db, _environment, event_type, stage, message, payload=None):
        stored_events.append((event_type, stage, message, payload or {}))
        return SimpleNamespace(id=len(stored_events))

    monkeypatch.setattr(sandbox_service, "SessionLocal", lambda: worker_db)
    monkeypatch.setattr(sandbox_service, "_call_worker", call_worker)
    monkeypatch.setattr(sandbox_service, "_append_event", append_event)
    monkeypatch.setattr(sandbox_service, "_emit", lambda *_args, **_kwargs: None)
    # 本用例聚焦 worker 事件顺序;跳过 agent 动态用例生成与部署补丁(另有专门测试)
    monkeypatch.setattr(sandbox_service, "_generate_agent_test_cases", lambda *_a, **_k: None)
    monkeypatch.setattr(sandbox_service, "_generate_deployment_patch", lambda *_a, **_k: None)
    monkeypatch.setattr(sandbox_service, "_run_test_review_report", lambda *_a, **_k: None)
    monkeypatch.setattr(sandbox_service.strategy_learning_service, "observe_sandbox_outcome", lambda *_a, **_k: None)

    sandbox_service._execute_environment(environment.id, "c291cmNl")

    db.refresh(environment)
    stages = [event[1] for event in stored_events]
    assert worker_calls == ["/execute"]
    # 本用例显式跳过多 Agent 审查,验证 worker 事件先于最终结论持久化。
    assert stages == ["executor", "running_whitebox", "succeeded", "conclusion"], environment.error
    assert stages.index("succeeded") < stages.index("conclusion")
    assert environment.status == "succeeded"
    result = json.loads(environment.result_json)
    assert result["passed"] is True
    assert result["summary"] == "确定性白盒测试通过；AI动态补充未执行"
    assert result["evidence"]["agent_test_generation"]["status"] == "skipped"
    assert result["evidence"]["deterministic_test_execution"] == {
        "requested_mode": "whitebox",
        "worker_mode": "whitebox",
        "status": "passed",
        "exit_code": 0,
    }
    assert result["source_provenance"]["worker_receipt"] == {
        "request_id": environment.public_id,
        "source_sha256": hashlib.sha256(b"source").hexdigest(),
    }
    assert worker_db.rollback.call_count == 0
    assert worker_db.close.call_count == 1


def test_preview_session_token_scope_version_and_expiry(db, monkeypatch) -> None:
    actor = _persist_execution_owner(db)
    actor.token_version = 6
    worker = _persist_execution_worker(db)
    environment = SandboxEnvironment(
        public_id="sbx_preview_a",
        project_id=91,
        owner_id=actor.id,
        agent_code="sandbox_deployer",
        purpose="deploy",
        language="python",
        test_mode="deploy",
        status="ready",
        runtime="runsc",
        image_ref="local-unused",
        source_sha256="0" * 64,
        resource_policy_json="{}",
        agent_config_json="{}",
        expires_at=datetime.utcnow() + timedelta(minutes=10),
        worker_id=worker.id,
        preview_path=None,
    )
    db.add(environment)
    db.commit()
    assert db.get(User, actor.id) is actor
    assert db.get(SandboxWorker, worker.id) is worker
    monkeypatch.setattr(sandbox_service.audit_service, "log", lambda *_args, **_kwargs: None)

    session = sandbox_service.create_preview_session(db, actor, environment.public_id)
    claims = sandbox_service.jwt.decode(
        session["token"],
        sandbox_service.settings.jwt_secret,
        algorithms=[sandbox_service.settings.jwt_algorithm],
    )
    assert claims["sub"] == str(actor.id)
    assert claims["ver"] == actor.token_version
    assert claims["typ"] == "sandbox_preview"
    assert claims["sbx"] == environment.public_id
    assert session["path"] == f"/api/sandboxes/{environment.public_id}/preview/"
    assert sandbox_service.authenticate_preview_session(db, environment.public_id, session["token"]) == (
        environment,
        worker,
    )

    now = datetime.now(timezone.utc)

    def encode(**overrides):
        payload = {
            "sub": str(actor.id),
            "ver": actor.token_version,
            "typ": "sandbox_preview",
            "sbx": environment.public_id,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=1)).timestamp()),
        }
        payload.update(overrides)
        return sandbox_service.jwt.encode(
            payload,
            sandbox_service.settings.jwt_secret,
            algorithm=sandbox_service.settings.jwt_algorithm,
        )

    with pytest.raises(sandbox_service.AuthError) as scope_error:
        sandbox_service.authenticate_preview_session(db, environment.public_id, encode(sbx="sbx_preview_b"))
    assert scope_error.value.code == 40101

    with pytest.raises(sandbox_service.AuthError) as version_error:
        sandbox_service.authenticate_preview_session(db, environment.public_id, encode(ver=actor.token_version - 1))
    assert version_error.value.code == 40102

    with pytest.raises(sandbox_service.AuthError) as expiry_error:
        sandbox_service.authenticate_preview_session(
            db,
            environment.public_id,
            encode(
                iat=int((now - timedelta(minutes=2)).timestamp()),
                exp=int((now - timedelta(seconds=1)).timestamp()),
            ),
        )
    assert expiry_error.value.code == 40101


@pytest.mark.parametrize("revocation", ["viewer", "global_permission"])
@pytest.mark.parametrize("entry", ["create", "preview", "existing_preview"])
def test_executor_entry_rechecks_committed_execution_revocation(
    independent_transaction_db,
    monkeypatch,
    revocation: str,
    entry: str,
) -> None:
    """真实已授权会话撤权后，创建与旧预览令牌均不得恢复执行能力。"""
    db = independent_transaction_db
    _persist_execution_owner(db)
    worker = _persist_execution_worker(db)
    actor = User(id=18, username="executor-local-member", password="local", role="user", status=1)
    db.add(actor)
    db.flush()
    _grant_sandbox_globals(db, actor)
    member = ProjectMember(project_id=91, user_id=actor.id, role_in_project="reviewer")
    environment = SandboxEnvironment(
        public_id="sbx_revoked_preview",
        project_id=91,
        owner_id=actor.id,
        worker_id=worker.id,
        agent_code="sandbox_deployer",
        purpose="deploy",
        language="python",
        test_mode="deploy",
        status="ready",
        runtime="runsc",
        image_ref="local-unused",
        source_sha256="0" * 64,
        execution_token="local-revoked-lease",
        resource_policy_json="{}",
        agent_config_json="{}",
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    db.add_all([member, environment])
    db.commit()
    monkeypatch.setattr(sandbox_service.audit_service, "log", lambda *_args, **_kwargs: None)
    session = sandbox_service.create_preview_session(db, actor, environment.public_id)
    # 用另一个真实事务提交变更；原事务仍持有旧身份映射。
    with sessionmaker(bind=db.get_bind(), expire_on_commit=False)() as auth_db:
        assert db.connection().connection.driver_connection is not auth_db.connection().connection.driver_connection
        if revocation == "viewer":
            auth_db.get(ProjectMember, member.id).role_in_project = "viewer"
        else:
            permission = auth_db.query(Permission).filter_by(code=PermissionCode.FILE_VIEW).one()
            auth_db.query(RolePermission).filter_by(permission_id=permission.id).delete()
        auth_db.commit()
    assert member.role_in_project == "reviewer"
    external_call = MagicMock(side_effect=AssertionError("拒绝后不得下载、运行或调度"))
    monkeypatch.setattr(sandbox_service.project_source_service, "build_source_archive", external_call)
    monkeypatch.setattr(sandbox_service, "_call_worker", external_call)
    monkeypatch.setattr(sandbox_service.threading, "Thread", external_call)
    with pytest.raises(sandbox_service.ForbiddenError) as error:
        if entry == "create":
            sandbox_service.create_environment(
                db,
                actor,
                {"project_id": 91, "purpose": "test", "language": "python", "test_mode": "whitebox"},
            )
        elif entry == "preview":
            sandbox_service.create_preview_session(db, actor, environment.public_id)
        else:
            sandbox_service.authenticate_preview_session(db, environment.public_id, session["token"])
    assert error.value.code == (40300 if revocation == "viewer" else 40303)
    external_call.assert_not_called()
    assert db.query(SandboxEnvironment).count() == 1


def _persist_source_archive(db, project: Project, owner: User) -> ProjectSourceArchive:
    raw = _archive({"src/index.php": "<?php echo 'audit';\n"})
    row = ProjectSourceArchive(
        project_id=project.id,
        owner_id=owner.id,
        original_filename="source.zip",
        archive_sha256=hashlib.sha256(raw).hexdigest(),
        compressed_size=len(raw),
        expanded_size=20,
        file_count=1,
        max_member_size=20,
        max_compression_ratio=1.0,
        storage_status="active",
        malware_status="clean",
        audit_status="not_started",
        threat_count=0,
        scan_summary_json="{}",
        archive_blob=raw,
    )
    db.add(row)
    db.commit()
    return row


def test_project_reviewer_cannot_create_deployment_environment(db) -> None:
    owner = User(username="sandbox_owner", password="x", role="user", status=1)
    reviewer = User(username="sandbox_reviewer", password="x", role="user", status=1)
    db.add_all([owner, reviewer])
    db.flush()
    project = Project(user_id=owner.id, project_name="review-only", status="active")
    db.add(project)
    db.flush()
    db.add(ProjectMember(project_id=project.id, user_id=reviewer.id, role_in_project="reviewer"))
    db.commit()

    with pytest.raises(sandbox_service.ForbiddenError) as exc_info:
        sandbox_service.create_environment(
            db,
            reviewer,
            {
                "project_id": project.id,
                "purpose": "deploy",
                "language": "python",
                "test_mode": "whitebox",
            },
        )

    assert exc_info.value.code == 40300
    assert db.query(SandboxEnvironment).count() == 0


def test_quarantined_project_allowed_deploy_in_sandbox(db, monkeypatch) -> None:
    """隔离归档项目允许在沙箱内部署:生成不可变快照并创建环境。

    恶意/隔离源码只允许运行在 runsc 沙箱内,绝不裸跑宿主机;预览经受
    JWT 保护的 backend 代理访问。此处不再抛 40341。
    """
    owner = User(username="isolated_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    _grant_sandbox_globals(db, owner)
    project = Project(user_id=owner.id, project_name="isolated-deploy", status="active", language="php")
    db.add(project)
    db.commit()
    _persist_source_archive(db, project, owner)
    real_build = sandbox_service.project_source_service.build_source_archive
    captured = {}

    def _build(db, user, project_id):
        data = real_build(db, user, project_id)
        captured["archive"] = data
        return data

    monkeypatch.setattr(sandbox_service.project_source_service, "build_source_archive", _build)
    monkeypatch.setattr(sandbox_service.threading, "Thread", _DormantThread)
    monkeypatch.setattr(sandbox_service, "_emit", lambda *_args, **_kwargs: None)
    worker = SandboxWorker(
        code="php-deploy",
        name="PHP deploy worker",
        worker_type="local",
        transport="unix",
        endpoint="/run/prism-sandbox/executor.sock",
        supported_languages_json='["php"]',
        supported_modes_json='["deploy"]',
        runtime="runsc",
        max_concurrency=1,
        priority=1,
        status="healthy",
        enabled=1,
    )
    db.add(worker)
    db.commit()

    environment = sandbox_service.create_environment(
        db,
        owner,
        {
            "project_id": project.id,
            "purpose": "deploy",
            "language": "php",
            "test_mode": "deploy",
        },
    )
    assert captured["archive"] is not None
    assert environment.purpose == "deploy"
    assert environment.project_id == project.id


def test_deploy_creation_locks_project_even_with_archive(db, monkeypatch) -> None:
    """隔离归档项目部署仍先对项目加行锁(并发下不混入源码/不重复建环境)。"""
    owner = User(username="lock_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    _grant_sandbox_globals(db, owner)
    project = Project(user_id=owner.id, project_name="locked", status="active", language="php")
    db.add(project)
    db.commit()
    _persist_source_archive(db, project, owner)
    worker = SandboxWorker(
        code="php-lock",
        name="PHP lock worker",
        worker_type="local",
        transport="unix",
        endpoint="/run/prism-sandbox/executor.sock",
        supported_languages_json='["php"]',
        supported_modes_json='["deploy"]',
        runtime="runsc",
        max_concurrency=1,
        priority=1,
        status="healthy",
        enabled=1,
    )
    db.add(worker)
    db.commit()
    real_build = sandbox_service.project_source_service.build_source_archive
    built = {}

    def _build(db_, user_, project_id_):
        data = real_build(db_, user_, project_id_)
        built["ok"] = True
        return data

    monkeypatch.setattr(sandbox_service.project_source_service, "build_source_archive", _build)
    monkeypatch.setattr(sandbox_service.threading, "Thread", _DormantThread)
    monkeypatch.setattr(sandbox_service, "_emit", lambda *_args, **_kwargs: None)

    # 隔离归档项目允许部署且生成不可变快照(内部对项目加了行锁)。
    sandbox_service.create_environment(
        db,
        owner,
        {
            "project_id": project.id,
            "purpose": "deploy",
            "language": "php",
            "test_mode": "deploy",
        },
    )
    assert built.get("ok") is True


def test_quarantine_allows_new_and_existing_preview_sessions(db, monkeypatch) -> None:
    """隔离归档项目允许创建与认证预览会话(经受 JWT 保护的沙箱代理访问)。"""
    owner = User(username="preview_owner", password="x", role="user", status=1, token_version=4)
    db.add(owner)
    db.flush()
    _grant_sandbox_globals(db, owner)
    project = Project(user_id=owner.id, project_name="preview-isolation", status="active")
    worker = SandboxWorker(
        code="preview-worker",
        name="Preview worker",
        worker_type="local",
        transport="unix",
        endpoint="/run/prism-sandbox/executor.sock",
        supported_languages_json='["php"]',
        supported_modes_json='["deploy"]',
        runtime="runsc",
        max_concurrency=1,
        priority=1,
        status="healthy",
        enabled=1,
    )
    db.add_all([project, worker])
    db.flush()
    environment = SandboxEnvironment(
        public_id="sbx_preview_quarantine_01",
        project_id=project.id,
        owner_id=owner.id,
        worker_id=worker.id,
        agent_code="sandbox_deployer",
        purpose="deploy",
        language="php",
        test_mode="deploy",
        status="ready",
        runtime="runsc",
        image_ref="prism-sandbox-php:8.3",
        source_sha256="f" * 64,
        resource_policy_json="{}",
        agent_config_json="{}",
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    db.add(environment)
    db.commit()
    monkeypatch.setattr(sandbox_service.audit_service, "log", lambda *_args, **_kwargs: None)

    existing_session = sandbox_service.create_preview_session(db, owner, environment.public_id)
    _persist_source_archive(db, project, owner)

    # 隔离归档不阻断预览:新建会话与既有会话认证都应放行。
    new_session = sandbox_service.create_preview_session(db, owner, environment.public_id)
    assert "token" in new_session

    authed_environment, authed_worker = sandbox_service.authenticate_preview_session(
        db,
        environment.public_id,
        existing_session["token"],
    )
    assert authed_environment.public_id == environment.public_id
    assert authed_worker.id == worker.id


def test_sandbox_terminal_transition_requires_current_execution_token(db) -> None:
    owner = User(username="sandbox_lease_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    project = Project(user_id=owner.id, project_name="sandbox-lease", status="active")
    db.add(project)
    db.flush()
    environment = SandboxEnvironment(
        public_id="sbx_execution_lease_01",
        project_id=project.id,
        owner_id=owner.id,
        agent_code="whitebox_tester",
        purpose="test",
        language="python",
        test_mode="whitebox",
        status="finalizing",
        runtime="runsc",
        image_ref="prism-sandbox-python:3.12",
        source_sha256="a" * 64,
        execution_token="current-token",
        resource_policy_json="{}",
        agent_config_json="{}",
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    db.add(environment)
    db.commit()

    assert (
        sandbox_service._complete_finalizing_transition(
            db,
            environment,
            final_status="succeeded",
            result_json='{"passed":true}',
            execution_token="stale-token",
        )
        is False
    )
    db.refresh(environment)
    assert environment.status == "finalizing"

    assert (
        sandbox_service._complete_finalizing_transition(
            db,
            environment,
            final_status="succeeded",
            result_json='{"passed":true}',
            execution_token="current-token",
        )
        is True
    )


@pytest.mark.parametrize(
    "late_status,expired",
    [
        ("stopping", False),
        ("stopped", False),
        ("expired", False),
        ("succeeded", True),
        ("succeeded", False),
    ],
)
def test_late_browser_worker_result_respects_current_sandbox_state(
    db,
    monkeypatch,
    late_status: str,
    expired: bool,
) -> None:
    owner = User(username="sandbox_late_browser_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    _grant_sandbox_globals(db, owner)
    project = Project(user_id=owner.id, project_name="late-browser", status="active")
    db.add(project)
    db.flush()
    environment = SandboxEnvironment(
        public_id="sbx_late_browser_01",
        project_id=project.id,
        owner_id=owner.id,
        agent_code="test_verifier",
        purpose="test",
        language="python",
        test_mode="blackbox",
        status="succeeded",
        runtime="runsc",
        image_ref="prism-sandbox-python:3.12",
        source_sha256="a" * 64,
        resource_policy_json="{}",
        agent_config_json="{}",
        remote_target_url="https://example.com/",
        remote_target_authorized_at=datetime.utcnow(),
        result_json='{"passed":true}',
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    db.add(environment)
    db.commit()

    monkeypatch.setattr(
        sandbox_service,
        "_normalize_browser_target",
        lambda _url: ("https://example.com/", SimpleNamespace(ip_address="93.184.216.34")),
    )
    monkeypatch.setattr(
        sandbox_service,
        "_select_browser_worker",
        lambda _db: SimpleNamespace(code="isolated-browser"),
    )

    def return_after_status_change(*_args, **_kwargs):
        values = {SandboxEnvironment.status: late_status}
        if expired:
            values[SandboxEnvironment.expires_at] = datetime.utcnow() - timedelta(seconds=1)
        db.query(SandboxEnvironment).filter_by(id=environment.id).update(
            values,
            synchronize_session=False,
        )
        db.commit()
        return {"result": {"protocol_version": "1.0", "passed": True}}

    monkeypatch.setattr(sandbox_service, "_call_worker", return_after_status_change)
    if late_status == "succeeded" and not expired:
        result = sandbox_service.run_browser_blackbox(
            db,
            owner,
            environment.public_id,
            "https://example.com/",
        )
        assert result["passed"] is True
        assert len(result["artifacts"]) == 1
    else:
        with pytest.raises(sandbox_service.ConflictError, match="已关闭|状态"):
            sandbox_service.run_browser_blackbox(
                db,
                owner,
                environment.public_id,
                "https://example.com/",
            )
    db.refresh(environment)
    assert environment.status == late_status
    if late_status == "succeeded" and not expired:
        assert len(json.loads(environment.result_json)["browser_blackbox_runs"]) == 1
        assert db.query(SandboxArtifact).filter_by(environment_id=environment.id).count() == 1
    else:
        assert json.loads(environment.result_json) == {"passed": True}
        assert db.query(SandboxArtifact).filter_by(environment_id=environment.id).count() == 0


def test_sandbox_snapshot_and_finalizing_require_current_execution_token(db) -> None:
    owner = User(username="sandbox_snapshot_lease_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    project = Project(user_id=owner.id, project_name="sandbox-snapshot-lease", status="active")
    db.add(project)
    db.flush()
    environment = SandboxEnvironment(
        public_id="sbx_snapshot_lease_01",
        project_id=project.id,
        owner_id=owner.id,
        agent_code="whitebox_tester",
        purpose="test",
        language="python",
        test_mode="whitebox",
        status="dispatching",
        runtime="runsc",
        image_ref="prism-sandbox-python:3.12",
        source_sha256="a" * 64,
        execution_token="current-token",
        resource_policy_json="{}",
        agent_config_json="{}",
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    db.add(environment)
    db.commit()

    request_envelope = {
        "request_id": environment.public_id,
        "purpose": "test",
        "language": "python",
        "test_mode": "whitebox",
        "db_type": "none",
        "source_sha256": "b" * 64,
        "ttl_seconds": 3600,
        "image_digest": "sha256:" + "c" * 64,
    }
    assert (
        sandbox_service._persist_worker_execution_snapshot(
            db,
            environment,
            execution_bytes=b"archive",
            source_sha256="b" * 64,
            repair_round=0,
            request_envelope=request_envelope,
            request_config_json='{"active_worker_request_id":"sbx_snapshot_lease_01"}',
            execution_token="stale-token",
        )
        is False
    )
    db.refresh(environment)
    assert environment.execution_archive_blob is None
    assert environment.worker_request_json is None

    assert (
        sandbox_service._persist_worker_execution_snapshot(
            db,
            environment,
            execution_bytes=b"archive",
            source_sha256="b" * 64,
            repair_round=0,
            request_envelope=request_envelope,
            request_config_json='{"active_worker_request_id":"sbx_snapshot_lease_01"}',
            execution_token="current-token",
        )
        is True
    )
    db.commit()
    db.refresh(environment)
    persisted = json.loads(environment.worker_request_json)
    assert persisted["ttl_seconds"] == 3600
    assert persisted["image_digest"] == "sha256:" + "c" * 64

    assert (
        sandbox_service._enter_finalizing(
            db,
            environment,
            result={"request_id": environment.public_id, "runtime": "runsc"},
            target_status="succeeded",
            execution_token="stale-token",
        )
        is False
    )
    db.refresh(environment)
    assert environment.status == "dispatching"

    assert (
        sandbox_service._enter_finalizing(
            db,
            environment,
            result={"request_id": environment.public_id, "runtime": "runsc"},
            target_status="succeeded",
            execution_token="current-token",
        )
        is True
    )
    db.commit()
    db.refresh(environment)
    assert environment.status == "finalizing"


def test_agent_test_paths_restore_persisted_execution_snapshot() -> None:
    source = base64.b64encode(
        _archive({"app.py": "print('ok')\n", "_agent_tests/test_app.py": "def test_ok(): assert True\n"})
    ).decode("ascii")
    assert sandbox_service._agent_test_paths(source) == {"test_app.py"}


def _persist_browser_environment(db, owner: User, *, suffix: str) -> tuple[Project, SandboxEnvironment]:
    _grant_sandbox_globals(db, owner)
    project = Project(user_id=owner.id, project_name=f"browser-{suffix}", status="active")
    db.add(project)
    db.flush()
    environment = SandboxEnvironment(
        public_id=f"sbx_browser_{suffix}",
        project_id=project.id,
        owner_id=owner.id,
        agent_code="blackbox_tester",
        purpose="test",
        language="python",
        test_mode="blackbox",
        status="succeeded",
        runtime="remote_http",
        image_ref="remote:https",
        source_sha256="a" * 64,
        resource_policy_json='{"network":"authorized_remote_target_only"}',
        agent_config_json='{"remote_only":true}',
        remote_target_url="https://example.com/allowed",
        remote_target_authorized_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=1),
        result_json='{"summary":"existing"}',
    )
    db.add(environment)
    db.commit()
    return project, environment


def test_browser_blackbox_reviewer_cannot_execute_owner_environment(db, monkeypatch) -> None:
    owner = User(username="browser_owner_acl", password="x", role="user", status=1)
    reviewer = User(username="browser_reviewer_acl", password="x", role="user", status=1)
    db.add_all([owner, reviewer])
    db.flush()
    _grant_sandbox_globals(db, reviewer)
    project, environment = _persist_browser_environment(db, owner, suffix="acl_01")
    db.add(ProjectMember(project_id=project.id, user_id=reviewer.id, role_in_project="reviewer"))
    db.commit()
    worker_call = MagicMock(side_effect=AssertionError("权限拒绝后不得调用 worker"))
    monkeypatch.setattr(sandbox_service, "_call_worker", worker_call)

    with pytest.raises(sandbox_service.ForbiddenError) as exc_info:
        sandbox_service.run_browser_blackbox(
            db,
            reviewer,
            environment.public_id,
            environment.remote_target_url,
        )

    assert exc_info.value.code == 40300
    worker_call.assert_not_called()


def test_browser_blackbox_rejects_target_drift_before_worker_call(db, monkeypatch) -> None:
    owner = User(username="browser_owner_target", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    _project, environment = _persist_browser_environment(db, owner, suffix="target_01")
    monkeypatch.setattr(
        sandbox_service,
        "pin_public_http_url",
        lambda url, **_kwargs: SimpleNamespace(original_url=url, ip_address="1.1.1.1"),
    )
    worker_call = MagicMock(side_effect=AssertionError("目标不一致时不得调用 worker"))
    monkeypatch.setattr(sandbox_service, "_call_worker", worker_call)

    with pytest.raises(sandbox_service.ForbiddenError) as exc_info:
        sandbox_service.run_browser_blackbox(
            db,
            owner,
            environment.public_id,
            "https://example.com/other",
        )

    assert exc_info.value.code == 40340
    worker_call.assert_not_called()


def test_browser_blackbox_appends_evidence_without_deleting_existing_artifacts(
    independent_transaction_db,
    monkeypatch,
) -> None:
    db = independent_transaction_db
    owner = User(username="browser_owner_artifact", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    _project, environment = _persist_browser_environment(db, owner, suffix="artifact_01")
    existing = SandboxArtifact(
        environment_id=environment.id,
        artifact_type="test_report",
        file_name="existing.json",
        mime_type="application/json",
        byte_size=2,
        sha256=hashlib.sha256(b"{}").hexdigest(),
        storage_ref="database://sandbox-artifact/existing",
        content_base64=base64.b64encode(b"{}").decode("ascii"),
    )
    db.add(existing)
    db.commit()
    worker = SimpleNamespace(code="browser-worker")
    monkeypatch.setattr(
        sandbox_service,
        "pin_public_http_url",
        lambda url, **_kwargs: SimpleNamespace(original_url=url, ip_address="1.1.1.1"),
    )
    monkeypatch.setattr(sandbox_service, "_select_browser_worker", lambda _db: worker)
    monkeypatch.setattr(
        sandbox_service,
        "_call_worker",
        lambda *_args, **_kwargs: {
            "result": {
                "protocol_version": "1.0",
                "kind": "playwright_browser_blackbox",
                "passed": True,
                "status_code": 200,
                "screenshot_base64": base64.b64encode(b"jpeg-bytes").decode("ascii"),
            }
        },
    )
    monkeypatch.setattr(sandbox_service.audit_service, "log", lambda *_args, **_kwargs: None)

    result = sandbox_service.run_browser_blackbox(
        db,
        owner,
        environment.public_id,
        environment.remote_target_url,
    )

    artifacts = (
        db.query(SandboxArtifact)
        .filter(SandboxArtifact.environment_id == environment.id)
        .order_by(SandboxArtifact.id)
        .all()
    )
    assert [row.artifact_type for row in artifacts] == [
        "test_report",
        "browser_screenshot",
        "browser_evidence",
    ]
    assert result["passed"] is True
    assert len(result["artifacts"]) == 2
    assert "browser_blackbox_runs" in json.loads(environment.result_json)


def test_stop_unknown_request_confirms_absence_and_persists_idempotent_tombstone(
    isolated_paths: Path,
    monkeypatch,
) -> None:
    request_id = "sandbox-unaccepted-01"
    calls = []
    monkeypatch.setattr(executor, "_remove_untracked_container", lambda value: calls.append(("container", value)))
    monkeypatch.setattr(executor, "_remove_job_data", lambda value: calls.append(("job", value)))

    result, duplicate = executor.stop_job({"request_id": request_id})
    repeated, repeated_duplicate = executor.stop_job({"request_id": request_id})

    assert duplicate is False
    assert result["request_id"] == request_id
    assert result["status"] == "stopped"
    assert result["result"]["cleanup_confirmed"] is True
    assert calls == [("container", request_id), ("job", request_id)]
    assert repeated_duplicate is True
    assert repeated["status"] == "stopped"


def test_stop_waits_for_submission_startup_before_confirming_unknown_resources(
    isolated_paths: Path,
    monkeypatch,
) -> None:
    request_id = "sandbox-starting-01"
    cleanup_calls = []
    monkeypatch.setattr(executor, "_remove_untracked_container", lambda value: cleanup_calls.append(value))
    monkeypatch.setattr(executor, "_remove_job_data", lambda _value: None)
    with executor.STATE_CONDITION:
        executor.SUBMISSIONS_INFLIGHT.add(request_id)
    result = []
    errors = []

    def stop() -> None:
        try:
            result.append(executor.stop_job({"request_id": request_id}))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=stop)
    thread.start()
    time.sleep(0.05)
    assert thread.is_alive()
    assert cleanup_calls == []
    with executor.STATE_CONDITION:
        executor.SUBMISSIONS_INFLIGHT.discard(request_id)
        executor.STATE_CONDITION.notify_all()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    assert cleanup_calls == [request_id]
    assert result[0][0]["status"] == "stopped"


def test_submit_is_rejected_while_unknown_request_id_cleanup_is_reserved(
    isolated_paths: Path,
    monkeypatch,
) -> None:
    request_id = "sandbox-stop-reserved-01"
    inspect_started = threading.Event()
    allow_inspect_to_finish = threading.Event()

    def block_cleanup(_request_id: str) -> None:
        inspect_started.set()
        assert allow_inspect_to_finish.wait(timeout=2)

    monkeypatch.setattr(executor, "_remove_untracked_container", block_cleanup)
    monkeypatch.setattr(executor, "_remove_job_data", lambda _request_id: None)
    stop_result = []
    stop_errors = []

    def stop() -> None:
        try:
            stop_result.append(executor.stop_job({"request_id": request_id}))
        except Exception as exc:  # pragma: no cover - surfaced below
            stop_errors.append(exc)

    thread = threading.Thread(target=stop)
    thread.start()
    assert inspect_started.wait(timeout=2)
    source = _archive({"main.py": "print('must not start')\n"})

    with pytest.raises(executor.ConflictError, match="正在停止"):
        executor.submit_job(_payload(source, request_id=request_id))

    allow_inspect_to_finish.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert stop_errors == []
    assert stop_result[0][0]["status"] == "stopped"
    assert executor._read_state(request_id)["result"]["cleanup_confirmed"] is True


def test_stop_retries_cleanup_for_legacy_terminal_failure_with_cleanup_error(
    isolated_paths: Path,
    monkeypatch,
) -> None:
    request_id = "sandbox-legacy-cleanup-01"
    executor._write_state(
        {
            "request_id": request_id,
            "request_digest": "legacy-request-digest",
            "purpose": "test",
            "language": "python",
            "test_mode": "whitebox",
            "source_sha256": "0" * 64,
            "status": "failed",
            "stage": "failed",
            "runtime": "runsc",
            "image_ref": "prism-sandbox-python:3.11",
            "events": [],
            "result": {"outcome": "failed", "cleanup_error": "HTTP 404"},
            "error": "执行失败，资源回收待重试",
        }
    )
    removed = []
    monkeypatch.setattr(executor, "_remove_container", lambda name: removed.append(("container", name)))
    monkeypatch.setattr(executor, "_remove_job_data", lambda value: removed.append(("source", value)))

    result, duplicate = executor.stop_job({"request_id": request_id})

    assert duplicate is False
    assert result["status"] == "failed"
    assert result["result"]["cleanup_confirmed"] is True
    assert "cleanup_error" not in result["result"]
    assert "资源回收待重试" not in result["error"]
    assert result["stage"] == "failed"
    assert [item[0] for item in removed] == ["container", "source"]


def test_remove_container_requires_docker_absence_proof(monkeypatch) -> None:
    calls = iter(
        [
            {"exit_code": 1, "stdout": "", "stderr": "cannot connect to Docker daemon"},
            {"exit_code": 1, "stdout": "", "stderr": "Cannot connect to the Docker daemon"},
        ]
    )
    monkeypatch.setattr(executor, "_run_command", lambda *_args, **_kwargs: next(calls))

    with pytest.raises(RuntimeError, match="未能确认"):
        executor._remove_container("prism-sandbox-unknown")
