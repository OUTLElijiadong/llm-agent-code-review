"""Orchestrator 依赖注入、包装器、工具路由与实例生命周期覆盖测试。"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import app.agents.orchestrator as orchestrator_module
from app.agents.base import AgentContext, AgentResult
from app.agents.orchestrator import Orchestrator
from app.agents.skills.registry import SkillRegistry
from app.models.agent_governance import AgentProfile
from app.models.code_file import CodeFile
from app.services import skill_service
from app.utils.api_resolver import ApiConfig


class FakeRegistry:
    """为 Orchestrator 提供可控的 Agent 与 Skill 注册数据。"""

    skills: list[Any] = []
    meta: list[dict[str, Any]] = []

    @classmethod
    def instance(cls) -> "FakeRegistry":
        """返回当前假注册中心实例。"""
        return cls()

    def list_all(self) -> list[Any]:
        """返回预置 Skill 实例。"""
        return type(self).skills

    def list_meta(self, agent_name: str) -> list[dict[str, Any]]:
        """返回预置 Skill 元数据。"""
        return [dict(item, requested_agent=agent_name) for item in type(self).meta]


class ConstructedOrchestrator:
    """记录单例/请求级辅助函数的构造与注入。"""

    created: list["ConstructedOrchestrator"] = []

    def __init__(self, register: bool = True):
        """保存 register 参数并记录实例。"""
        self.register = register
        self.injected: tuple[Any, Any] | None = None
        type(self).created.append(self)

    def inject_db(self, db: Any, user: Any = None) -> None:
        """记录请求级数据库与用户。"""
        self.injected = (db, user)


def _bare_orchestrator() -> Orchestrator:
    """绕过重量初始化构造可按测试填充字段的 Orchestrator。"""
    orch = Orchestrator.__new__(Orchestrator)
    orch._api_config = None
    orch._db = None
    orch._user = None
    orch._registry = SimpleNamespace(list=MagicMock(return_value={"a": "A"}), get=MagicMock(return_value="agent"))
    orch.test_verifier = SimpleNamespace(inject=MagicMock())
    orch.sandbox_deployer = SimpleNamespace(inject=MagicMock())
    return orch


def _result(label: str = "ok") -> AgentResult:
    """构造带标签的成功 AgentResult。"""
    return AgentResult(success=True, data=label)


def _add_code_file(
    db: Any,
    *,
    project_id: int,
    file_name: str,
    status: str = "active",
) -> CodeFile:
    """向隔离数据库写入一个最小代码文件。

    Args:
        db: SQLAlchemy 测试会话。
        project_id: 文件所属项目 ID。
        file_name: 文件名。
        status: 文件状态，默认 active。

    Returns:
        CodeFile: 已提交并刷新主键的代码文件。
    """
    row = CodeFile(
        project_id=project_id,
        file_name=file_name,
        file_path=file_name,
        language="python",
        content="print('ok')\n",
        size_bytes=12,
        line_count=1,
        status=status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_set_and_get_api_config_updates_chat_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """自定义配置应写入 ChatAgent，传 None 时应恢复系统默认。"""
    orch = _bare_orchestrator()
    orch.chat_agent = SimpleNamespace(_base_url="", _api_key="", _model="")
    custom = ApiConfig(
        api_key="secret-test-key",
        base_url="https://example.test/v1/",
        model="custom-model",
        source="user",
    )

    orch.set_api_config(custom)

    assert orch.get_api_config() is custom
    assert orch.chat_agent._base_url == "https://example.test/v1"
    assert orch.chat_agent._api_key == "secret-test-key"
    assert orch.chat_agent._model == "custom-model"

    fake_settings = SimpleNamespace(
        deepseek_base_url="https://default.example/v1/",
        deepseek_api_key="default-key",
        deepseek_model="default-model",
        deepseek_orchestrator_model="default-orchestrator-model",
    )
    monkeypatch.setattr("app.core.config.settings", fake_settings)
    orch.set_api_config(None)

    assert orch.chat_agent._base_url == "https://default.example/v1"
    assert orch.chat_agent._api_key == "default-key"
    # 传 None 恢复系统默认时，小菱人格应恢复 orchestrator pro，而不是子 Agent 的 flash 默认。
    assert orch.chat_agent._model == "default-orchestrator-model"


def test_inject_db_requires_user_and_injects_all_operation_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    """请求级注入应拒绝空用户，并向全部操作 Agent 传播 DB/用户与 API 配置。"""
    orch = _bare_orchestrator()
    delegates = [SimpleNamespace(inject=MagicMock()) for _ in range(9)]
    (
        orch.project_mgr,
        orch.review_orch,
        orch.file_mgr,
        orch.dashboard_agent,
        orch.rule_mgr,
        orch.reporter,
        orch.ai_prompt,
        orch.security_sentinel,
        orch.evolution_agent,
    ) = delegates
    delegates.extend((orch.test_verifier, orch.sandbox_deployer))
    orch.chat_agent = SimpleNamespace(_base_url="", _api_key="", _model="")
    db = object()
    user = SimpleNamespace(id=42)
    config = ApiConfig("key", "https://api.example/v1", "model", source="user")

    def _resolve_config(db_obj: Any, user_id: int) -> ApiConfig:
        """返回固定用户 API 配置。"""
        assert db_obj is db
        assert user_id == user.id
        return config

    monkeypatch.setattr(orchestrator_module, "resolve_api_config", _resolve_config)

    with pytest.raises(ValueError, match="真实用户"):
        orch.inject_db(db, user=None)

    orch.inject_db(db, user=user)

    assert all(delegate.inject.call_args.args == (db,) for delegate in delegates)
    assert all(delegate.inject.call_args.kwargs == {"user": user} for delegate in delegates)
    assert orch._db is db
    assert orch._user is user
    assert orch.get_api_config() is config


def test_agent_wrapper_methods_forward_arguments_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """所有固定业务包装器应把参数转发给正确的专业 Agent。"""
    orch = _bare_orchestrator()
    ctx = AgentContext(user_id=1)
    user = SimpleNamespace(id=1)
    orch._db = object()
    orch._user = user
    monkeypatch.setattr(orchestrator_module, "check_permission", lambda *_args, **_kwargs: True)
    orch.lang_agent = SimpleNamespace(execute=MagicMock(return_value=_result("language")))
    orch.project_agent = SimpleNamespace(execute=MagicMock(return_value=_result("project")))
    orch.code_reviewer = SimpleNamespace(execute=MagicMock(return_value=_result("review")))
    orch.project_mgr = SimpleNamespace(
        create_project=MagicMock(return_value=_result("create")),
        list_projects=MagicMock(return_value=_result("projects")),
        delete_project=MagicMock(return_value=_result("delete")),
    )
    orch.review_orch = SimpleNamespace(
        start_review=MagicMock(return_value=_result("start")),
        list_tasks=MagicMock(return_value=_result("tasks")),
        list_issues=MagicMock(return_value=_result("issues")),
    )
    orch.file_mgr = SimpleNamespace(list_files=MagicMock(return_value=_result("files")))
    orch.dashboard_agent = SimpleNamespace(summary=MagicMock(return_value=_result("dashboard")))
    orch.rule_mgr = SimpleNamespace(list_rules=MagicMock(return_value=_result("rules")))
    orch.reporter = SimpleNamespace(list_reports=MagicMock(return_value=_result("reports")))
    orch.ai_prompt = SimpleNamespace(
        execute_for_issue=MagicMock(return_value=_result("prompt-issue")),
        execute_for_task=MagicMock(return_value=_result("prompt-task")),
        execute_for_project=MagicMock(return_value=_result("prompt-project")),
    )
    orch.security_sentinel = SimpleNamespace(
        scan_file=MagicMock(return_value=_result("security-file")),
        scan_task=MagicMock(return_value=_result("security-task")),
        scan_project=MagicMock(return_value=_result("security-project")),
    )

    assert orch.detect_language("a.py", ctx=ctx).data == "language"
    assert orch.analyze_project(["a.py"], ctx=ctx).data == "project"
    assert orch.review_code("code", ctx=ctx).data == "review"
    assert orch.create_project("demo", ctx=ctx).data == "create"
    assert orch.list_projects(page=2, ctx=ctx).data == "projects"
    assert orch.delete_project(3, ctx=ctx).data == "delete"
    assert orch.start_review(3, [4], "security", "task", user, ctx).data == "start"
    assert orch.list_review_tasks(project_id=3, ctx=ctx).data == "tasks"
    assert orch.list_review_issues(task_id=5, ctx=ctx).data == "issues"
    assert orch.list_code_files(project_id=3, ctx=ctx).data == "files"
    assert orch.dashboard_summary(ctx=ctx).data == "dashboard"
    assert orch.list_rules(ctx=ctx).data == "rules"
    assert orch.list_reports(ctx=ctx).data == "reports"
    assert orch.generate_ai_prompt_for_issue(6, "cursor", False, ctx).data == "prompt-issue"
    assert orch.generate_ai_prompt_for_task(7, "generic", ["高"], False, ctx).data == "prompt-task"
    assert orch.generate_ai_prompt_for_project(8, "generic", 10, False, ctx).data == "prompt-project"
    assert orch.audit_security_for_file(9, "deep", ctx).data == "security-file"
    assert orch.audit_security_for_task(10, ctx).data == "security-task"
    assert orch.audit_security_for_project(11, 20, False, ctx).data == "security-project"
    assert "ctx" not in orch.lang_agent.execute.call_args.kwargs
    assert "ctx" not in orch.project_agent.execute.call_args.kwargs
    assert "ctx" not in orch.code_reviewer.execute.call_args.kwargs


def test_run_full_project_validation_forces_combined_mode_and_forwards_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上传后的组合验证必须固定为 combined，并保留源版本追踪信息。"""
    orch = _bare_orchestrator()
    orch._db = object()
    orch._user = SimpleNamespace(id=17)
    ctx = AgentContext(user_id=17, extra={"trace_id": "trace-upload-1"})
    verifier = MagicMock(run_project_tests=MagicMock(return_value=_result("validated")))
    orch.test_verifier = verifier
    monkeypatch.setattr(orchestrator_module, "check_permission", lambda *_args, **_kwargs: True)

    result = orch.run_full_project_validation(
        project_id=7,
        language="python",
        source_revision_id=3,
        ctx=ctx,
    )

    assert result.success is True
    assert result.data == "validated"
    verifier.run_project_tests.assert_called_once_with(
        project_id=7,
        language="python",
        test_mode="combined",
        worker_code="",
        source_revision_id=3,
        ctx=ctx,
    )


