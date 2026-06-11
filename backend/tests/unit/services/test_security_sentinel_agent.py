"""单元测试 (v2.1): SecuritySentinelAgent 核心行为

不发起任何真实 LLM 调用,使用 monkeypatch 替换 call_json。
"""
from unittest.mock import MagicMock

from app.agents.base import AgentResult
from app.agents.security_sentinel_agent import SecuritySentinelAgent
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User


def _make_user(role="admin", uid=1):
    return User(id=uid, role=role, status=1, username="tester")


def _make_file(file_id=1, project_id=1, content="x = 1",
               file_name="foo.py", language="python"):
    return CodeFile(
        id=file_id, project_id=project_id, file_name=file_name,
        file_path=file_name, language=language, content=content,
        line_count=content.count("\n") + 1, size_bytes=len(content),
        status="active",
    )


def _make_project(project_id=1, user_id=1, name="demo"):
    return Project(
        id=project_id, user_id=user_id, project_name=name,
        language="python", status="active",
    )


def _make_task(task_id=1, user_id=1, project_id=1):
    return ReviewTask(
        id=task_id, user_id=user_id, project_id=project_id,
        task_name="t1", review_type="security", status="success",
        total_files=1, processed_files=1,
    )


# ---------- 元数据与 checklist ----------


def test_agent_metadata_fields_are_set():
    agent = SecuritySentinelAgent()
    assert agent.name == "security_sentinel"
    assert agent.category == "security"
    assert agent.icon == "security_sentinel"
    assert "OWASP Top10" in agent.skills


def test_get_checklist_contains_owasp_and_secret_patterns():
    agent = SecuritySentinelAgent()
    data = agent.get_checklist()
    assert len(data["owasp_top10"]) == 10
    assert any(item["code"] == "A03" for item in data["owasp_top10"])
    assert len(data["secret_patterns"]) >= 10


# ---------- _ensure_db / 鉴权 ----------


def test_scan_file_without_db_returns_error():
    agent = SecuritySentinelAgent()
    result = agent.scan_file(file_id=1)
    assert result.success is False
    assert "DB" in (result.error or "")


def test_scan_file_authz_rejects_non_owner():
    agent = SecuritySentinelAgent()
    db = MagicMock()
    file = _make_file(file_id=1, project_id=99)
    project = _make_project(project_id=99, user_id=999)
    db.get.side_effect = lambda model, _id: {
        CodeFile: file,
        Project: project,
    }.get(model)
    user = _make_user(role="user", uid=42)
    agent.inject(db, user=user)
    result = agent.scan_file(file_id=1)
    assert result.success is False
    assert "无权" in result.error


# ---------- 单文件正则路径 ----------


def test_scan_file_regex_secret_detected_without_llm(monkeypatch):
    """正则路径独立工作: 即使 LLM 失败也能返回 finding"""
    agent = SecuritySentinelAgent()

    db = MagicMock()
    content = 'OPENAI_KEY = "sk-proj-AbCdEf1234567890XyZqWeRtYu"\n'
    file = _make_file(content=content)
    db.get.return_value = file
    agent.inject(db, user=_make_user())

    # mock LLM 失败
    monkeypatch.setattr(
        agent, "call_json",
        lambda *a, **kw: AgentResult(success=False, error="mocked"),
    )

    result = agent.scan_file(file_id=1, scan_depth="standard")
    assert result.success is True
    findings = result.data["findings"]
    assert any(f["source"] == "regex" for f in findings)
    secret = next(f for f in findings if f["source"] == "regex")
    assert secret["severity"] == "严重"
    assert secret["cwe"] == "CWE-798"
    # evidence 必须脱敏
    assert "sk-proj-AbCdEf1234567890XyZqWeRtYu" not in secret["evidence"]


