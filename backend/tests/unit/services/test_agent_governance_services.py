"""Agent 治理平台服务测试。"""
from app.models.agent_governance import AgentToolPermission, ApprovalItem, PolicyRule
from app.models.code_file import CodeFile
from app.models.project import Project
from app.services import (
    agent_governance_service,
    agent_knowledge_service,
    agent_scheduler_runtime,
    approval_service,
    policy_engine,
    rollback_service,
    tool_gateway,
)


def test_policy_engine_allows_low_risk_action(db):
    """验证低风险动作默认自动放行。"""
    decision = policy_engine.evaluate(
        db,
        subject="agent:manager",
        action="knowledge.read",
        resource="agent:manager",
    )

    assert decision.decision == "allow"
    assert decision.risk_level == "low"
    assert decision.log_id is not None


def test_policy_engine_escalates_high_risk_delete(db):
    """验证删除类高风险系统操作会升级审批。"""
    decision = policy_engine.evaluate(
        db,
        subject="agent:manager",
        action="delete",
        resource="production_config",
    )

    assert decision.decision == "escalate"
    assert decision.risk_level == "high"


def test_policy_engine_matches_explicit_deny_rule(db):
    """验证显式拒绝策略优先于默认策略。"""
    db.add(PolicyRule(
        rule_code="deny_shell_all",
        name="拒绝 shell",
        subject="agent:*",
        action="shell*",
        resource="*",
        effect="deny",
        risk_level="critical",
        priority=1,
        enabled=1,
    ))
    db.commit()

    decision = policy_engine.evaluate(
        db,
        subject="agent:manager",
        action="shell.read",
        resource="workspace",
    )

    assert decision.decision == "deny"
    assert decision.risk_level == "critical"


def test_approval_service_auto_approves_low_risk(db, admin_user):
    """验证低风险 allow 决策自动审批。"""
    item = approval_service.create_or_auto_decide(
        db,
        title="读取知识库",
        action="knowledge.read",
        resource="agent:manager",
        risk_level="low",
        decision="allow",
        reason="低风险",
        agent_code="manager",
        actor=admin_user,
    )

    assert item.status == "auto_approved"
    assert item.decision == "allow"


def test_tool_gateway_escalates_high_risk_action(db, admin_user):
    """验证工具网关会把高风险动作升级审批并写日志。"""
    result = tool_gateway.execute(
        db,
        agent_code="manager",
        tool_code="config",
        action="production_config.update",
        resource="production_config",
        input_summary="change prod config",
        actor=admin_user,
    )

    assert result.success is False
    assert result.status == "escalated"
    assert result.approval_id is not None
    assert result.log_id is not None


def test_tool_gateway_applies_tool_permission_deny(db):
    """验证工具权限配置会真实阻断工具网关调用。"""
    db.add(AgentToolPermission(
        agent_code="manager",
        tool_code="shell",
        permission="deny",
        risk_level="critical",
        enabled=1,
        note="禁止 shell",
    ))
    db.commit()

    result = tool_gateway.execute(
        db,
        agent_code="manager",
        tool_code="shell",
        action="shell.read",
        resource="workspace",
    )

    assert result.success is False
    assert result.status == "denied"
    assert result.risk_level == "critical"


def test_agent_governance_sync_profiles_creates_governance_agents(db):
    """验证治理 Agent 画像可同步并绑定自我进化 skill。"""
    rows = agent_governance_service.sync_profiles(db)
    manager = next(row for row in rows if row.code == "manager")
    data = agent_governance_service.profile_to_dict(db, manager)

    assert manager.name == "管理Agent"
    assert "selfimprovingagent" in data["skills"]
    assert "reflection" in data["skills"]


def test_high_risk_agent_knowledge_requires_approval_and_can_activate(db, admin_user):
    """验证高风险知识入库进入审批，通过后变为 active。"""
    doc = agent_knowledge_service.add_document(
        db,
        agent_code="security",
        title="高风险外部知识",
        content="需要审批的外部内容",
        source_type="url",
        source_ref="https://example.com/risk",
        risk_level="high",
        confidence=0.4,
    )
    approval = db.query(ApprovalItem).filter(ApprovalItem.action == "knowledge.activate").first()

    assert doc.status == "pending_approval"
    assert approval is not None
    approval_service.decide_item(db, admin_user, approval.id, approve=True, note="确认可信")
    db.refresh(doc)
    assert doc.status == "active"


def test_agent_knowledge_crawls_project_code_source(db, admin_user):
    """验证项目代码来源可被每日抓取任务沉淀为 Agent 知识。"""
    project = Project(user_id=admin_user.id, project_name="Prism", language="python", status="active")
    db.add(project)
    db.commit()
    db.refresh(project)
    code_file = CodeFile(
        project_id=project.id,
        file_name="policy.py",
        file_path="backend/policy.py",
        language="python",
        size_bytes=20,
        line_count=1,
        content="def allow():\n    return True",
        status="active",
    )
    db.add(code_file)
    db.commit()

    agent_knowledge_service.upsert_source(
        db,
        agent_code="manager",
        source_type="project",
        source_uri=str(project.id),
        config={"file_limit": 5},
    )
    result = agent_knowledge_service.crawl_enabled_sources(db, agent_code="manager")
    docs = agent_knowledge_service.list_docs(db, agent_code="manager")

    assert result["doc_count"] == 1
    assert docs[0].source_ref == f"code_file:{code_file.id}"
    assert docs[0].status == "active"