def test_run_full_project_validation_denies_without_project_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通用户无项目查看权限时不得创建或运行沙箱。"""
    orch = _bare_orchestrator()
    orch._db = object()
    orch._user = SimpleNamespace(id=17)
    verifier = MagicMock(run_project_tests=MagicMock(return_value=_result("must-not-run")))
    orch.test_verifier = verifier
    monkeypatch.setattr(orchestrator_module, "check_permission", lambda *_args, **_kwargs: False)

    result = orch.run_full_project_validation(project_id=7, language="python")

    assert result.success is False
    assert "project:view" in (result.error or "")
    verifier.run_project_tests.assert_not_called()


def test_archive_agent_wrappers_forward_static_full_and_remote_audit_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent 调度不得丢失整包扫描模式或隔离导入标志。"""
    orch = _bare_orchestrator()
    orch._db = object()
    orch._user = SimpleNamespace(id=17, role="user")
    monkeypatch.setattr(orchestrator_module, "check_permission", lambda *_args, **_kwargs: True)
    orch.security_sentinel = SimpleNamespace(scan_project=MagicMock(return_value=_result("audit")))

    result = orch.audit_security_for_project(11)

    assert result.success is True
    orch.security_sentinel.scan_project.assert_called_once_with(
        11,
        50,
        True,
        None,
        "static_full",
    )

    import_remote = MagicMock(return_value={"id": 91, "source_mode": "quarantined_archive"})
    monkeypatch.setattr(orchestrator_module.project_source_service, "import_remote_project", import_remote)

    imported = orch.import_remote_project(
        "https://example.test/source.zip",
        "remote-audit",
        description="whole archive",
        language="php",
        audit_mode=True,
    )

    assert imported.success is True
    import_remote.assert_called_once_with(
        orch._db,
        orch._user,
        url="https://example.test/source.zip",
        project_name="remote-audit",
        description="whole archive",
        language="php",
        audit_mode=True,
    )