def test_scan_file_llm_finding_integrates_offset(monkeypatch):
    """LLM 返回相对行号,应正确换算 (单 chunk 内 offset=0)"""
    agent = SecuritySentinelAgent()

    db = MagicMock()
    file = _make_file(content="def f():\n    return 1\n")
    db.get.return_value = file
    agent.inject(db, user=_make_user())

    monkeypatch.setattr(agent, "call_json", lambda *a, **kw: AgentResult(
        success=True,
        data={
            "findings": [{
                "title": "SQL 注入",
                "category": "注入",
                "owasp": "A03:2021-Injection",
                "cwe": "CWE-89",
                "severity": "高",
                "line_start": 2,
                "line_end": 2,
                "evidence": "query = f\"...\"",
                "exploit_scenario": "字符串拼接构造 SQL",
                "fix_suggestion": "参数化查询",
                "references": ["https://owasp.org/x"],
                "confidence": 0.9,
            }],
            "entry_points": [],
            "dangerous_sinks": [],
        },
    ))

    result = agent.scan_file(file_id=1)
    assert result.success is True
    findings = [f for f in result.data["findings"] if f["source"] == "llm"]
    assert len(findings) == 1
    assert findings[0]["owasp"] == "A03:2021-Injection"
    assert findings[0]["cwe"] == "CWE-89"
    assert findings[0]["line_number"] == 2


def test_scan_file_invalid_depth_returns_error():
    agent = SecuritySentinelAgent()
    db = MagicMock()
    agent.inject(db, user=_make_user())
    result = agent.scan_file(file_id=1, scan_depth="ultra")
    assert result.success is False
    assert "scan_depth" in result.error


def test_scan_all_projects_empty_scope_returns_empty_result():
    """全量扫描在没有可见项目时应稳定返回空结果"""
    agent = SecuritySentinelAgent()
    db = MagicMock()
    chain = MagicMock()
    chain.filter.return_value = chain
    chain.order_by.return_value.all.return_value = []
    db.query.return_value = chain
    agent.inject(db, user=_make_user())

    result = agent.scan_all_projects()

    assert result.success is True
    assert result.data["findings"] == []
    assert result.data["file_count"] == 0
    assert result.data["risk_score"] == 100
    assert result.data["compliance"]["project_count"] == 0


def test_scan_all_projects_aggregates_project_results(monkeypatch):
    """全量扫描应复用单项目扫描并聚合 findings/文件数/数据流"""
    agent = SecuritySentinelAgent()
    db = MagicMock()
    projects = [
        _make_project(project_id=1, user_id=1, name="alpha"),
        _make_project(project_id=2, user_id=1, name="beta"),
    ]
    chain = MagicMock()
    chain.filter.return_value = chain
    chain.order_by.return_value.all.return_value = projects
    db.query.return_value = chain
    agent.inject(db, user=_make_user())

    def fake_scan_project(project_id, top_n=50, trace_dataflow=True, ctx=None):
        return AgentResult(success=True, data={
            "findings": [{
                "title": f"SQL 注入 {project_id}",
                "category": "注入",
                "owasp": "A03:2021-Injection",
                "cwe": "CWE-89",
                "severity": "严重",
                "file_path": "api.py",
                "file_id": project_id,
                "lines": "L1",
                "line_number": 1,
                "end_line": 1,
                "evidence": "query",
                "exploit_scenario": "SQL 注入",
                "fix_suggestion": "参数化查询",
                "references": [],
                "confidence": 0.9,
                "source": "llm",
            }],
            "threat_model": {
                "entry_points": [{"file": "api.py", "line": 1}],
                "data_flows": [{
                    "from": "api.py:handler",
                    "via": ["service.py:run"],
                    "to": "db.py:query",
                    "risk_type": "SQL 注入",
                    "severity": "严重",
                }],
                "api_endpoints": [{
                    "method": "POST",
                    "path": "/login",
                    "file_path": "api.py",
                    "line_number": 1,
                    "handler": "handler",
                    "auth_hint": "未发现明显认证线索",
                    "source": "python_route",
                }],
                "code_links": [{
                    "from": "api.py:handler",
                    "to": "db.py:query",
                    "relation": "跨文件数据流",
                    "risk_type": "SQL 注入",
                    "severity": "严重",
                }],
                "attack_surface_summary": "mock",
            },
            "compliance": {},
            "risk_score": 85,
            "summary": "mock",
            "file_count": project_id,
            "duration_ms": 1,
        })

    monkeypatch.setattr(agent, "scan_project", fake_scan_project)

    result = agent.scan_all_projects(top_n_per_project=10)

    assert result.success is True
    assert len(result.data["findings"]) == 2
    assert result.data["file_count"] == 3
    assert result.data["risk_score"] == 70
    assert result.data["compliance"]["scanned_project_count"] == 2
    assert result.data["findings"][0]["file_path"] == "alpha/api.py"
    assert result.data["threat_model"]["data_flows"][1]["from"] == "beta/api.py:handler"
    assert result.data["threat_model"]["api_endpoints"][0]["file_path"] == "alpha/api.py"
    assert result.data["threat_model"]["code_links"][1]["to"] == "beta/db.py:query"
    assert result.data["discussion"]["turns"]


