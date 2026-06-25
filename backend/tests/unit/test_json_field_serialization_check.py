"""验证 IssueOut JSON 字段(references_json/compliance_mapping)的序列化防御。

检查点:
1. references_json (Optional[list]) — list/字符串/None/错误类型 四种值
2. compliance_mapping (Optional[dict]) — dict/字符串/None/错误类型 四种值
3. ORM 对象路径(from_attributes)和 dict 构造路径均覆盖
4. 类型安全:references_json 只接受 list,compliance_mapping 只接受 dict
"""
from types import SimpleNamespace

from app.schemas.review import IssueListItemOut, IssueOut


def _make_orm_like(**overrides):
    """构造测试用 ORM-like 对象,默认值合法,可通过 overrides 覆盖指定字段。"""
    defaults = dict(
        id=1, task_id=1, file_id=1, file_name="x.py",
        line_number=1, end_line=2, issue_type="sqli", severity="高",
        title="t", description="d", suggestion=None, fixed_code=None,
        status="unfixed", create_time="2026-06-25T10:00:00",
        handled_by=None, handled_at=None, update_time=None,
        owasp=None, cwe=None, evidence=None, exploit_scenario=None,
        references_json=None,
        confidence=None, source=None,
        cvss_score=None, cvss_vector=None, compliance_mapping=None,
        remediation=None, static_rule_hits=0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_dict(**overrides):
    """构造测试用 dict,用于验证 list_issues 手动构造路径。"""
    base = {
        "id": 1, "task_id": 1, "project_id": 1, "project_name": "p",
        "task_name": "t", "file_id": 1, "file_name": "x.py",
        "line_number": 1, "end_line": 2, "issue_type": "sqli", "severity": "高",
        "title": "t", "description": "d", "suggestion": None, "fixed_code": None,
        "status": "unfixed", "create_time": "2026-06-25T10:00:00",
        "owasp": None, "cwe": None, "evidence": None, "exploit_scenario": None,
        "references_json": None,
        "confidence": None, "source": None,
        "cvss_score": None, "cvss_vector": None, "compliance_mapping": None,
        "remediation": None, "static_rule_hits": 0,
        "handled_by": None, "handled_at": None, "update_time": None,
    }
    base.update(overrides)
    return base


class TestReferencesJsonSerialization:
    """references_json 字段序列化验证(含 field_validator 防御)"""

    def test_references_json_as_list_orm_path(self):
        """ORM 路径:list → 正确接受"""
        orm = _make_orm_like(references_json=["https://ref1.com", "https://ref2.com"])
        out = IssueOut.model_validate(orm)
        assert out.references_json == ["https://ref1.com", "https://ref2.com"]

    def test_references_json_as_list_dict_path(self):
        """dict 路径:list → 正确接受"""
        data = _make_dict(references_json=["ref1", "ref2"])
        out = IssueListItemOut.model_validate(data)
        assert out.references_json == ["ref1", "ref2"]

    def test_references_json_as_none(self):
        """None → 默认 None"""
        orm = _make_orm_like(references_json=None)
        out = IssueOut.model_validate(orm)
        assert out.references_json is None

    def test_references_json_as_json_string_orm_path(self):
        """ORM 路径:JSON 字符串 → field_validator 解析为 list(防御成功)"""
        orm = _make_orm_like(references_json='["https://ref1.com", "https://ref2.com"]')
        out = IssueOut.model_validate(orm)
        assert out.references_json == ["https://ref1.com", "https://ref2.com"]

    def test_references_json_as_json_string_dict_path(self):
        """dict 路径:JSON 字符串 → field_validator 解析为 list(防御成功)"""
        data = _make_dict(references_json='["ref1", "ref2"]')
        out = IssueListItemOut.model_validate(data)
        assert out.references_json == ["ref1", "ref2"]

    def test_references_json_as_invalid_json_string(self):
        """无效 JSON 字符串 → field_validator 返回 None(不报错)"""
        orm = _make_orm_like(references_json="not a json string")
        out = IssueOut.model_validate(orm)
        assert out.references_json is None

    def test_references_json_as_dict_string_returns_none(self):
        """JSON 字符串解析为 dict(非 list) → 返回 None(类型安全)"""
        orm = _make_orm_like(references_json='{"key": "value"}')
        out = IssueOut.model_validate(orm)
        # references_json 声明为 Optional[list],dict 不符合类型,返回 None
        assert out.references_json is None


class TestComplianceMappingSerialization:
    """compliance_mapping 字段序列化验证(含 field_validator 防御)"""

    def test_compliance_mapping_as_dict_orm_path(self):
        """ORM 路径:dict → 正确接受"""
        mapping = {"iso27001": ["A.5"], "gdpr": ["Art.5"]}
        orm = _make_orm_like(compliance_mapping=mapping)
        out = IssueOut.model_validate(orm)
        assert out.compliance_mapping == mapping

    def test_compliance_mapping_as_dict_dict_path(self):
        """dict 路径:dict → 正确接受"""
        mapping = {"pci_dss": ["6.5.1"]}
        data = _make_dict(compliance_mapping=mapping)
        out = IssueListItemOut.model_validate(data)
        assert out.compliance_mapping == mapping

    def test_compliance_mapping_as_none(self):
        """None → 默认 None"""
        orm = _make_orm_like(compliance_mapping=None)
        out = IssueOut.model_validate(orm)
        assert out.compliance_mapping is None

    def test_compliance_mapping_as_json_string_orm_path(self):
        """ORM 路径:JSON 字符串 → field_validator 解析为 dict(防御成功)"""
        orm = _make_orm_like(compliance_mapping='{"iso27001": ["A.5"]}')
        out = IssueOut.model_validate(orm)
        assert out.compliance_mapping == {"iso27001": ["A.5"]}

    def test_compliance_mapping_as_json_string_dict_path(self):
        """dict 路径:JSON 字符串 → field_validator 解析为 dict(防御成功)"""
        data = _make_dict(compliance_mapping='{"gdpr": ["Art.5"]}')
        out = IssueListItemOut.model_validate(data)
        assert out.compliance_mapping == {"gdpr": ["Art.5"]}

    def test_compliance_mapping_as_invalid_json_string(self):
        """无效 JSON 字符串 → field_validator 返回 None(不报错)"""
        orm = _make_orm_like(compliance_mapping="not a json string")
        out = IssueOut.model_validate(orm)
        assert out.compliance_mapping is None

    def test_compliance_mapping_as_list_string_returns_none(self):
        """JSON 字符串解析为 list(非 dict) → 返回 None(类型安全)"""
        orm = _make_orm_like(compliance_mapping='["item1", "item2"]')
        out = IssueOut.model_validate(orm)
        # compliance_mapping 声明为 Optional[dict],list 不符合类型,返回 None
        assert out.compliance_mapping is None


class TestNonJsonFieldsUnaffected:
    """验证非 JSON 字段未受 field_validator 影响"""

    def test_string_fields_normal(self):
        """owasp/cwe/source 等字符串字段正常"""
        orm = _make_orm_like(
            owasp="A03:2021-Injection",
            cwe="CWE-89",
            source="static",
            evidence="cursor.execute(query)",
        )
        out = IssueOut.model_validate(orm)
        assert out.owasp == "A03:2021-Injection"
        assert out.cwe == "CWE-89"
        assert out.source == "static"
        assert out.evidence == "cursor.execute(query)"

    def test_numeric_fields_normal(self):
        """cvss_score/confidence/static_rule_hits 数值字段正常"""
        orm = _make_orm_like(
            cvss_score=9.8,
            cvss_vector="AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
            confidence=0.95,
            static_rule_hits=3,
        )
        out = IssueOut.model_validate(orm)
        assert out.cvss_score == 9.8
        assert out.cvss_vector == "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
        assert out.confidence == 0.95
        assert out.static_rule_hits == 3
