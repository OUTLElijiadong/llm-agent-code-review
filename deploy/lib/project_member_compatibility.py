"""无网检验目标镜像能否安全读取只读成员；不导入应用或连接数据库。

提取实际项目角色判断和权限门，用固定本地记录验证读、写、执行语义。
发布版本或函数名称不能代替行为；未知源码和异常均拒绝切换镜像。
"""
from __future__ import annotations

import ast
import pathlib
import signal
import sys
import types


def _namespace(root: pathlib.Path) -> dict:
    tree = ast.parse((root / "services/project_member_service.py").read_text(encoding="utf-8"))
    names = {"_require_visible_project", "is_project_member", "require_project_access", "require_project_execution"}
    definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in definitions} != names:
        raise ValueError("required access contract unavailable")
    namespace = {"__name__": "_prism_member_contract_probe"}
    for name in ("HIDDEN_PROJECT_STATUSES", "PROJECT_MEMBER_ROLES", "PROJECT_WRITE_ROLES", "PROJECT_EXECUTION_ROLES"):
        node = next(item for item in tree.body if isinstance(item, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == name for target in item.targets))
        value = node.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "frozenset":
            if len(value.args) != 1 or value.keywords:
                raise ValueError("unknown role constant")
            namespace[name] = frozenset(ast.literal_eval(value.args[0]))
        else:
            namespace[name] = ast.literal_eval(value)
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *definitions,
    ], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<target-image-member-access>", "exec"), namespace)
    return namespace


def _probe(root: pathlib.Path) -> bool:
    namespace = _namespace(root)

    class Column:
        def __eq__(self, value):
            return True

    class Denied(Exception):
        def __init__(self, *args, **kwargs):
            super().__init__(*args)

    class Missing(Denied):
        pass

    project_model = type("Project", (), {"id": Column()})
    member_model = type("ProjectMember", (), {name: Column() for name in ("project_id", "user_id")})
    actor = types.SimpleNamespace(id=17)
    state = {"admin": False, "owner": False, "role": "viewer", "status": "active", "exists": True}

    class Query:
        def __init__(self, model):
            self.model = model

        def populate_existing(self):
            return self

        def filter(self, *conditions):
            return self

        def first(self):
            if self.model is project_model:
                if not state["exists"]:
                    return None
                return types.SimpleNamespace(user_id=actor.id if state["owner"] else 29, status=state["status"])
            return types.SimpleNamespace(role_in_project=state["role"]) if state["role"] else None

        one_or_none = first

    database = types.SimpleNamespace(query=lambda model: Query(model))
    namespace.update({"Project": project_model, "ProjectMember": member_model,
                      "NotFoundError": Missing, "ForbiddenError": Denied,
                      "is_admin_user": lambda db, user_id: state["admin"],
                      "_audit_project_access": lambda *args, **kwargs: None})

    def allowed(fn, expected, **kwargs):
        try:
            return namespace[fn](database, 31, actor, **kwargs) == expected
        except Denied:
            return False

    def denied(fn, **kwargs):
        try:
            namespace[fn](database, 31, actor, **kwargs)
        except Denied:
            return True
        return False

    # 必须同时保留合法角色的能力，否则“全部拒绝”也不能称兼容。
    for role, writable, executable in (("viewer", False, False), ("reviewer", False, True), ("owner", True, True)):
        state.update(role=role, owner=role == "owner", admin=False)
        if not allowed("require_project_access", role):
            return False
        write_result = (allowed("require_project_access", role, need_write=True) if writable
                        else denied("require_project_access", need_write=True))
        if not write_result:
            return False
        if not (allowed("require_project_execution", role) if executable else denied("require_project_execution")):
            return False
    state.update(role="viewer", owner=False, admin=True)
    if not all((allowed("require_project_access", "admin", need_write=True),
                allowed("require_project_execution", "admin"))):
        return False
    for role in ("unknown-role", ""):
        state.update(role=role, admin=False)
        if namespace["is_project_member"](database, 31, actor) != (False, ""):
            return False
        if not all((denied("require_project_access"), denied("require_project_access", need_write=True),
                    denied("require_project_execution"))):
            return False
    for status in ("deleted", "quarantined"):
        state.update(role="viewer", admin=True, status=status)
        if not all((denied("require_project_access"), denied("require_project_execution"))):
            return False
    state.update(status="active", exists=False)
    return denied("require_project_access") and denied("require_project_execution")


def main() -> int:
    def expired(signum, frame):
        raise TimeoutError("member capability probe deadline")

    signal.signal(signal.SIGALRM, expired)
    signal.alarm(15)
    try:
        root = pathlib.Path(sys.argv[1]) if len(sys.argv) == 2 else pathlib.Path("/app/app")
        print("supported" if _probe(root) else "unsupported")
        return 0
    except (StopIteration, ValueError):
        print("unsupported")
        return 0
    except Exception as exc:
        print(f"member probe failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