def test_extract_api_endpoints_detects_common_routes():
    """接口扫描应识别后端路由和前端 HTTP client 调用"""
    agent = SecuritySentinelAgent()
    file = _make_file(
        content=(
            "from fastapi import APIRouter, Depends\n"
            "router = APIRouter()\n"
            "@router.post('/users')\n"
            "async def create_user(current_user=Depends(get_current_user)):\n"
            "    pass\n"
            "app.get('/health', handler)\n"
            "axios.post('/api/login', data)\n"
        ),
        file_name="api.py",
        language="python",
    )

    endpoints = agent._extract_api_endpoints(file)

    paths = {(item["method"], item["path"]) for item in endpoints}
    assert ("POST", "/users") in paths
    assert ("GET", "/health") in paths
    assert ("POST", "/api/login") in paths
    user_endpoint = next(item for item in endpoints if item["path"] == "/users")
    assert user_endpoint["handler"] == "create_user"
    assert user_endpoint["auth_hint"] == "发现认证/权限线索"


def test_build_code_links_connects_endpoint_to_sink_and_dataflow():
    """代码联动关系应同时包含接口到 sink 和已有数据流"""
    agent = SecuritySentinelAgent()

    links = agent._build_code_links(
        api_endpoints=[{
            "method": "POST",
            "path": "/users",
            "file_path": "api.py",
            "line_number": 3,
            "handler": "create_user",
        }],
        sinks=[{
            "file": "api.py",
            "name": "raw_query",
            "sink_type": "SQL",
        }],
        data_flows=[{
            "from": "api.py:create_user",
            "to": "db.py:query",
            "risk_type": "SQL 注入",
            "severity": "高",
        }],
    )

    assert any(link["relation"] == "接口到同文件危险接收点" for link in links)
    assert any(link["relation"] == "跨文件数据流" for link in links)
    assert any(link["risk_type"] == "SQL 注入" for link in links)


def test_build_multi_agent_discussion_returns_action_items():
    """多 Agent 讨论摘要应给出发言、共识和行动项"""
    agent = SecuritySentinelAgent()
    discussion = agent._build_multi_agent_discussion(
        findings=[{
            "severity": "高",
            "owasp": "A03:2021-Injection",
        }],
        threat_model={
            "api_endpoints": [{
                "auth_hint": "未发现明显认证线索",
            }],
            "data_flows": [],
            "code_links": [{"severity": "高"}],
        },
        project_count=2,
    )

    assert discussion["mode"] == "multi_agent_summary"
    assert len(discussion["turns"]) >= 4
    assert "多 Agent 共识" in discussion["consensus"]
    assert any("认证" in item for item in discussion["action_items"])


# ---------- 风险评分 ----------


def test_compute_risk_score_severe_drops_15_each():
    agent = SecuritySentinelAgent()
    findings = [{"severity": "严重"}, {"severity": "严重"}, {"severity": "中"}]
    score = agent._compute_risk_score(findings)
    # 100 - 15*2 - 3 = 67
    assert score == 67


