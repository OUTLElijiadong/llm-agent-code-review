"""只读后端审计复现；全部业务数据仅存在新建的临时 SQLite 中。

运行方式（仓库 backend 目录）：
PYTHONPATH=. .venv311/bin/python ../docs/全系统架构与体验审计20261001/evidence/backend-probe.py

不连接生产数据库、不进行网络请求、不运行模型。Worker 的模型执行入口被
替换为捕获器，正式审查的后台 Thread.start 被替换为空实现，防止付费调用。
代码文件 PUT 使用真实路由、真实 RBAC 和真实全局异常处理。
"""
import importlib.util
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.v1 import code_files
from app.agents.orchestrator import Orchestrator
from app.agents.review_orchestrator_agent import ReviewOrchestratorAgent
from app.core import rate_limit
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.error_handlers import register_handlers
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import code_file_service, project_service, review_service
from app.services.project_member_service import is_project_member
from app.services.rbac_service import check_permission
from app.services.review_input_service import freeze_task_inputs

logger.remove()
# 探针使用独立内存存储，不连接外部Redis；准入逻辑仍走相同 limits backend 接口。
rate_limit.limiter = rate_limit.build_limiter("memory://")
settings.app_env = "test"
settings.redis_url = ""
# 只导入测试模型注册；不调用 fixtures，不运行生产初始化或启动任务。
spec = importlib.util.spec_from_file_location("audit_conftest", "tests/conftest.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

with tempfile.TemporaryDirectory(prefix="prism-backend-readonly-probe-") as temp_dir:
    engine = create_engine(f"sqlite:///{Path(temp_dir) / 'isolated.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    results = []
    cfg = SimpleNamespace(model="local-no-model-call", base_url="https://example.invalid", api_key="local-placeholder", source="system")
    with sessions() as db:
        owner = User(username="probe-owner", password="local-placeholder", role="user", status=1)
        author = User(username="probe-author", password="local-placeholder", role="reviewer", status=1)
        member = User(username="probe-member", password="local-placeholder", role="user", status=1)
        roles = {code: Role(code=code, name=code, status="active", is_builtin=1) for code in ("user", "reviewer")}
        db.add_all([owner, author, member, *roles.values()]); db.flush()
        permissions = {}
        for code in ("report:view", "review:start", "file:edit"):
            permission = Permission(code=code, name=code, module="probe", type="api")
            db.add(permission); db.flush(); permissions[code] = permission.id
            for role in roles.values(): db.add(RolePermission(role_id=role.id, permission_id=permission.id))
        for user in (owner, author, member): db.add(UserRole(user_id=user.id, role_id=roles[user.role].id))
        project = Project(user_id=owner.id, project_name="local isolated probe", status="active", language="python")
        db.add(project); db.flush()
        db.add_all(ProjectMember(project_id=project.id, user_id=user.id, role_in_project="reviewer") for user in (author, member))
        source = CodeFile(project_id=project.id, file_name="probe.py", file_path="probe.py", content="x = 1\n", language="python", status="active", size_bytes=6, raw_size=6, line_count=1, version_no=1, is_binary=0)
        db.add(source); db.flush()
        db.add(CodeVersion(file_id=source.id, version_no=1, content=source.content, create_time=datetime.now(timezone.utc)))
        private = ReviewTask(user_id=author.id, project_id=project.id, task_name="private domain", review_type="pentest", status="success", score=17, total_issues=23)
        running = ReviewTask(user_id=author.id, project_id=project.id, task_name="revocation probe", review_type="quick", status="running", execution_token="probe-lease", rules_snapshot=[], total_files=1)
        db.add_all([private, running]); db.flush(); freeze_task_inputs(db, running.id, [source]); db.commit()
        owner_id, author_id, project_id, file_id, task_id = owner.id, author.id, project.id, source.id, running.id
        for domain in ("pentest", "sandbox_test"):
            private.review_type = domain; db.commit()
            for actor in (member, owner):
                reference = review_service.get_task_detail(db, actor, private.id)
                exposed = next(item for item in project_service.get_project(db, actor, project.id)["recent_tasks"] if item["id"] == private.id)
                list_score = project_service.list_projects(db, actor)["items"][0]["score"]
                results.append({"probe": "private_domain_metrics_secondary_outlet", "actor": actor.username, "type": domain, "direct_task": {key: reference[key] for key in ("can_view_report", "score", "total_issues")}, "project_recent_task": {key: exposed[key] for key in ("score", "total_issues")}, "project_list_score": list_score})

    for revoke_membership, revoke_permission in ((True, False), (False, True), (True, True)):
        with sessions() as db:
            running_task = db.get(ReviewTask, task_id)
            running_task.status = "running"
            running_task.execution_token = "probe-lease"
            running_task.error_message = None
            running_task.coverage = {}
            db.query(ProjectMember).filter_by(project_id=project_id, user_id=author_id).delete()
            db.query(RolePermission).filter_by(role_id=roles["reviewer"].id, permission_id=permissions["review:start"]).delete()
            if not revoke_membership: db.add(ProjectMember(project_id=project_id, user_id=author_id, role_in_project="reviewer"))
            if not revoke_permission: db.add(RolePermission(role_id=roles["reviewer"].id, permission_id=permissions["review:start"]))
            db.commit()
            actor = db.get(User, author_id)
            preconditions = {"is_member": is_project_member(db, project_id, actor)[0], "review_start_permission": check_permission(db, author_id, "review:start")}
        captured = []
        with patch.object(review_service, "SessionLocal", sessions), patch.object(review_service, "DeepSeekAgent", lambda **kw: SimpleNamespace(model="local-no-call")), patch.object(review_service, "_execute_review", lambda *args, **kw: captured.append({"user_id": args[4].id, "file_count": len(args[5]), "snapshot_content": args[5][0].content})), patch("app.services.agent_model_service.resolve_subagent_config", lambda db, config, **kw: config), patch("app.utils.api_resolver.resolve_api_config", lambda *args: cfg), patch("app.services.declarative_agent_runtime.DeclarativeReviewAgentFactory.snapshot_profiles", lambda *args, **kw: ()), patch("app.services.experience_service.retrieve", lambda *args, **kw: []), patch("app.services.personalization_service.build_review_context", lambda *args, **kw: ""), patch.object(review_service, "_enabled_review_profiles", lambda db, profiles: profiles):
            review_service._run_review_task(task_id, author_id, "probe-lease")
        with sessions() as db:
            stopped_task = db.get(ReviewTask, task_id)
            terminal = {
                "task_status": stopped_task.status,
                "coverage_reason": (stopped_task.coverage or {}).get("reason"),
                "error_message": stopped_task.error_message,
            }
        results.append({"probe": "worker_after_authorization_revoked", **preconditions, **terminal, "executor_invocations": captured})

    for sample in range(3):
        a, b = sessions(), sessions()
        try:
            owner_a, owner_b = a.get(User, owner_id), b.get(User, owner_id)
            # 对应真实 helper 的 read CodeFile -> acquire Project lock 顺序。
            # 持有强引用，确保第二次 db.get 使用同一个未 refresh 的 ORM 实例。
            stale = b.get(CodeFile, file_id)
            cached_version = stale.version_no
            committed_version = code_file_service.update_content(
                a,
                owner_a,
                file_id,
                f"a = {sample + 2}\n",
                expected_version=cached_version,
            )
            app = FastAPI()
            register_handlers(app); app.include_router(code_files.router, prefix="/api/code-files")
            app.dependency_overrides[get_db] = lambda: b
            app.dependency_overrides[get_current_user] = lambda: owner_b
            with patch.object(settings, "app_env", "prod"), TestClient(app, raise_server_exceptions=False) as client:
                response = client.put(f"/api/code-files/{file_id}", json={"content": f"b = {sample + 3}\n", "expected_version": cached_version})
            results.append({"probe": "stale_file_before_project_lock_http", "sample": sample + 1, "cached_version": cached_version, "committed_version": committed_version, "http_status": response.status_code, "error_code": response.json().get("code"), "message": response.json().get("message")})
            missing_version = client.put(
                f"/api/code-files/{file_id}",
                json={"content": f"legacy-client = {sample + 4}\n"},
            )
            results.append({
                "probe": "save_without_expected_version_http",
                "sample": sample + 1,
                "http_status": missing_version.status_code,
                "error_code": missing_version.json().get("code"),
                "message": missing_version.json().get("message"),
            })
            b.rollback()
        finally:
            a.close(); b.close()

    with sessions() as db:
        owner = db.get(User, owner_id)
        # 真实小菱固定工具 Orchestrator.start_review，省去只负责元数据构造的初始化。
        orchestrator = object.__new__(Orchestrator)
        orchestrator._db, orchestrator._user = db, owner
        orchestrator.review_orch = ReviewOrchestratorAgent(); orchestrator.review_orch.inject(db, user=owner)
        # 替换 service 模块的 threading 引用，不能改全局 threading.Thread：
        # Limits 的共享存储会用 threading.Timer 清理窗口，改全局类会破坏限流探针。
        with patch.object(review_service, "threading", SimpleNamespace(Thread=lambda **kw: SimpleNamespace(start=lambda: None))), patch.object(review_service, "DeepSeekAgent", lambda **kw: SimpleNamespace(model="local-no-call")), patch("app.services.agent_model_service.resolve_subagent_config", lambda db, config, **kw: config), patch("app.utils.api_resolver.resolve_api_config", lambda *args: cfg):
            tool_calls = [orchestrator.start_review(project_id, [file_id], "quick") for _ in range(6)]
        results.append({"probe": "xiaoling_tool_review_admission", "calls_same_account_same_minute": len(tool_calls), "accepted": sum(bool(item.success) for item in tool_calls), "errors": [item.error for item in tool_calls if not item.success], "background_execution": "disabled; zero model calls"})
    engine.dispose()
    print(json.dumps(results, ensure_ascii=False, indent=2))