def test_agent_knowledge_crawls_official_url_source(db, monkeypatch):
    """验证官方 URL 来源会抓取并抽取 HTML 文本。"""
    monkeypatch.setattr(
        agent_knowledge_service,
        "_fetch_text_url",
        lambda url: "<html><body><h1>Policy Guide</h1><p>Use approvals.</p></body></html>",
    )
    agent_knowledge_service.upsert_source(
        db,
        agent_code="policy",
        source_type="official",
        source_uri="https://docs.example.com/policy",
    )

    result = agent_knowledge_service.crawl_enabled_sources(db, agent_code="policy")
    docs = agent_knowledge_service.list_docs(db, agent_code="policy")

    assert result["doc_count"] == 1
    assert docs[0].title == "Policy Guide"
    assert docs[0].source_type == "official"


def test_agent_knowledge_crawls_github_issue_source(db, monkeypatch):
    """验证 GitHub issue/PR 来源会通过 GitHub API 生成知识文档。"""
    monkeypatch.setattr(
        agent_knowledge_service,
        "_fetch_json_url",
        lambda url: {
            "number": 12,
            "title": "审批链路需要审计",
            "body": "记录风险分、审批人和决策原因。",
            "state": "open",
            "html_url": "https://github.com/org/repo/issues/12",
            "labels": [{"name": "governance"}],
            "user": {"login": "maintainer"},
        },
    )
    agent_knowledge_service.upsert_source(
        db,
        agent_code="approval",
        source_type="github",
        source_uri="https://github.com/org/repo/issues/12",
    )

    result = agent_knowledge_service.crawl_enabled_sources(db, agent_code="approval")
    docs = agent_knowledge_service.list_docs(db, agent_code="approval")

    assert result["doc_count"] == 1
    assert "Issue #12" in docs[0].title
    assert docs[0].source_ref == "https://github.com/org/repo/issues/12"


def test_agent_knowledge_url_validation_blocks_private_urls(monkeypatch):
    """验证外部知识抓取默认阻断内网 URL，同时允许公网 URL 查询参数。"""
    monkeypatch.setattr(agent_knowledge_service.settings, "agent_knowledge_allow_private_urls", False)
    monkeypatch.setattr(agent_knowledge_service.settings, "agent_knowledge_enforce_dns_check", False)

    assert agent_knowledge_service._validate_knowledge_url("http://127.0.0.1:8000/admin") == ""
    assert (
        agent_knowledge_service._validate_knowledge_url(
            "https://api.github.com/repos/org/repo/issues?state=open&per_page=1",
        )
        == "https://api.github.com/repos/org/repo/issues?state=open&per_page=1"
    )


def test_agent_knowledge_fetch_blocks_unsafe_redirect(monkeypatch):
    """验证知识抓取会阻断跳转到内网地址的外部来源。"""
    class FakeResponse:
        """用于模拟 HTTP 跳转响应。"""

        status_code = 302
        content = b""
        encoding = "utf-8"
        headers = {"location": "http://127.0.0.1:8000/private"}

        def raise_for_status(self):
            """模拟 httpx 响应接口。"""
            return None

    class FakeClient:
        """用于模拟 httpx.Client 上下文。"""

        def __init__(self, *args, **kwargs):
            """保存构造参数以兼容真实 Client 接口。"""
            self.calls = 0

        def __enter__(self):
            """进入上下文。"""
            return self

        def __exit__(self, exc_type, exc, tb):
            """退出上下文。"""
            return False

        def get(self, url, headers=None):
            """返回跳转到内网的响应。"""
            self.calls += 1
            return FakeResponse()

    monkeypatch.setattr(agent_knowledge_service.settings, "agent_knowledge_allow_private_urls", False)
    monkeypatch.setattr(agent_knowledge_service.httpx, "Client", FakeClient)

    assert agent_knowledge_service._fetch_text_url("https://docs.example.com/redirect") == ""


def test_policy_artifact_rollback_restores_policy_rule(db):
    """验证策略 artifact 回滚会反写策略规则。"""
    rule = PolicyRule(
        rule_code="allow_knowledge",
        name="允许知识读取",
        subject="agent:*",
        action="knowledge.read",
        resource="*",
        effect="allow",
        risk_level="low",
        priority=10,
        enabled=1,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    snapshot = (
        '{"rule_id": %d, "rule_code": "allow_knowledge", "name": "允许知识读取", '
        '"subject": "agent:*", "action": "knowledge.read", "resource": "*", '
        '"effect": "allow", "risk_level": "low", "condition_json": "{}", '
        '"priority": 10, "enabled": 1}'
    ) % rule.id
    version = rollback_service.create_version(
        db,
        agent_code="policy",
        artifact_type="policy",
        version="v1",
        content=snapshot,
        snapshot=snapshot,
        status="stable",
    )
    rule.effect = "deny"
    rule.risk_level = "critical"
    db.commit()

    rollback_service.rollback_version(db, version.id)
    db.refresh(rule)

    assert rule.effect == "allow"
    assert rule.risk_level == "low"


def test_agent_scheduler_runtime_parses_daily_schedule():
    """验证治理后台调度器可解析每日调度表达式。"""
    assert agent_scheduler_runtime._parse_daily_schedule("daily@02:30") == (2, 30)
    assert agent_scheduler_runtime._parse_daily_schedule("manual") is None
    assert agent_scheduler_runtime._parse_daily_schedule("daily@25:00") is None


def test_agent_scheduler_runtime_respects_disabled_config(monkeypatch):
    """验证关闭配置时不会启动治理后台调度器。"""
    monkeypatch.setattr(agent_scheduler_runtime.settings, "agent_governance_scheduler_enabled", False)
    agent_scheduler_runtime.stop_agent_governance_scheduler()

    agent_scheduler_runtime.start_agent_governance_scheduler()

    assert agent_scheduler_runtime._scheduler is None