def test_compute_risk_score_clamps_to_zero():
    agent = SecuritySentinelAgent()
    findings = [{"severity": "严重"}] * 20  # 300 deduct
    assert agent._compute_risk_score(findings) == 0


# ---------- OWASP/CWE 推断 ----------


def test_infer_owasp_cwe_for_sql_injection():
    agent = SecuritySentinelAgent()
    owasp, cwe = agent._infer_owasp_cwe("SQL 注入风险", "存在 sql injection")
    assert "Injection" in owasp
    assert cwe == "CWE-89"


def test_infer_owasp_cwe_for_path_traversal():
    agent = SecuritySentinelAgent()
    owasp, cwe = agent._infer_owasp_cwe("路径遍历", "")
    assert cwe == "CWE-22"


def test_infer_owasp_cwe_fallback_to_empty():
    agent = SecuritySentinelAgent()
    owasp, cwe = agent._infer_owasp_cwe("一些无关问题", "blablabla")
    assert owasp == ""
    assert cwe == ""


# ---------- 任务复审 ----------


def test_scan_task_labels_existing_security_issues(monkeypatch):
    """已落库的安全 issue 应被打上 OWASP/CWE 标签"""
    agent = SecuritySentinelAgent()
    db = MagicMock()
    task = _make_task(task_id=10, user_id=1)
    db.get.return_value = task

    # 模拟 db.query(...).filter(...).all()
    issues = [
        ReviewIssue(
            id=1, task_id=10, file_id=1, file_name="x.py",
            line_number=5, end_line=5,
            issue_type="安全漏洞", severity="高",
            title="SQL 注入风险",
            description="字符串拼接构造 SQL",
            suggestion="参数化查询",
            status="unfixed",
        ),
        ReviewIssue(
            id=2, task_id=10, file_id=1, file_name="x.py",
            line_number=20, end_line=20,
            issue_type="安全漏洞", severity="中",
            title="XSS 风险",
            description="未转义用户输入",
            suggestion="使用模板自动转义",
            status="unfixed",
        ),
    ]
    chain = MagicMock()
    chain.filter.return_value.all.return_value = issues
    db.query.return_value = chain

    agent.inject(db, user=_make_user())
    result = agent.scan_task(task_id=10)
    assert result.success is True
    findings = result.data["findings"]
    assert len(findings) == 2
    titles = {f["title"] for f in findings}
    assert "SQL 注入风险" in titles
    # 自动打了 OWASP/CWE 标签
    sql_finding = next(f for f in findings if "SQL" in f["title"])
    assert "Injection" in sql_finding["owasp"]
    assert sql_finding["cwe"] == "CWE-89"


# ---------- 合规与文件优先级 ----------


def test_compute_compliance_extracts_owasp_codes():
    agent = SecuritySentinelAgent()
    findings = [
        {"owasp": "A03:2021-Injection"},
        {"owasp": "A03:2021-Injection"},
        {"owasp": "A07:2021-Identification"},
    ]
    out = agent._compute_compliance(findings)
    assert set(out["owasp_coverage"]) == {"A03", "A07"}


def test_prioritize_files_puts_high_risk_first():
    agent = SecuritySentinelAgent()
    files = [
        _make_file(file_id=1, file_name="readme.md"),
        _make_file(file_id=2, file_name="api/auth_controller.py"),
        _make_file(file_id=3, file_name="utils/format.py"),
    ]
    ordered = agent._prioritize_files(files)
    assert ordered[0].id == 2  # auth_controller 含 api/auth/controller 三个关键词


def test_upgrade_findings_on_dataflow():
    agent = SecuritySentinelAgent()
    findings = [
        {"severity": "中", "file_path": "api/auth.py"},
        {"severity": "低", "file_path": "utils/helper.py"},
    ]
    flows = [{"from": "api/auth.py:login", "via": [], "to": "db:query"}]
    agent._upgrade_findings_on_dataflow(findings, flows)
    # auth.py 的 finding 升级到 高
    assert findings[0]["severity"] == "高"
    # helper.py 不在数据流上,不变
    assert findings[1]["severity"] == "低"