def test_disabled_agent_blocks_fixed_runtime_before_delegate(db: Any) -> None:
    """治理画像停用后，固定工具必须在调用专业 Agent 前失败。"""
    db.add(AgentProfile(
        code="code_reviewer",
        name="代码审查 Agent",
        category="quality",
        status="disabled",
        is_enabled=0,
    ))
    db.commit()
    orch = _bare_orchestrator()
    orch._db = db
    execute = MagicMock(return_value=_result("unexpected"))
    orch.code_reviewer = SimpleNamespace(execute=execute)

    result = orch.review_code("print(1)", language="python")

    assert result.success is False
    assert "已停用" in (result.error or "")
    execute.assert_not_called()


def test_start_review_auto_selects_only_active_project_files(db: Any) -> None:
    """统一入口应在 file_ids 为空时按 ID 选择同项目 active 文件。

    Args:
        db: 内存 SQLite 会话。

    Returns:
        None: 断言 deleted 和其他项目文件均被排除。
    """
    first = _add_code_file(db, project_id=7, file_name="a.py")
    _add_code_file(db, project_id=7, file_name="deleted.py", status="deleted")
    second = _add_code_file(db, project_id=7, file_name="b.py")
    _add_code_file(db, project_id=8, file_name="other.py")
    orch = _bare_orchestrator()
    orch._db = db
    orch.review_orch = SimpleNamespace(start_review=MagicMock(return_value=_result("started")))
    ctx = AgentContext(user_id=3)

    result = orch.start_review(project_id=7, file_ids=[], review_type="standard", ctx=ctx)

    assert result.success is True
    orch.review_orch.start_review.assert_called_once_with(
        7, [first.id, second.id], "standard", "", None, ctx,
    )


