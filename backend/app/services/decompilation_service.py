"""Deterministic Android decompilation decisions and evidence collection.

The model may explain or display these decisions, but it never controls the
executable, arguments, paths, timeout, or output acceptance rules.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import zipfile
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Iterable

from app.utils.archive_extractor import ArchiveMember

ANDROID_EXTENSIONS = frozenset({".apk", ".aab", ".dex"})
DECOMPILED_SOURCE_EXTENSIONS = frozenset({".java", ".kt"})
DEFAULT_MAX_OUTPUT_FILES = 20_000
DEFAULT_MAX_OUTPUT_BYTES = 512 * 1024 * 1024
MAX_LOG_CHARS = 32_000


class DecompilationError(RuntimeError):
    """A deterministic decompilation failure that must remain fail-closed."""


class InputKind(str, Enum):
    SOURCE_ARCHIVE = "source_archive"
    ANDROID_APK = "android_apk"
    ANDROID_AAB = "android_aab"
    ANDROID_DEX = "android_dex"
    JAVA_JAR = "java_jar"
    INVALID_ANDROID = "invalid_android"
    UNKNOWN_BINARY = "unknown_binary"


class DecompilationStatus(str, Enum):
    SKIPPED = "skipped"
    PLANNED = "planned"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class InspectedInput:
    filename: str
    kind: InputKind
    input_sha256: str
    size_bytes: int
    reason: str


@dataclass(frozen=True)
class DecompilationDecision:
    status: DecompilationStatus
    input_kind: InputKind
    input_sha256: str
    tool: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["input_kind"] = self.input_kind.value
        return payload


def _safe_basename(filename: str) -> str:
    value = PurePosixPath((filename or "").replace("\\", "/")).name
    return value[:255] or "artifact"


def _zip_names(raw: bytes) -> set[str] | None:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            return {
                info.filename.replace("\\", "/").lstrip("/")
                for info in archive.infolist()
                if not info.is_dir()
            }
    except (zipfile.BadZipFile, OSError, ValueError):
        return None


def _zip_has_dex_magic(raw: bytes, names: set[str]) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for info in archive.infolist():
                name = info.filename.replace("\\", "/").lstrip("/")
                if not info.is_dir() and name in names and name.endswith("classes.dex"):
                    if archive.read(info, pwd=None).startswith(b"dex\n"):
                        return True
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError):
        return False
    return False


def inspect_decompilation_input(filename: str, raw: bytes) -> InspectedInput:
    """Classify one already-quarantined input without executing it."""
    safe_name = _safe_basename(filename)
    suffix = Path(safe_name.lower()).suffix
    digest = hashlib.sha256(raw).hexdigest()
    size = len(raw)
    if suffix == ".dex":
        if raw.startswith(b"dex\n"):
            return InspectedInput(safe_name, InputKind.ANDROID_DEX, digest, size, "DEX magic 与扩展名一致")
        return InspectedInput(safe_name, InputKind.INVALID_ANDROID, digest, size, "DEX 扩展名与文件 magic 不一致")
    if suffix in {".apk", ".aab", ".jar", ".zip"}:
        names = _zip_names(raw)
        if names is None:
            kind = InputKind.INVALID_ANDROID if suffix in {".apk", ".aab"} else InputKind.UNKNOWN_BINARY
            return InspectedInput(safe_name, kind, digest, size, "ZIP 容器结构无效")
        has_dex = any(name == "classes.dex" or name.endswith("/classes.dex") for name in names)
        has_valid_dex = has_dex and _zip_has_dex_magic(raw, names)
        has_apk_manifest = "AndroidManifest.xml" in names
        has_aab_manifest = any(
            name.endswith("/manifest/AndroidManifest.xml") for name in names
        )
        if suffix == ".apk":
            kind = InputKind.ANDROID_APK if has_valid_dex and has_apk_manifest else InputKind.INVALID_ANDROID
            reason = "APK 结构已确认" if kind is InputKind.ANDROID_APK else "APK 缺少 manifest 或 classes.dex"
            return InspectedInput(safe_name, kind, digest, size, reason)
        if suffix == ".aab":
            kind = InputKind.ANDROID_AAB if has_valid_dex and has_aab_manifest else InputKind.INVALID_ANDROID
            reason = "AAB 结构已确认" if kind is InputKind.ANDROID_AAB else "AAB 缺少模块 manifest 或 classes.dex"
            return InspectedInput(safe_name, kind, digest, size, reason)
        if suffix == ".jar":
            has_class = any(name.endswith(".class") for name in names)
            kind = InputKind.JAVA_JAR if has_class else InputKind.UNKNOWN_BINARY
            reason = "Java class 归档需要专用 Java 反编译器" if has_class else "JAR 未发现 class 文件"
            return InspectedInput(safe_name, kind, digest, size, reason)
        return InspectedInput(safe_name, InputKind.SOURCE_ARCHIVE, digest, size, "普通源码 ZIP 不需要反编译")
    return InspectedInput(safe_name, InputKind.SOURCE_ARCHIVE, digest, size, "源码输入不需要反编译")


def choose_decompilation_tool(inspected: InspectedInput) -> DecompilationDecision:
    if inspected.kind in {InputKind.ANDROID_APK, InputKind.ANDROID_AAB, InputKind.ANDROID_DEX}:
        return DecompilationDecision(
            DecompilationStatus.PLANNED,
            inspected.kind,
            inspected.input_sha256,
            "jadx",
            "Android 字节码使用固定 JADX CLI 反编译",
        )
    if inspected.kind is InputKind.SOURCE_ARCHIVE:
        return DecompilationDecision(
            DecompilationStatus.SKIPPED,
            inspected.kind,
            inspected.input_sha256,
            "none",
            "已是源码输入，跳过反编译",
        )
    return DecompilationDecision(
        DecompilationStatus.UNSUPPORTED,
        inspected.kind,
        inspected.input_sha256,
        "none",
        inspected.reason,
    )


def inspect_archive_members(members: Iterable[ArchiveMember]) -> dict[str, object]:
    """Find embedded Android artifacts in a safe source archive member list."""
    candidates: list[dict[str, object]] = []
    for member in members:
        suffix = Path(member.path.lower()).suffix
        if suffix not in ANDROID_EXTENSIONS and suffix != ".jar":
            continue
        inspected = inspect_decompilation_input(member.path, member.content)
        decision = choose_decompilation_tool(inspected)
        payload = decision.to_dict()
        payload["path"] = member.path
        payload["size_bytes"] = len(member.content)
        candidates.append(payload)
    planned = sum(item["status"] == DecompilationStatus.PLANNED.value for item in candidates)
    unsupported = sum(item["status"] == DecompilationStatus.UNSUPPORTED.value for item in candidates)
    status = "planned" if planned else "unsupported" if unsupported else "skipped"
    return {
        "status": status,
        "candidate_count": len(candidates),
        "planned_count": planned,
        "unsupported_count": unsupported,
        "candidates": candidates,
    }


def plan_decompilation_archive(raw: bytes, filename: str) -> dict[str, object]:
    """Plan direct or embedded Android artifacts in an already validated archive."""
    direct = inspect_decompilation_input(filename, raw)
    if direct.kind is not InputKind.SOURCE_ARCHIVE:
        return choose_decompilation_tool(direct).to_dict()
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = [
                ArchiveMember(info.filename, archive.read(info))
                for info in archive.infolist()
                if not info.is_dir()
                and Path(info.filename.lower()).suffix in ANDROID_EXTENSIONS.union({".jar"})
            ]
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise DecompilationError("源码归档无法读取反编译候选成员") from exc
    for member in members:
        normalized = PurePosixPath(member.path.replace("\\", "/"))
        if normalized.is_absolute() or ".." in normalized.parts:
            raise DecompilationError("反编译候选成员路径不安全")
    return inspect_archive_members(members)


def build_jadx_command(*, executable: str, input_path: Path, output_dir: Path) -> tuple[str, ...]:
    """Build the fixed argv contract used by the trusted decompilation worker."""
    if not executable or "\x00" in executable:
        raise DecompilationError("JADX 可执行文件配置无效")
    return (
        executable,
        "--output-dir",
        str(output_dir),
        "--no-debug-info",
        "--no-inline-anonymous",
        "--show-bad-code",
        str(input_path),
    )


def manifest_decompiled_sources(
    output_dir: Path,
    *,
    max_files: int = DEFAULT_MAX_OUTPUT_FILES,
    max_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> dict[str, object]:
    """Validate JADX output and return a deterministic content manifest."""
    root = output_dir.resolve()
    files: list[dict[str, object]] = []
    total_bytes = 0
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in DECOMPILED_SOURCE_EXTENSIONS:
            continue
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise DecompilationError("反编译输出越过受控目录") from exc
        size = resolved.stat().st_size
        total_bytes += size
        if len(files) + 1 > max_files or total_bytes > max_bytes:
            raise DecompilationError("反编译输出超过文件数或总字节上限")
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        files.append({"path": relative, "sha256": digest, "size_bytes": size})
    if not files:
        raise DecompilationError("JADX 没有生成可审计的 Java/Kotlin 源码")
    canonical = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "output_file_count": len(files),
        "output_size_bytes": total_bytes,
        "output_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "files": files,
    }


def run_jadx_cli(
    *,
    input_path: Path,
    output_dir: Path,
    executable: str,
    tool_version: str,
    timeout_seconds: int,
) -> dict[str, object]:
    """Run trusted JADX CLI and return evidence; never uses a shell."""
    if not input_path.is_file():
        raise DecompilationError("反编译输入文件不存在")
    output_dir.mkdir(parents=True, exist_ok=True)
    command = build_jadx_command(
        executable=executable,
        input_path=input_path,
        output_dir=output_dir,
    )
    try:
        completed = subprocess.run(
            command,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=max(1, timeout_seconds),
            check=False,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
        )
    except subprocess.TimeoutExpired as exc:
        raise DecompilationError(f"JADX 反编译超时（{max(1, timeout_seconds)} 秒）") from exc
    except OSError as exc:
        raise DecompilationError("JADX 工具不可用") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:500]
        suffix = f"：{detail}" if detail else ""
        raise DecompilationError(f"JADX 反编译失败，退出码 {completed.returncode}{suffix}")
    manifest = manifest_decompiled_sources(output_dir)
    input_sha256 = hashlib.sha256(input_path.read_bytes()).hexdigest()
    return {
        "status": DecompilationStatus.SUCCEEDED.value,
        "input_kind": inspect_decompilation_input(input_path.name, input_path.read_bytes()).kind.value,
        "tool": "jadx",
        "tool_version": tool_version,
        "input_sha256": input_sha256,
        "input_artifact_sha256s": [input_sha256],
        "output_sha256": manifest["output_sha256"],
        "output_file_count": manifest["output_file_count"],
        "output_size_bytes": manifest["output_size_bytes"],
        "exit_code": completed.returncode,
        "stdout": (completed.stdout or "")[:MAX_LOG_CHARS],
        "stderr": (completed.stderr or "")[:MAX_LOG_CHARS],
        "files": manifest["files"],
    }
