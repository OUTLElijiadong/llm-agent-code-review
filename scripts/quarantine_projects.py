"""显式、可逆的项目隔离工具，不导入应用配置，不读取 .env。

操作契约：
1. 先部署业务访问过滤，并由主代理确认没有遗留业务入口绕过权限过滤。
   运维在维护窗口暂停目标项目的在途写入，独立完成数据库备份和恢复验证。
2. 仅从 PRISM_QUARANTINE_DATABASE_URL 获取操作者显式注入的连接串；不要放进
   命令行或版本库。每个 --target 必须给出 ID、精确名称、所属用户 ID；不内置
   生产项目 ID，不接受 LIKE，不会因名称相似或停用账号自动选择任何项目。
3. quarantine/restore 均默认 dry-run，不产生数据库写入或证据文件。
   --apply 必须给出新 --manifest 路径、已有 --backup-file 及独立核验的
   --backup-sha256、--operator 与 --reason（操作者/审批引用标识）。
   备份仅流式计算哈希，不复制内容；哈希通过不等于已经验证备份可恢复。
4. 一个事务内锁定显式项目；目标仍有 pending/running 审查任务时整批拒绝。
   只允许 active/archived -> quarantined。
   清单先落盘并 fsync，记录精确状态及身份/时间戳；只更新 status，并显式保留
   update_time。关联业务数据不修改；源码、审计和模型调用日志不读、不复制、不删除。
5. restore 必须再次给出同一组 --target、原 --restore-manifest 及其独立保管的
   --restore-sha256；校验数据库指纹和完整隔离后快照，任何变化均整批拒绝。
   恢复按原值还原 active/archived，不通过业务 API 恢复，不把 archived 当删除。
6. 清单、JSONL 审计、结果回执均独占创建、权限 0600，不覆盖历史文件。
   清单是准备证据，不是提交证明；.result.json 的 committed 才是完成回执。
   崩溃或缺少完成回执时禁止直接重试：先对照 before/after 只读核验；若已经
   全部处于 after，可用原清单恢复。混合/漂移状态需人工处理，绝不部分恢复。

示例（占位值须由只读证据替换，以下命令默认不执行写入）：
    python scripts/quarantine_projects.py quarantine --target ID EXACT_NAME USER_ID
    python scripts/quarantine_projects.py restore --target ID EXACT_NAME USER_ID \
        --restore-manifest /secure/ops/quarantine.json --restore-sha256 SHA256

实际写入另加 --apply --manifest /secure/ops/NEW.json --backup-file /secure/BACKUP \
    --backup-sha256 SHA256 --operator OPERATOR_ID --reason APPROVAL_ID
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import BigInteger, DateTime, String, column, create_engine, select, table, update
from sqlalchemy.engine import Engine

SCHEMA = "prism-project-quarantine-v1"
PUBLIC_STATUSES = {"active", "archived"}
PROJECT = table(
    "project", column("id", BigInteger), column("user_id", BigInteger),
    column("project_name", String(100)), column("status", String(20)),
    column("create_time", DateTime), column("update_time", DateTime),
)
REVIEW_TASK = table(
    "review_task", column("id", BigInteger), column("project_id", BigInteger), column("status", String(20)),
)
SNAPSHOT_FIELDS = set(PROJECT.c.keys())


class QuarantineError(ValueError):
    """可安全展示的操作门禁错误，不包含连接信息或数据库异常正文。"""


@dataclass(frozen=True)
class Target:
    project_id: int
    expected_name: str
    expected_user_id: int

    def __post_init__(self):
        for value in (self.project_id, self.expected_user_id):
            if type(value) is not int or not 0 < value < 2 ** 63:
                raise QuarantineError("项目 ID 和所属用户 ID 必须是正整数")
        if not isinstance(self.expected_name, str) or not self.expected_name.strip() or len(self.expected_name) > 100:
            raise QuarantineError("必须提供 1 至 100 字符的精确项目名")


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def database_fingerprint(engine: Engine) -> str:
    """仅对数据库位置计算指纹，不输出 URL、用户名、密码或连接参数。"""
    url = engine.url
    database = url.database
    if url.get_backend_name() == "sqlite" and database not in {None, "", ":memory:"}:
        database = str(Path(database).resolve())
    location = {
        "backend": url.get_backend_name(), "host": url.host, "port": url.port,
        "database": database, "unix_socket": url.query.get("unix_socket"),
    }
    return hashlib.sha256(_json_bytes(location)).hexdigest()


def _verify_file(path: Path, expected_sha256: str) -> dict:
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise QuarantineError("必须提供独立核验的 SHA-256")
    if not path.is_file() or path.is_symlink():
        raise QuarantineError("备份或清单必须是现存的普通文件，不能是符号链接")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    if size == 0 or digest.hexdigest() != expected_sha256.lower():
        raise QuarantineError("备份或清单为空或 SHA-256 不匹配")
    return {"sha256": digest.hexdigest(), "bytes": size}


def _exclusive_file(path: Path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(descriptor, "wb")


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_manifest(path: Path, content: dict) -> str:
    raw = _json_bytes(content)
    with _exclusive_file(path) as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    _sync_directory(path.parent)
    return hashlib.sha256(raw).hexdigest()


def _audit(output, operation_id: str, state: str, changed_ids: list[int]) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(), "operation_id": operation_id,
        "state": state, "changed_ids": changed_ids,
    }
    output.write((json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    output.flush()
    os.fsync(output.fileno())


def _snapshot(connection, ids: list[int], *, lock: bool) -> list[dict]:
    query = select(PROJECT).where(PROJECT.c.id.in_(ids)).order_by(PROJECT.c.id)
    if lock:
        query = query.with_for_update()
    return [
        {key: value.isoformat(timespec="microseconds") if isinstance(value, datetime) else value
         for key, value in row.items()}
        for row in connection.execute(query).mappings()
    ]


def _check_targets(rows: list[dict], targets: list[Target]) -> None:
    expected = {target.project_id: target for target in targets}
    if len(rows) != len(expected) or {row["id"] for row in rows} != set(expected):
        raise QuarantineError("至少一个显式项目 ID 不存在；整批拒绝")
    for row in rows:
        target = expected[row["id"]]
        if row["project_name"] != target.expected_name or row["user_id"] != target.expected_user_id:
            raise QuarantineError(f"项目 {target.project_id} 的精确名称或所属用户不匹配；整批拒绝")


def _require_idle_projects(connection, ids: list[int], *, lock: bool) -> None:
    query = select(REVIEW_TASK.c.id).where(
        REVIEW_TASK.c.project_id.in_(ids), REVIEW_TASK.c.status.in_(("pending", "running")),
    ).limit(1)
    if lock:
        query = query.with_for_update()
    if connection.execute(query).first() is not None:
        raise QuarantineError("目标项目仍有 pending/running 审查任务；请正常结束任务后重试，工具不会取消任务")


def _restore_plan(path: Path, digest: str, fingerprint: str, targets: list[Target]) -> dict:
    verified = _verify_file(path, digest)
    if verified["bytes"] > 1024 * 1024:
        raise QuarantineError("恢复清单超出大小上限")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest.lower():
        raise QuarantineError("恢复清单在读取期间发生变化")
    try:
        manifest = json.loads(raw)
        if manifest["schema"] != SCHEMA or manifest["action"] != "quarantine":
            raise QuarantineError("仅支持本工具生成的隔离清单")
        if manifest["database_fingerprint"] != fingerprint:
            raise QuarantineError("清单与当前数据库指纹不匹配")
        before, after = manifest["before"], manifest["after"]
        _check_targets(before, targets)
        _check_targets(after, targets)
        for previous, current in zip(before, after):
            if set(previous) != SNAPSHOT_FIELDS or set(current) != SNAPSHOT_FIELDS:
                raise QuarantineError("恢复清单字段不符合允许范围")
            if previous["status"] not in PUBLIC_STATUSES or current != {**previous, "status": "quarantined"}:
                raise QuarantineError("恢复清单不是合法的逐项隔离变更")
            for key in ("create_time", "update_time"):
                datetime.fromisoformat(previous[key])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise QuarantineError("恢复清单结构无效") from exc
    return manifest


def run_operation(
    engine: Engine, *, targets: list[Target], action: str = "quarantine", apply: bool = False,
    manifest_path: Optional[Path] = None, restore_manifest: Optional[Path] = None,
    restore_sha256: Optional[str] = None, backup_file: Optional[Path] = None,
    backup_sha256: Optional[str] = None, operator: str = "", reason: str = "",
) -> dict:
    """在单事务内执行显式状态切换；默认只返回计划，不提交任何项目变更。"""
    if action not in {"quarantine", "restore"}:
        raise QuarantineError("仅支持 quarantine 或 restore")
    if not targets or len(targets) > 100 or any(not isinstance(target, Target) for target in targets):
        raise QuarantineError("必须逐项指定 1 至 100 个项目")
    targets = sorted(targets, key=lambda target: target.project_id)
    ids = [target.project_id for target in targets]
    if len(set(ids)) != len(ids):
        raise QuarantineError("项目 ID 不可重复")
    if engine.url.get_backend_name() not in {"sqlite", "mysql"}:
        raise QuarantineError("只支持项目使用的 MySQL 和隔离测试 SQLite")
    fingerprint = database_fingerprint(engine)
    original = None
    if action == "restore":
        if restore_manifest is None or restore_sha256 is None:
            raise QuarantineError("恢复必须提供原清单和独立保存的 SHA-256")
        original = _restore_plan(Path(restore_manifest), restore_sha256, fingerprint, targets)
    elif restore_manifest is not None or restore_sha256 is not None:
        raise QuarantineError("隔离操作不得携带恢复参数")
    backup = None
    if apply:
        if manifest_path is None or backup_file is None or backup_sha256 is None:
            raise QuarantineError("写入必须提供新清单路径和已验证备份及 SHA-256")
        if not all(re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value) for value in (operator, reason)):
            raise QuarantineError("写入必须提供操作者和审批引用标识，不接受自由文本或凭据")
        manifest_path = Path(manifest_path).absolute()
        if not manifest_path.parent.is_dir():
            raise QuarantineError("清单父目录须由操作者预先创建")
        evidence_paths = [
            manifest_path, Path(str(manifest_path) + ".audit.jsonl"), Path(str(manifest_path) + ".result.json"),
        ]
        if any(path.exists() or path.is_symlink() for path in evidence_paths):
            raise QuarantineError("证据路径已存在；禁止覆盖或直接重试")
        backup = _verify_file(Path(backup_file), backup_sha256)
    operation_id = uuid.uuid4().hex
    audit_output = None
    committed = False
    try:
        with engine.begin() as connection:
            before = _snapshot(connection, ids, lock=apply)
            _check_targets(before, targets)
            _require_idle_projects(connection, ids, lock=apply)
            if action == "quarantine":
                if any(row["status"] not in PUBLIC_STATUSES for row in before):
                    raise QuarantineError("仅允许隔离 active/archived 项目；内部态、已删除或隔离态整批拒绝")
                after = [{**row, "status": "quarantined"} for row in before]
            else:
                if before != original["after"]:
                    raise QuarantineError("项目已偏离隔离后快照；禁止覆盖后续变更")
                after = original["before"]
            plan = {
                "schema": SCHEMA, "operation_id": operation_id, "action": action,
                "created_at": datetime.now(timezone.utc).isoformat(), "operator": operator,
                "reason": reason, "database_fingerprint": fingerprint, "backup": backup,
                "targets": [asdict(target) for target in targets], "before": before, "after": after,
                "restore_of": restore_sha256 if original else None,
            }
            if not apply:
                return {**plan, "state": "dry_run", "changed_ids": ids}
            manifest_sha256 = _write_manifest(manifest_path, plan)
            audit_output = _exclusive_file(evidence_paths[1])
            _audit(audit_output, operation_id, "prepared", ids)
            _sync_directory(manifest_path.parent)
            for previous, current in zip(before, after):
                expected = {
                    key: datetime.fromisoformat(value) if key in {"create_time", "update_time"} else value
                    for key, value in previous.items()
                }
                statement = update(PROJECT).where(
                    *(PROJECT.c[key] == value for key, value in expected.items())
                ).values(status=current["status"], update_time=expected["update_time"])
                if connection.execute(statement).rowcount != 1:
                    raise QuarantineError("状态条件更新未精确命中一行；整批回滚")
            if _snapshot(connection, ids, lock=False) != after:
                raise QuarantineError("变更后快照不匹配；整批回滚")
        committed = True
        result = {
            "state": "committed", "operation_id": operation_id, "changed_ids": ids,
            "manifest_sha256": manifest_sha256,
        }
        try:
            _audit(audit_output, operation_id, "committed", ids)
            _write_manifest(evidence_paths[2], result)
        except OSError as exc:
            raise QuarantineError("事务已提交但完成回执未完整落盘；先只读核验清单，禁止直接重试") from exc
        return result
    except Exception:
        if audit_output is not None and not committed:
            try:
                _audit(audit_output, operation_id, "unconfirmed_check_before_retry", ids)
            except OSError:
                pass
        raise
    finally:
        if audit_output is not None:
            audit_output.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("quarantine", "restore"))
    parser.add_argument("--target", action="append", required=True, nargs=3, metavar=("ID", "EXACT_NAME", "USER_ID"))
    parser.add_argument("--apply", action="store_true", help="显式启用写入；默认 dry-run")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--backup-file", type=Path)
    parser.add_argument("--backup-sha256")
    parser.add_argument("--restore-manifest", type=Path)
    parser.add_argument("--restore-sha256")
    parser.add_argument("--operator", default="")
    parser.add_argument("--reason", default="")
    args = parser.parse_args(argv)
    engine = None
    try:
        targets = [Target(int(project_id), name, int(user_id)) for project_id, name, user_id in args.target]
        url = os.environ.get("PRISM_QUARANTINE_DATABASE_URL", "")
        if not url:
            raise QuarantineError("必须显式注入 PRISM_QUARANTINE_DATABASE_URL；工具不加载 .env")
        engine = create_engine(url, echo=False, hide_parameters=True)
        if engine.url.get_backend_name() == "sqlite":
            database = engine.url.database
            if not database or database == ":memory:" or not Path(database).is_file():
                raise QuarantineError("命令行只能连接现存 SQLite 文件，不创建数据库")
        result = run_operation(
            engine, targets=targets, action=args.action, apply=args.apply, manifest_path=args.manifest,
            restore_manifest=args.restore_manifest, restore_sha256=args.restore_sha256,
            backup_file=args.backup_file, backup_sha256=args.backup_sha256,
            operator=args.operator, reason=args.reason,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except QuarantineError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("操作失败，连接信息和底层异常已隐藏；如已生成清单，请先只读核验，勿直接重试", file=sys.stderr)
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