def test_start_review_without_active_files_or_database_fails_safely(db: Any) -> None:
    """统一入口无法解析 active 文件时不得调用下游审查。

    Args:
        db: 内存 SQLite 会话。

    Returns:
        None: 断言空项目、无数据库和查询异常三类安全失败。
    """
    _add_code_file(db, project_id=9, file_name="deleted.py", status="deleted")
    orch = _bare_orchestrator()
    downstream = MagicMock(return_value=_result("unexpected"))
    orch.review_orch = SimpleNamespace(start_review=downstream)

    orch._db = db
    no_files = orch.start_review(project_id=9, file_ids=[])
    orch._db = None
    no_db = orch.start_review(project_id=9, file_ids=[])
    orch._db = object()
    query_error = orch.start_review(project_id=9, file_ids=[])

    assert all(result.success is False for result in (no_files, no_db, query_error))
    assert all("没有可审查的代码文件" in (result.error or "") for result in (no_files, no_db, query_error))
    downstream.assert_not_called()


def test_download_project_source_requires_same_permission_as_http_route(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent 不得在缺少 project:view 时伪称已生成可用下载地址。"""
    orch = _bare_orchestrator()
    orch._db = db
    orch._user = SimpleNamespace(id=17, role="user")
    get_project = MagicMock(return_value={"id": 9, "project_name": "download-demo"})
    monkeypatch.setattr(orchestrator_module.project_service, "get_project", get_project)
    monkeypatch.setattr(
        orchestrator_module.project_source_service,
        "get_source_archive_metadata",
        MagicMock(return_value=None),
    )
    monkeypatch.setattr(orchestrator_module, "check_permission", lambda *_args, **_kwargs: False)

    denied = orch.download_project_source(9)

    assert denied.success is False
    assert "project:view" in (denied.error or "")
    get_project.assert_not_called()

    monkeypatch.setattr(orchestrator_module, "check_permission", lambda *_args, **_kwargs: True)
    allowed = orch.download_project_source(9)

    assert allowed.success is True
    assert allowed.data["download_url"] == "/api/projects/9/source-archive"
    get_project.assert_called_once_with(db, orch._user, 9)


def test_chat_creates_trace_emits_dispatch_and_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    """聊天入口应生成或保留 trace_id，发布调度事件并调用 ChatAgent。"""
    orch = _bare_orchestrator()
    orch.chat_agent = SimpleNamespace(execute=MagicMock(return_value=_result("chat")))
    emitted = MagicMock()
    monkeypatch.setattr(orchestrator_module, "new_trace_id", MagicMock(return_value="trace-new"))
    monkeypatch.setattr(orchestrator_module, "emit_event", emitted)

    result = orch.chat([{"role": "user", "content": "hi"}])

    ctx = orch.chat_agent.execute.call_args.args[1]
    assert result.data == "chat"
    assert ctx.extra["trace_id"] == "trace-new"
    assert emitted.call_args.kwargs["trace_id"] == "trace-new"
    assert emitted.call_args.kwargs["payload"] == {"messages": 1}

    existing = AgentContext(extra={"trace_id": "trace-existing", "keep": True})
    orch.chat([], existing)
    assert existing.extra == {"trace_id": "trace-existing", "keep": True}


def test_list_and_get_agent_delegate_to_registry() -> None:
    """Agent 元数据读取应直接复用注册中心。"""
    orch = _bare_orchestrator()

    assert orch.list_agents() == {"a": "A"}
    assert orch.get_agent("a") == "agent"
    orch._registry.list.assert_called_once_with()
    orch._registry.get.assert_called_once_with("a")


def test_invoke_tool_routes_skill_and_validated_fixed_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """通用入口应保留 Skill 路由，并在固定工具执行前规范化兼容别名。"""
    orch = _bare_orchestrator()
    skill = SimpleNamespace(name="demo.run", agent_name="demo")
    FakeRegistry.skills = [skill]
    monkeypatch.setattr(SkillRegistry, "instance", FakeRegistry.instance)
    orch.invoke_skill = MagicMock(return_value=_result("skill"))
    ctx = AgentContext(user_id=1)

    assert orch.invoke_tool("demo.run", {"value": 1}, ctx).data == "skill"
    orch.invoke_skill.assert_called_once_with("demo", "demo.run", {"value": 1}, ctx)

    missing = orch.invoke_tool("missing", {}, ctx)
    assert missing.success is False
    assert "不存在" in (missing.error or "")

    orch.list_projects = MagicMock(return_value=_result("projects"))
    fixed = orch.invoke_tool("list_projects", {"project_query": "legacy", "page": 2}, ctx)

    assert fixed.data == "projects"
    orch.list_projects.assert_called_once_with(keyword="legacy", page=2, ctx=ctx)

    orch.start_review = MagicMock(return_value=_result("started"))
    reviewed = orch.invoke_tool("start_review", {"project_id": 7}, ctx)
    assert reviewed.data == "started"
    orch.start_review.assert_called_once_with(project_id=7, ctx=ctx)


def test_invoke_tool_blocks_invalid_or_model_owned_arguments_before_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺失、extra、ctx/user 覆盖请求应返回结构化失败且底层方法绝不执行。"""
    orch = _bare_orchestrator()
    FakeRegistry.skills = []
    monkeypatch.setattr(SkillRegistry, "instance", FakeRegistry.instance)
    orch.delete_project = MagicMock(return_value=_result("deleted"))
    orch.start_review = MagicMock(return_value=_result("started"))

    missing = orch.invoke_tool("delete_project", {})
    extra = orch.invoke_tool("delete_project", {"project_id": 1, "ctx": "forged"})
    forged_user = orch.invoke_tool(
        "start_review",
        {"project_id": 1, "file_ids": [2], "user": "forged"},
    )

    assert missing.success is False
    assert missing.data["validation_errors"]
    assert extra.success is False
    assert forged_user.success is False
    orch.delete_project.assert_not_called()
    orch.start_review.assert_not_called()


def test_invoke_tool_does_not_retry_handler_type_error_and_respects_ctx_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """handler 内部 TypeError 只能调用一次，且无 ctx 工具不得收到上下文。"""
    orch = _bare_orchestrator()
    FakeRegistry.skills = []
    monkeypatch.setattr(SkillRegistry, "instance", FakeRegistry.instance)
    ctx = AgentContext(user_id=7)
    orch.list_projects = MagicMock(side_effect=TypeError("handler bug"))
    orch.review_code = MagicMock(return_value=_result("reviewed"))

    failed = orch.invoke_tool("list_projects", {}, ctx)
    reviewed = orch.invoke_tool(
        "review_code",
        {"code": "print(1)", "rules": "security", "language": "python"},
        ctx,
    )

    assert failed.success is False
    assert "固定工具 list_projects 调用异常" in (failed.error or "")
    assert orch.list_projects.call_count == 1
    assert reviewed.data == "reviewed"
    assert "ctx" not in orch.review_code.call_args.kwargs


def test_invoke_tool_wraps_non_agent_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """返回 dict/list 的固定元数据方法应统一包装为 AgentResult。"""
    orch = _bare_orchestrator()
    FakeRegistry.skills = []
    monkeypatch.setattr(SkillRegistry, "instance", FakeRegistry.instance)
    orch.list_agents = MagicMock(return_value={"a": "A"})
    orch.list_agent_skills = MagicMock(return_value=[{"name": "demo.run"}])

    agents = orch.invoke_tool("list_agents", {})
    skills = orch.invoke_tool("list_agent_skills", {})

    assert agents.success is True
    assert agents.data == {"a": "A"}
    assert skills.success is True
    assert skills.data == [{"name": "demo.run"}]
    orch.list_agent_skills.assert_called_once_with()


def test_invoke_skill_requires_db_and_maps_service_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skill 调用应拒绝未注入 DB，并完整映射服务层记录结果。"""
    orch = _bare_orchestrator()
    denied = orch.invoke_skill("demo", "demo.run", {}, AgentContext())
    assert denied.success is False
    assert "DB 未注入" in (denied.error or "")

    db = object()
    user = SimpleNamespace(id=2)
    ctx = AgentContext(user_id=2)
    orch._db = db
    orch._user = user
    monkeypatch.setattr(orchestrator_module, "check_permission", lambda *_args: True)
    invoke = MagicMock(
        return_value={
            "success": False,
            "data": None,
            "error": "skill failed",
            "duration_ms": 37,
            "effect": "failed",
        },
    )
    monkeypatch.setattr(skill_service, "invoke_skill_with_record", invoke)

    result = orch.invoke_skill("demo", "demo.run", {"x": 1}, ctx, trigger_type="manual")

    assert result.success is False
    assert result.error == "skill failed"
    assert result.duration_ms == 37
    assert result.data["effect"] == "failed"
    assert invoke.call_args.kwargs["trigger_source"] == "orchestrator.invoke_skill:manual"
    assert invoke.call_args.kwargs["user"] is user


def test_list_agent_skills_and_trigger_evolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skill 元数据读取和自进化触发应使用统一 Skill 接口。"""
    orch = _bare_orchestrator()
    FakeRegistry.meta = [{"name": "demo.self_improve"}]
    monkeypatch.setattr(SkillRegistry, "instance", FakeRegistry.instance)

    skills = orch.list_agent_skills("demo")
    assert skills == [{"name": "demo.self_improve", "requested_agent": "demo"}]

    orch.invoke_skill = MagicMock(return_value=_result("evolved"))
    ctx = AgentContext(user_id=5)
    result = orch.trigger_evolution("demo", 30, ctx)
    assert result.data == "evolved"
    orch.invoke_skill.assert_called_once_with(
        "demo",
        "demo.self_improve",
        {"action": "evolve", "window_days": 30},
        ctx,
    )


def test_orchestrator_factory_helpers_cache_metadata_and_isolate_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """元数据单例应缓存，请求级辅助函数每次应构造并注入独立实例。"""
    ConstructedOrchestrator.created.clear()
    monkeypatch.setattr(orchestrator_module, "Orchestrator", ConstructedOrchestrator)
    monkeypatch.setattr(orchestrator_module, "_orchestrator", None)

    first = orchestrator_module.get_orchestrator()
    second = orchestrator_module.get_orchestrator()
    assert first is second
    assert first.register is True

    db = object()
    user = object()
    request_orch = orchestrator_module.get_request_orchestrator(db, user)
    assert request_orch is not first
    assert request_orch.register is False
    assert request_orch.injected == (db, user)
