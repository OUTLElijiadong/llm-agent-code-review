from __future__ import annotations

import io
import json
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from app.services.decompilation_service import (
    DecompilationError,
    DecompilationStatus,
    InputKind,
    build_jadx_command,
    choose_decompilation_tool,
    inspect_archive_members,
    inspect_decompilation_input,
    manifest_decompiled_sources,
    plan_decompilation_archive,
    run_jadx_cli,
)
from app.utils.archive_extractor import ArchiveMember


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return out.getvalue()


def _tar_gz_bytes(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        for path, content in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return out.getvalue()


def test_source_archive_skips_decompilation() -> None:
    raw = _zip_bytes({"src/main.py": b"print('ok')\n"})

    inspected = inspect_decompilation_input("project.zip", raw)
    decision = choose_decompilation_tool(inspected)

    assert inspected.kind is InputKind.SOURCE_ARCHIVE
    assert decision.status is DecompilationStatus.SKIPPED
    assert decision.tool == "none"
    assert decision.input_sha256


@pytest.mark.parametrize(
    ("filename", "raw", "expected"),
    [
        ("classes.dex", b"dex\n039\x00" + b"\x00" * 40, InputKind.ANDROID_DEX),
        (
            "client.apk",
            _zip_bytes({"AndroidManifest.xml": b"manifest", "classes.dex": b"dex\n039\x00"}),
            InputKind.ANDROID_APK,
        ),
        (
            "client.aab",
            _zip_bytes({"base/manifest/AndroidManifest.xml": b"manifest", "base/dex/classes.dex": b"dex\n039\x00"}),
            InputKind.ANDROID_AAB,
        ),
    ],
)
def test_android_inputs_choose_jadx(filename: str, raw: bytes, expected: InputKind) -> None:
    inspected = inspect_decompilation_input(filename, raw)
    decision = choose_decompilation_tool(inspected)

    assert inspected.kind is expected
    assert decision.status is DecompilationStatus.PLANNED
    assert decision.tool == "jadx"


def test_fake_apk_with_non_zip_content_is_rejected() -> None:
    inspected = inspect_decompilation_input("fake.apk", b"not a zip")
    decision = choose_decompilation_tool(inspected)

    assert inspected.kind is InputKind.INVALID_ANDROID
    assert decision.status is DecompilationStatus.UNSUPPORTED
    assert "结构" in decision.reason or "格式" in decision.reason


def test_java_jar_is_not_misclassified_as_android() -> None:
    raw = _zip_bytes({"com/example/App.class": b"\xca\xfe\xba\xbe"})

    inspected = inspect_decompilation_input("app.jar", raw)
    decision = choose_decompilation_tool(inspected)

    assert inspected.kind is InputKind.JAVA_JAR
    assert decision.status is DecompilationStatus.UNSUPPORTED
    assert decision.tool == "none"


def test_archive_member_inspection_lists_embedded_android_candidates() -> None:
    members = [
        ArchiveMember("src/main.py", b"print('ok')"),
        ArchiveMember(
            "artifacts/client.apk",
            _zip_bytes({"AndroidManifest.xml": b"manifest", "classes.dex": b"dex\n039\x00"}),
        ),
    ]

    summary = inspect_archive_members(members)

    assert summary["status"] == "planned"
    assert summary["candidate_count"] == 1
    assert summary["candidates"][0]["path"] == "artifacts/client.apk"
    assert summary["candidates"][0]["tool"] == "jadx"


def test_plan_decompilation_archive_handles_direct_apk() -> None:
    raw = _zip_bytes({"AndroidManifest.xml": b"manifest", "classes.dex": b"dex\n039\x00"})

    plan = plan_decompilation_archive(raw, "client.apk")

    assert plan["status"] == "planned"
    assert plan["tool"] == "jadx"


def test_plan_decompilation_archive_handles_tar_gz_source_archive() -> None:
    raw = _tar_gz_bytes({"src/index.php": b"<?php echo 'ok';"})

    plan = plan_decompilation_archive(raw, "bWAPP-master.tar.gz")

    assert plan["status"] == "skipped"
    assert plan["candidate_count"] == 0


def test_plan_decompilation_archive_detects_embedded_apk_in_tar_gz() -> None:
    apk = _zip_bytes({"AndroidManifest.xml": b"manifest", "classes.dex": b"dex\n039\x00"})
    raw = _tar_gz_bytes({"artifacts/client.apk": apk})

    plan = plan_decompilation_archive(raw, "bundle.tar.gz")

    assert plan["status"] == "planned"
    assert plan["candidate_count"] == 1
    assert plan["candidates"][0]["path"] == "artifacts/client.apk"


def test_plan_decompilation_archive_rejects_corrupt_tar_gz() -> None:
    with pytest.raises(DecompilationError, match="无法读取"):
        plan_decompilation_archive(b"not a tar gzip", "broken.tar.gz")


def test_build_jadx_command_is_fixed_and_contains_no_shell() -> None:
    command = build_jadx_command(
        executable="/opt/jadx/bin/jadx",
        input_path=Path("/input/client.apk"),
        output_dir=Path("/output"),
    )

    assert command[0] == "/opt/jadx/bin/jadx"
    assert command[-1] == "/input/client.apk"
    assert "--output-dir" in command
    assert "--no-debug-info" in command
    assert all(";" not in part and "&&" not in part for part in command)


def test_manifest_decompiled_sources_is_deterministic(tmp_path: Path) -> None:
    (tmp_path / "sources/com/example").mkdir(parents=True)
    (tmp_path / "sources/com/example/App.java").write_text("class App {}\n", encoding="utf-8")
    (tmp_path / "sources/com/example/Util.kt").write_text("class Util\n", encoding="utf-8")

    first = manifest_decompiled_sources(tmp_path)
    second = manifest_decompiled_sources(tmp_path)

    assert first == second
    assert first["output_file_count"] == 2
    assert len(first["output_sha256"]) == 64
    assert [item["path"] for item in first["files"]] == sorted(
        item["path"] for item in first["files"]
    )
    json.dumps(first, ensure_ascii=False)


def test_manifest_rejects_empty_output(tmp_path: Path) -> None:
    with pytest.raises(DecompilationError, match="没有生成"):
        manifest_decompiled_sources(tmp_path)


def test_run_jadx_cli_returns_structured_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    input_path = tmp_path / "client.apk"
    output_dir = tmp_path / "out"
    input_path.write_bytes(_zip_bytes({"AndroidManifest.xml": b"x", "classes.dex": b"dex\n039\x00"}))

    def fake_run(command, **kwargs):  # noqa: ANN001
        assert kwargs["shell"] is False
        (output_dir / "sources").mkdir(parents=True)
        (output_dir / "sources/App.java").write_text("class App {}\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="done", stderr="warning")

    monkeypatch.setattr(subprocess, "run", fake_run)

    evidence = run_jadx_cli(
        input_path=input_path,
        output_dir=output_dir,
        executable="/opt/jadx/bin/jadx",
        tool_version="1.5.6",
        timeout_seconds=30,
    )

    assert evidence["status"] == "succeeded"
    assert evidence["tool"] == "jadx"
    assert evidence["tool_version"] == "1.5.6"
    assert evidence["exit_code"] == 0
    assert evidence["output_file_count"] == 1
    assert len(evidence["input_sha256"]) == 64
    assert evidence["input_artifact_sha256s"] == [evidence["input_sha256"]]
    assert len(evidence["output_sha256"]) == 64


def test_run_jadx_cli_fails_closed_on_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    input_path = tmp_path / "client.dex"
    input_path.write_bytes(b"dex\n039\x00")

    def fake_run(command, **kwargs):  # noqa: ANN001
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(DecompilationError, match="超时"):
        run_jadx_cli(
            input_path=input_path,
            output_dir=tmp_path / "out",
            executable="/opt/jadx/bin/jadx",
            tool_version="1.5.6",
            timeout_seconds=1,
        )


def test_run_jadx_cli_fails_closed_on_nonzero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    input_path = tmp_path / "client.dex"
    input_path.write_bytes(b"dex\n039\x00")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 2, stdout="", stderr="decode failed"
        ),
    )

    with pytest.raises(DecompilationError, match="退出码 2"):
        run_jadx_cli(
            input_path=input_path,
            output_dir=tmp_path / "out",
            executable="/opt/jadx/bin/jadx",
            tool_version="1.5.6",
            timeout_seconds=30,
        )
