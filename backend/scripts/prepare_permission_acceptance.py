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

PERMISSIONS = {
    "project:view", "project:create", "project:update", "project:delete", "project:member:manage",
    "file:view", "file:upload", "file:edit", "file:delete", "file:download",
    "review:view", "review:cancel", "issue:view", "issue:handle", "issue:batch",
    "report:view", "report:export:json", "report:export:html", "security:view", "agent:view",
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
    role_code = f"qa_permission_{args.marker}"
    names = {account: f"qa_{args.marker}_{account}" for account in ACCOUNTS}
    with SessionLocal() as db:
        if args.disable:
            manifest = json.loads(args.credentials.read_text())
            if manifest.get("marker") != args.marker or manifest.get("role_code") != role_code:
                raise ValueError("清单标记不匹配")
            rows = []
            for account, expected_name in names.items():
                item = manifest["accounts"][account]
                user = db.query(User).filter_by(id=item["id"]).with_for_update().one()
                if item["username"] != expected_name or user.username != expected_name or user.role != "user":
                    raise ValueError("账号身份发生变化，拒绝自动清理")
                rows.append(user)
            for user in rows:
                user.status = 0
                user.token_version = int(user.token_version or 0) + 1
            db.commit()
            print(json.dumps({"status": "disabled", "account_ids": [user.id for user in rows]}))
            return

        permissions = db.query(Permission).filter(Permission.code.in_(PERMISSIONS)).all()
        missing = sorted(PERMISSIONS - {item.code for item in permissions})
        if missing:
            raise ValueError(f"缺少权限点，未写库：{missing}")
        if (
            db.query(User).filter(User.username.in_(names.values())).count()
            or db.query(Role).filter_by(code=role_code).count()
        ):
            raise ValueError("专用账号或角色已存在；拒绝覆盖，使用原清单继续或更换明确标记")
        plan = {"status": "dry_run", "marker": args.marker, "role_code": role_code,
                "account_names": names, "permissions": sorted(PERMISSIONS)}
        if not args.apply:
            print(json.dumps(plan, ensure_ascii=False))
            return
        args.credentials.parent.mkdir(parents=True, exist_ok=True)
        # 在数据库写入前拒绝覆盖已有凭据；保留唯一文件以便失联时可核账。
        descriptor = os.open(args.credentials, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            role = Role(name=f"专用权限验收{args.marker}", code=role_code, status="active", is_builtin=0,
                        description="来源明确的权限验收专用角色，不含管理员、模型执行或运维权限")
            db.add(role)
            db.flush()
            db.add_all([RolePermission(role_id=role.id, permission_id=item.id) for item in permissions])
            manifest = {"marker": args.marker, "role_code": role_code, "role_id": role.id, "accounts": {}}
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
        print(json.dumps({"status": "created", "role_id": role.id,
                          "account_ids": {name: item["id"] for name, item in manifest["accounts"].items()}}))


if __name__ == "__main__":
    main()
