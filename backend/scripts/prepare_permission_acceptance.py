"""准备来源明确的最小权限验收账号；默认只读计划，凭据仅写入 0600 文件。

不创建成功任务、报告或模型用量，不修改真实账号/历史任务，不启动后台任务。
执行完成后可用同一清单 --disable 禁用专用账号并撤销会话，保留审计追溯。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import SessionLocal  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models.rbac import Permission, Role, RolePermission, UserRole  # noqa: E402
from app.models.user import User  # noqa: E402

REQUIRED_PERMISSIONS = {
    "project:view", "project:create", "project:update", "project:delete", "project:member:manage",
    "file:view", "file:upload", "file:edit", "file:delete",
    "review:view", "issue:view", "report:view",
}
FORBIDDEN_ELEVATED_PERMISSIONS = {
    "agent_asset:approve", "agent_asset:publish", "agent_asset:disable", "agent_asset:rollback",
    "pentest:authorize", "pentest:manage", "role:manage", "menu:manage",
    "user:create", "user:update", "user:delete",
    "server_ops:view", "server_ops:execute", "server_ops:critical",
}
ACCOUNTS = ("owner_a", "member_a", "owner_b", "no_permission")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--marker", default="20260907")
    parser.add_argument("--credentials", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--disable", action="store_true")
    args = parser.parse_args()
    if not args.marker.isalnum() or len(args.marker) > 16:
        parser.error("marker 只能是最多16位的字母数字")
    role_code = "user"
    names = {account: f"qa_{args.marker}_{account}" for account in ACCOUNTS}
    with SessionLocal() as db:
        if args.disable:
            manifest = json.loads(args.credentials.read_text())
            if (
                manifest.get("marker") != args.marker
                or manifest.get("role_code") != role_code
                or manifest.get("role_origin") != "existing_builtin"
            ):
                raise ValueError("清单标记不匹配")
            rows = []
            for account, expected_name in names.items():
                item = manifest["accounts"][account]
                user = db.query(User).filter_by(id=item["id"]).with_for_update().one()
                if item["username"] != expected_name or user.username != expected_name or user.role != role_code:
                    raise ValueError("账号身份发生变化，拒绝自动清理")
                assigned_role_ids = {
                    value for value, in db.query(UserRole.role_id).filter(UserRole.user_id == user.id).all()
                }
                expected_role_ids = set() if account == "no_permission" else {manifest["role_id"]}
                if assigned_role_ids != expected_role_ids:
                    raise ValueError("账号角色绑定发生变化，拒绝自动清理")
                rows.append(user)
            for user in rows:
                user.status = 0
                user.token_version = int(user.token_version or 0) + 1
            db.commit()
            print(json.dumps({"status": "disabled", "account_ids": [user.id for user in rows]}))
            return

        role = db.query(Role).filter_by(code=role_code, status="active", is_builtin=1).one_or_none()
        if role is None:
            raise ValueError("缺少启用的内置普通用户角色，未写库")
        granted = {
            code for code, in db.query(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .filter(RolePermission.role_id == role.id)
            .all()
        }
        missing = sorted(REQUIRED_PERMISSIONS - granted)
        if missing:
            raise ValueError(f"普通用户角色缺少验收所需权限，未写库：{missing}")
        elevated = sorted(FORBIDDEN_ELEVATED_PERMISSIONS & granted)
        if elevated:
            raise ValueError(f"普通用户角色含越权权限，未写库：{elevated}")
        if db.query(User).filter(User.username.in_(names.values())).count():
            raise ValueError("专用账号已存在；拒绝覆盖，使用原清单继续或更换明确标记")
        plan = {"status": "dry_run", "marker": args.marker, "role_code": role_code,
                "role_origin": "existing_builtin", "role_id": role.id,
                "account_names": names, "required_permissions": sorted(REQUIRED_PERMISSIONS)}
        if not args.apply:
            print(json.dumps(plan, ensure_ascii=False))
            return
        args.credentials.parent.mkdir(parents=True, exist_ok=True)
        # 在数据库写入前拒绝覆盖已有凭据；保留唯一文件以便失联时可核账。
        descriptor = os.open(args.credentials, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            manifest = {
                "marker": args.marker,
                "role_code": role_code,
                "role_id": role.id,
                "role_origin": "existing_builtin",
                "accounts": {},
            }
            for account, username in names.items():
                password = secrets.token_urlsafe(24)
                user = User(username=username, password=hash_password(password), role="user", status=1,
                            nickname=f"验收专用-{account}")
                db.add(user)
                db.flush()
                if account != "no_permission":
                    db.add(UserRole(user_id=user.id, role_id=role.id))
                manifest["accounts"][account] = {"id": user.id, "username": username, "password": password}
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
            db.commit()
        print(json.dumps({"status": "created", "referenced_role_id": role.id,
                          "account_ids": {name: item["id"] for name, item in manifest["accounts"].items()}}))


if __name__ == "__main__":
    main()
