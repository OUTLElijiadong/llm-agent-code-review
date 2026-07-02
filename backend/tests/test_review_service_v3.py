"""集成测试:review_service v3 双引擎 + Agent 集成 + v3 字段持久化(T08)

验证:
1. _issue_to_review_issue 持久化全量 v3 字段
2. _finding_to_review_issue 持久化全量 v3 字段
3. _review_one_file 双引擎调用流程(static + agent + merge + persist)
4. 静态命中 → source="static", static_rule_hits≥1
5. LLM 命中 → source="llm", static_rule_hits=0
6. 双引擎命中 → source="hybrid", static_rule_hits=1
7. ai_call_log 表 agent_label 字段被填充(BaseAgent._log_call)
8. review_type=security 映射到 SecuritySentinelAgent
9. review_type=standard 映射到 CodeReviewerAgent
10. 二进制文件跳过审查
11. v3 字段(cvss_score/cvss_vector/compliance_mapping/remediation/static_rule_hits)全部持久化
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.agents.base import AgentResult, BaseAgent
from app.ai.multi_agent import GENERAL_AGENT as _GENERAL_PROFILE
from app.ai.result_parser import Issue
from app.ai.static_analyzer import Finding
from app.models.ai_call_log import AiCallLog
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services.review_service import (
    _PROFILE_TO_AGENT_CODE,
    _finding_to_review_issue,
    _issue_to_review_issue,
    _review_one_file,
)


# ============ 辅助函数 ============

def _make_user(uid: int = 1, role: str = "admin") -> User:
    """构造用户对象

    Args:
        uid: 用户 ID
        role: 角色

    Returns:
        User: 未持久化的用户对象
    """
    return User(id=uid, username="tester", password="x", role=role, status=1, email="t@t.com")


def _make_code_file(
    fid: int = 1,
    pid: int = 1,
    content: str = "x = 1\n",
    file_name: str = "test.py",
    is_binary: int = 0,
) -> CodeFile:
    """构造 CodeFile 对象

    Args:
        fid: 文件 ID
        pid: 项目 ID
        content: 文件内容
        file_name: 文件名
        is_binary: 是否二进制

    Returns:
        CodeFile: 未持久化的 ORM 对象
    """
    return CodeFile(
        id=fid, project_id=pid, file_name=file_name, file_path=file_name,
        language="python", content=content, size_bytes=len(content),
        line_count=content.count("\n") + 1, version_no=1, status="active",
        is_binary=is_binary,
    )


def _make_review_task(tid: int = 1, pid: int = 1, uid: int = 1) -> ReviewTask:
    """构造 ReviewTask 对象

    Args:
        tid: 任务 ID
        pid: 项目 ID
        uid: 用户 ID

    Returns:
        ReviewTask: 未持久化的 ORM 对象
    """
    return ReviewTask(
        id=tid, user_id=uid, project_id=pid, task_name="test",
        review_type="standard", status="running", total_files=1,
        model_name="test-model",
    )


def _make_finding(
    *,
    line_number: int = 10,
    cwe: str = "CWE-89",
    severity: str = "高",
    confidence: float = 0.95,
    cvss_score: float = 7.5,
    cvss_vector: str = "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
    compliance_mapping: dict = None,
    remediation: str = "参数化查询",
    static_rule_hits: int = 1,
    source: str = "static",
) -> Finding:
    """构造 Finding 对象

    Args:
        line_number: 行号
        cwe: CWE 编号
        severity: 严重程度
        confidence: 置信度
        cvss_score: CVSS 基础分
        cvss_vector: CVSS 向量
        compliance_mapping: 合规映射
        remediation: 修复方案
        static_rule_hits: 静态命中次数
        source: 来源

    Returns:
        Finding: 标准化漏洞发现
    """
    return Finding(
        line_number=line_number, end_line=line_number,
        issue_type="安全漏洞", severity=severity,
        title="SQL 注入", description="字符串拼接 SQL",
        suggestion="使用参数化查询", fixed_code="",
        owasp="A03:2021-Injection", cwe=cwe,
        evidence="cursor.execute(f'...')",
        exploit_scenario="攻击者注入 OR 1=1",
        references=["https://cwe.mitre.org/"],
        confidence=confidence, source=source,
        cvss_score=cvss_score, cvss_vector=cvss_vector,
        compliance_mapping=compliance_mapping or {},
        remediation=remediation,
        static_rule_hits=static_rule_hits,
    )


def _make_issue(
    *,
    line_number: int = 10,
    cwe: str = "CWE-89",
    severity: str = "高",
    confidence: float = 0.85,
    cvss_score: float = 8.0,
    cvss_vector: str = "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
    compliance_mapping: dict = None,
    remediation: str = "ORM 替代裸 SQL",
    source: str = "llm",
    static_rule_hits: int = 0,
) -> Issue:
    """构造 Issue 对象

    Args:
        line_number: 行号
        cwe: CWE 编号
        severity: 严重程度
        confidence: 置信度
        cvss_score: CVSS 基础分
        cvss_vector: CVSS 向量
        compliance_mapping: 合规映射
        remediation: 修复方案
        source: 来源
        static_rule_hits: 静态命中次数

    Returns:
        Issue: 解析后的问题对象
    """
    return Issue(
        line_number=line_number, end_line=line_number,
        issue_type="安全漏洞", severity=severity,
        title="SQL 注入漏洞", description="存在 SQL 注入风险",
        suggestion="修复建议", fixed_code="",
        owasp="A03:2021-Injection", cwe=cwe,
        evidence="execute(input)", exploit_scenario="注入攻击",
        references=["https://owasp.org/"],
        confidence=confidence,
        cvss_score=cvss_score, cvss_vector=cvss_vector,
        compliance_mapping=compliance_mapping or {},
        remediation=remediation,
        source=source, static_rule_hits=static_rule_hits,
    )


# ============ _issue_to_review_issue v3 字段持久化 ============

class TestIssueToReviewIssue:
    """_issue_to_review_issue() v3 字段持久化测试"""

    def test_v3_fields_persisted(self):
        """v3 字段(cvss_score/cvss_vector/compliance_mapping/remediation/static_rule_hits)全部持久化"""
        code_file = _make_code_file()
        issue = _make_issue(
            cvss_score=9.8,
            cvss_vector="AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
            compliance_mapping={"iso27001": ["A.8"], "gdpr": ["Art.32"]},
            remediation="详细修复方案",
            static_rule_hits=3,
            source="hybrid",
        )
        ri = _issue_to_review_issue(task_id=1, code_file=code_file, issue=issue)
        assert ri.cvss_score == 9.8
        assert ri.cvss_vector == "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
        assert ri.compliance_mapping == {"iso27001": ["A.8"], "gdpr": ["Art.32"]}
        assert ri.remediation == "详细修复方案"
        assert ri.static_rule_hits == 3
        assert ri.source == "hybrid"

    def test_v2_fields_preserved(self):
        """v2 字段(owasp/cwe/evidence/exploit_scenario/references/confidence)保留"""
        code_file = _make_code_file()
        issue = _make_issue()
        ri = _issue_to_review_issue(task_id=1, code_file=code_file, issue=issue)
        assert ri.owasp == "A03:2021-Injection"
        assert ri.cwe == "CWE-89"
        assert ri.evidence == "execute(input)"
        assert ri.exploit_scenario == "注入攻击"
        assert ri.confidence == 0.85
        assert ri.references_json == ["https://owasp.org/"]

    def test_source_static(self):
        """source="static" 时正确持久化"""
        code_file = _make_code_file()
        issue = _make_issue(source="static", static_rule_hits=1)
        ri = _issue_to_review_issue(task_id=1, code_file=code_file, issue=issue)
        assert ri.source == "static"
        assert ri.static_rule_hits == 1

    def test_source_llm(self):
        """source="llm" 时正确持久化"""
        code_file = _make_code_file()
        issue = _make_issue(source="llm", static_rule_hits=0)
        ri = _issue_to_review_issue(task_id=1, code_file=code_file, issue=issue)
        assert ri.source == "llm"
        assert ri.static_rule_hits == 0


# ============ _finding_to_review_issue v3 字段持久化 ============

class TestFindingToReviewIssue:
    """_finding_to_review_issue() v3 字段持久化测试"""

    def test_v3_fields_persisted(self):
        """Finding 的 v3 字段全部持久化到 ReviewIssue"""
        code_file = _make_code_file()
        finding = _make_finding(
            cvss_score=9.8,
            cvss_vector="AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
            compliance_mapping={"pci_dss": ["6.5.1"]},
            remediation="修复方案",
            static_rule_hits=2,
        )
        ri = _finding_to_review_issue(task_id=1, code_file=code_file, finding=finding)
        assert ri.cvss_score == 9.8
        assert ri.cvss_vector == "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
        assert ri.compliance_mapping == {"pci_dss": ["6.5.1"]}
        assert ri.remediation == "修复方案"
        assert ri.static_rule_hits == 2


# ============ _review_one_file 双引擎集成 ============

class TestReviewOneFileDualEngine:
    """_review_one_file() 双引擎集成测试"""

    def test_static_only_persisted_with_source_static(self, db):
        """仅静态命中:source ∈ {static, regex}(均为静态引擎产物), static_rule_hits≥1"""
        code = 'password = "SuperSecret123!"\n'
        code_file = _make_code_file(content=code)
        db.add(code_file)
        db.commit()

        task = _make_review_task()
        user = _make_user()
        db.add(user)
        db.commit()

        # Mock LLM 路径返回空(仅测试静态引擎)
        with patch("app.services.review_service._review_chunk_sequential", return_value=[]):
            profiles = (_GENERAL_PROFILE,)
            issues = _review_one_file(
                db, None, None, task, code_file, [], user, profiles,
            )
        assert len(issues) >= 1
        for ri in issues:
            # 静态引擎包含"静态语义规则(source=static)"与"正则秘钥扫描(source=regex)"两类
            assert ri.source in ("static", "regex")
            assert ri.static_rule_hits >= 1

    def test_llm_only_persisted_with_source_llm(self, db):
        """仅 LLM 命中:source="llm", static_rule_hits=0"""
        code = "x = 1\n"
        code_file = _make_code_file(content=code)
        db.add(code_file)
        db.commit()

        task = _make_review_task()
        user = _make_user()
        db.add(user)
        db.commit()

        llm_finding = _make_finding(line_number=1, cwe="CWE-89", source="llm", static_rule_hits=0)
        with patch("app.services.review_service._review_chunk_sequential", return_value=[llm_finding]):
            profiles = (_GENERAL_PROFILE,)
            issues = _review_one_file(
                db, None, None, task, code_file, [], user, profiles,
            )
        assert len(issues) == 1
        assert issues[0].source == "llm"
        assert issues[0].static_rule_hits == 0

    def test_hybrid_persisted_with_source_hybrid(self, db):
        """双引擎命中:source="hybrid", static_rule_hits≥2(static 1 + LLM 确认 1)"""
        # 静态引擎会命中这个秘钥
        code = 'password = "SuperSecret123!"\n'
        code_file = _make_code_file(content=code)
        db.add(code_file)
        db.commit()

        task = _make_review_task()
        user = _make_user()
        db.add(user)
        db.commit()

        # 先用静态扫描得到实际命中的 cwe 与行号,确保 LLM finding 与之匹配(同 cwe + 同行号 ±2)
        from app.ai.static_analyzer import scan_file as _scan_file
        static_findings = _scan_file(code_file)
        assert len(static_findings) >= 1
        target_cwe = static_findings[0].cwe
        target_line = static_findings[0].line_number

        # LLM 也命中同一行同一 CWE
        llm_finding = _make_finding(
            line_number=target_line, cwe=target_cwe, source="llm", static_rule_hits=0,
        )
        with patch("app.services.review_service._review_chunk_sequential", return_value=[llm_finding]):
            profiles = (_GENERAL_PROFILE,)
            issues = _review_one_file(
                db, None, None, task, code_file, [], user, profiles,
            )
        # 应有至少一个 hybrid 问题
        hybrid_issues = [ri for ri in issues if ri.source == "hybrid"]
        assert len(hybrid_issues) >= 1
        assert hybrid_issues[0].static_rule_hits >= 2

    def test_v3_fields_persisted_to_db(self, db):
        """v3 字段全部持久化到 ReviewIssue 表"""
        code = "x = 1\n"
        code_file = _make_code_file(content=code)
        db.add(code_file)
        db.commit()

        task = _make_review_task()
        user = _make_user()
        db.add(user)
        db.commit()

        llm_finding = _make_finding(
            line_number=1,
            cwe="CWE-89",
            cvss_score=9.8,
            cvss_vector="AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
            compliance_mapping={"iso27001": ["A.8"]},
            remediation="参数化查询",
            static_rule_hits=0,
            source="llm",
        )
        with patch("app.services.review_service._review_chunk_sequential", return_value=[llm_finding]):
            profiles = (_GENERAL_PROFILE,)
            _review_one_file(
                db, None, None, task, code_file, [], user, profiles,
            )

        # 从 DB 查回验证 v3 字段
        db_issues = db.query(ReviewIssue).filter(ReviewIssue.file_id == code_file.id).all()
        assert len(db_issues) >= 1
        ri = db_issues[0]
        assert ri.cvss_score == 9.8
        assert ri.cvss_vector == "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
        assert ri.compliance_mapping == {"iso27001": ["A.8"]}
        assert ri.remediation == "参数化查询"
        assert ri.source == "llm"

    def test_binary_file_skipped(self, db):
        """二进制文件应跳过审查,返回空列表"""
        code_file = _make_code_file(content="", is_binary=1)
        db.add(code_file)
        db.commit()

        task = _make_review_task()
        user = _make_user()
        db.add(user)
        db.commit()

        profiles = (_GENERAL_PROFILE,)
        issues = _review_one_file(
            db, None, None, task, code_file, [], user, profiles,
        )
        assert issues == []


# ============ Agent 映射测试 ============

class TestAgentMapping:
    """review_type → Agent 映射测试"""

    def test_security_maps_to_security_sentinel(self):
        """review_type=security 应映射到 security_sentinel Agent"""
        assert _PROFILE_TO_AGENT_CODE.get("security") == "security_sentinel"

    def test_standard_maps_to_code_reviewer(self):
        """review_type=standard(默认)应映射到 code_reviewer Agent"""
        assert _PROFILE_TO_AGENT_CODE.get("general") == "code_reviewer"

    def test_reliability_maps_to_code_reviewer(self):
        """reliability 画像映射到 code_reviewer"""
        assert _PROFILE_TO_AGENT_CODE.get("reliability") == "code_reviewer"


# ============ BaseAgent._log_call agent_label 测试 ============

class TestAgentLogCall:
    """BaseAgent._log_call() agent_label 写入测试"""

    def test_log_call_writes_agent_label(self, db):
        """_log_call 应将 agent_label 写入 AiCallLog 表"""
        # 构造一个最小 Agent 实例(不走真实 LLM)
        class TestAgent(BaseAgent):
            name = "test_agent_for_log"
            description = "测试 Agent"

        agent = TestAgent(system_prompt="test", temperature=0.1, max_tokens=10)
        result = AgentResult(
            success=True, data="{}", model="test-model",
            duration_ms=100, tokens={"prompt": 10, "completion": 5, "total": 15},
        )
        agent._log_call(
            db,
            task_id=1, user_id=1, file_id=1, chunk_index=0,
            result=result, status="success",
        )
        db.commit()

        log = db.query(AiCallLog).filter(AiCallLog.agent_label == "test_agent_for_log").first()
        assert log is not None
        assert log.agent_label == "test_agent_for_log"
        assert log.model_name == "test-model"
        assert log.duration_ms == 100
        assert log.total_tokens == 15

    def test_log_call_failed_status(self, db):
        """_log_call 失败状态也应写入 agent_label"""
        class TestAgent(BaseAgent):
            name = "test_agent_fail"
            description = "测试 Agent"

        agent = TestAgent(system_prompt="test", temperature=0.1, max_tokens=10)
        agent._log_call(
            db,
            task_id=1, user_id=1, file_id=1, chunk_index=0,
            result=None, status="failed", error="LLM 超时",
        )
        db.commit()

        log = db.query(AiCallLog).filter(AiCallLog.agent_label == "test_agent_fail").first()
        assert log is not None
        assert log.agent_label == "test_agent_fail"
        assert log.status == "failed"
        assert log.error_message == "LLM 超时"

    def test_log_sequential_call_uses_agent_log_call(self, db):
        """_log_sequential_call 传入 agent 时应通过 agent._log_call 写入"""
        from app.services.review_service import _log_sequential_call

        class TestAgent(BaseAgent):
            name = "code_reviewer"
            description = "代码审查 Agent"

        agent = TestAgent(system_prompt="test", temperature=0.1, max_tokens=10)
        task = _make_review_task()
        user = _make_user()
        code_file = _make_code_file()
        db.add_all([user, code_file])
        db.commit()

        result = AgentResult(
            success=True, data="{}", model="deepseek",
            duration_ms=50, tokens={"prompt": 5, "completion": 3, "total": 8},
        )
        _log_sequential_call(
            db, task, user, code_file, 0, 0,
            "code_reviewer", result, status="success",
            agent=agent,
        )
        db.commit()

        log = db.query(AiCallLog).filter(AiCallLog.agent_label == "code_reviewer").first()
        assert log is not None
        assert log.agent_label == "code_reviewer"
        assert log.model_name == "deepseek"
        assert log.total_tokens == 8


# ============ 双引擎调用流程顺序验证 ============

class TestDualEngineFlowOrder:
    """验证审查主流程顺序:静态扫描 → Agent.call() → Issue 合并去重 → v3 字段持久化"""

    def test_flow_order_static_then_llm_then_merge(self, db):
        """验证:静态扫描结果 + LLM 结果 → 合并去重 → 持久化"""
        code = 'api_key = "sk-1234567890abcdef"\n'
        code_file = _make_code_file(content=code)
        db.add(code_file)
        db.commit()

        task = _make_review_task()
        user = _make_user()
        db.add(user)
        db.commit()

        # LLM 返回一个不同位置的发现(确保不与静态去重)
        llm_finding = _make_finding(
            line_number=99, cwe="CWE-89", source="llm", static_rule_hits=0,
        )
        with patch("app.services.review_service._review_chunk_sequential", return_value=[llm_finding]):
            profiles = (_GENERAL_PROFILE,)
            issues = _review_one_file(
                db, None, None, task, code_file, [], user, profiles,
            )

        # 应同时存在静态引擎命中(source ∈ {static, regex})和 llm 引擎命中
        sources = {ri.source for ri in issues}
        assert sources & {"static", "regex"}  # 静态引擎命中(语义规则或正则秘钥)
        assert "llm" in sources                 # LLM 引擎命中

    def test_merged_issues_all_persisted(self, db):
        """合并后的所有 Issue 都应持久化到 ReviewIssue 表"""
        code = "x = 1\n"
        code_file = _make_code_file(content=code)
        db.add(code_file)
        db.commit()

        task = _make_review_task()
        user = _make_user()
        db.add(user)
        db.commit()

        llm_findings = [
            _make_finding(line_number=5, cwe="CWE-89", source="llm"),
            _make_finding(line_number=10, cwe="CWE-79", source="llm"),
            _make_finding(line_number=15, cwe="CWE-22", source="llm"),
        ]
        with patch("app.services.review_service._review_chunk_sequential", return_value=llm_findings):
            profiles = (_GENERAL_PROFILE,)
            _review_one_file(
                db, None, None, task, code_file, [], user, profiles,
            )

        db_issues = db.query(ReviewIssue).filter(ReviewIssue.file_id == code_file.id).all()
        assert len(db_issues) == 3
        cwes = {ri.cwe for ri in db_issues}
        assert cwes == {"CWE-89", "CWE-79", "CWE-22"}
