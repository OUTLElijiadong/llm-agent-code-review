"""Disabled transaction experiment against real config rows; never a production gate.

Only participating writers are fenced. Existing writers bypass this experiment;
an independent authorization, migration, old-writer isolation and all send-path
integration are mandatory before any operational use. The outer transaction belongs
to the caller; no function commits or rolls it back.
"""

import hashlib
import json
import secrets
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.api_config import UserApiConfig
from app.models.credential_policy_prototype import CredentialPolicyPrototype
from app.models.system_config import SystemConfig

_EXPERIMENT_ENABLED = False
_MAX_REVISION = 2**63 - 1


class PolicyError(RuntimeError):
    pass


class PolicyConflict(PolicyError):
    pass


@dataclass(frozen=True)
class Snapshot:
    kind: str
    owner_id: int
    state: str
    revision: int
    incarnation: bytes


def _ready(db: Session, kind: str, owner_id: int) -> None:
    if not _EXPERIMENT_ENABLED:
        raise PolicyError("凭据策略实验尚未启用")
    # This rejects pending ORM changes only. A prior flush or direct SQL can
    # already have modified the outer transaction; we never commit that work.
    if db.new or db.dirty or db.deleted:
        raise PolicyError("调用者存在未提交的 ORM 修改")
    # An existing caller SAVEPOINT can itself be SQLite's outermost physical
    # transaction. Refuse it rather than infer safe outer-transaction ownership.
    if db.in_nested_transaction():
        raise PolicyError("不支持调用者嵌套事务")
    if (kind not in {"user", "global"} or type(owner_id) is not int
            or (kind == "user" and owner_id <= 0) or (kind == "global" and owner_id != 0)):
        raise PolicyError("配置主体无效")
    # SQLite SELECT does not start a physical transaction under legacy driver
    # mode, and releasing the first SAVEPOINT could otherwise commit it. This
    # experiment allows only one operation per SQLite physical transaction;
    # any existing physical transaction is rejected and left untouched.
    connection = db.connection()
    if connection.dialect.name == "sqlite":
        if connection.connection.driver_connection.in_transaction:
            raise PolicyError("SQLite 调用者已有物理事务，实验拒绝接管")
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def _row(db: Session, kind: str, owner_id: int):
    if kind == "user":
        return (db.query(UserApiConfig).filter_by(user_id=owner_id)
                .populate_existing().with_for_update().one_or_none())
    return (db.query(SystemConfig).filter_by(config_key="llm_provider")
            .populate_existing().with_for_update().one_or_none())


def _global_data(row) -> dict:
    try:
        data = json.loads(row.config_value)
    except (TypeError, ValueError) as exc:
        raise PolicyError("全局配置格式未知") from exc
    if not isinstance(data, dict):
        raise PolicyError("全局配置格式未知")
    return data


def _digest(kind: str, row) -> bytes:
    if row is None:
        value = None
    elif kind == "user":
        value = [row.id, row.user_id, row.provider, row.base_url, row.model, bool(row.is_active), row.api_key_enc]
    else:
        value = [row.id, row.config_key, row.config_value]
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).digest()


def _current(db: Session, kind: str, owner_id: int):
    record = (db.query(CredentialPolicyPrototype).filter_by(kind=kind, owner_id=owner_id)
              .populate_existing().with_for_update().one_or_none())
    if record is None:
        raise PolicyError("凭据策略尚未初始化")
    config = _row(db, kind, owner_id)
    if not secrets.compare_digest(bytes(record.config_digest), _digest(kind, config)):
        raise PolicyConflict("实际配置与策略版本不一致")
    return record, config


def _snapshot(record) -> Snapshot:
    return Snapshot(record.kind, record.owner_id, record.state, record.revision, bytes(record.incarnation))


def initialize(db: Session, *, kind: str, owner_id: int) -> Snapshot:
    _ready(db, kind, owner_id)
    with db.no_autoflush:
        # Match the policy -> configuration lock order used by mutation paths.
        existing = (db.query(CredentialPolicyPrototype).filter_by(kind=kind, owner_id=owner_id)
                    .populate_existing().with_for_update().one_or_none())
        if existing is not None:
            raise PolicyConflict("凭据策略已存在")
        config = _row(db, kind, owner_id)
        if config is None:
            state = "system"
        elif kind == "user":
            state = "active" if config.is_active else "unknown"
        else:
            state = "active" if _global_data(config).get("active") is True else "unknown"
        record = CredentialPolicyPrototype(kind=kind, owner_id=owner_id, state=state, revision=1,
                                           incarnation=secrets.token_bytes(16), config_digest=_digest(kind, config))
        try:
            with db.begin_nested():
                db.add(record)
                db.flush()
        except IntegrityError as exc:
            raise PolicyConflict("凭据策略已存在或主体无效") from exc
        return _snapshot(record)


def read(db: Session, *, kind: str, owner_id: int) -> Snapshot:
    _ready(db, kind, owner_id)
    with db.no_autoflush:
        record, _ = _current(db, kind, owner_id)
        return _snapshot(record)


def _apply(db: Session, expected: Snapshot, action: str, *, ciphertext=None, user_config=None) -> Snapshot:
    if not isinstance(expected, Snapshot):
        raise PolicyError("凭据版本无效")
    _ready(db, expected.kind, expected.owner_id)
    with db.no_autoflush:
        record, config = _current(db, expected.kind, expected.owner_id)
        if (_snapshot(record) != expected or record.revision >= _MAX_REVISION):
            raise PolicyConflict("凭据版本已变更")
        if action in {"fail", "rotate"} and (record.state != "active" or config is None):
            raise PolicyConflict("当前配置不允许此操作")
        if action in {"replace", "rotate"} and (not isinstance(ciphertext, str) or not ciphertext):
            raise PolicyError("密文无效")
        if user_config is not None and (action != "replace" or expected.kind != "user" or config is not None):
            raise PolicyError("已有配置仅支持更换密钥，不支持更新其他配置字段")
        with db.begin_nested():
            if action == "system":
                if expected.kind == "user" and config is not None:
                    db.delete(config)
                    config = None
                elif config is not None:
                    data = _global_data(config)
                    data["active"] = False
                    config.config_value = json.dumps(data, ensure_ascii=False)
                record.state = "system"
                record.incarnation = secrets.token_bytes(16)
            elif action == "fail":
                if expected.kind == "user":
                    config.is_active = False
                else:
                    data = _global_data(config)
                    data["active"] = False
                    config.config_value = json.dumps(data, ensure_ascii=False)
                record.state = "failed"
            elif action in {"replace", "rotate"}:
                if expected.kind == "user":
                    if config is None:
                        keys = {"provider", "base_url", "model"}
                        if (not isinstance(user_config, dict) or set(user_config) != keys
                                or any(not isinstance(user_config[k], str) or not user_config[k] for k in keys)):
                            raise PolicyError("新用户配置无效")
                        config = UserApiConfig(user_id=expected.owner_id, is_active=True,
                                               api_key_enc=ciphertext, **user_config)
                        db.add(config)
                        record.incarnation = secrets.token_bytes(16)
                    else:
                        config.api_key_enc = ciphertext
                        config.is_active = True
                else:
                    if config is None:
                        raise PolicyError("全局配置尚未建立")
                    data = _global_data(config)
                    data.update(api_key_enc=ciphertext, active=True)
                    config.config_value = json.dumps(data, ensure_ascii=False)
                record.state = "active"
            else:
                raise PolicyError("未知操作")
            db.flush()
            record.config_digest = _digest(expected.kind, config)
            record.revision += 1
            db.flush()
        return _snapshot(record)


def mark_failed(db: Session, expected: Snapshot) -> Snapshot:
    return _apply(db, expected, "fail")


def rotate_key(db: Session, expected: Snapshot, *, ciphertext: str) -> Snapshot:
    return _apply(db, expected, "rotate", ciphertext=ciphertext)


def replace_key(db: Session, expected: Snapshot, *, ciphertext: str, user_config=None) -> Snapshot:
    return _apply(db, expected, "replace", ciphertext=ciphertext, user_config=user_config)


def save_current_user_configuration(
    db: Session, *, owner_id: int, ciphertext: str, provider: str, base_url: str, model: str,
):
    """Save the real user row and version together; caller owns the transaction.

    The caller must validate URL and encrypt the key before entering this short
    transaction. This experiment is not a complete writer or send fence.
    """
    _ready(db, "user", owner_id)
    fields = ((ciphertext, 512), (provider, 32), (base_url, 512), (model, 128))
    if any(not isinstance(value, str) or not value or len(value) > limit for value, limit in fields):
        raise PolicyError("用户配置无效")
    with db.no_autoflush:
        record, config = _current(db, "user", owner_id)
        if record.revision >= _MAX_REVISION:
            raise PolicyConflict("凭据版本已耗尽")
        with db.begin_nested():
            if config is None:
                config = UserApiConfig(user_id=owner_id)
                db.add(config)
                record.incarnation = secrets.token_bytes(16)
            config.provider = provider
            config.base_url = base_url
            config.model = model
            config.api_key_enc = ciphertext
            config.is_active = True
            record.state = "active"
            db.flush()
            record.config_digest = _digest("user", config)
            record.revision += 1
            db.flush()
        return _snapshot(record), config


def choose_system(db: Session, expected: Snapshot) -> Snapshot:
    return _apply(db, expected, "system")


def require_paid_authorization(_snapshot_value: Snapshot) -> None:
    raise PolicyError("实验不能授权扣费")
