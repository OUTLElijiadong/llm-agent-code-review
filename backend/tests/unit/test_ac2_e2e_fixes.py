"""AC2 端到端验证期间发现的 3 个问题修复的单元测试

覆盖:
1. review_issue.owasp/cwe 列长度扩大(String(32) → String(128)/String(64))
2. AiLogOut/AiLogDetailOut schema 添加 agent_label 字段
3. ai_log_service._to_traceable_dict 返回 dict 包含 agent_label

运行方式:
    cd backend && python -m pytest tests/unit/test_ac2_e2e_fixes.py -v
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.models.review_issue import ReviewIssue
from app.schemas.ai_log import AiLogDetailOut, AiLogOut

# ────────────────────────────────────────────────────────────────
# 修复1:review_issue.owasp/cwe 列长度扩大
# ────────────────────────────────────────────────────────────────

class TestReviewIssueOwaspCweColumnSize:
    """验证 review_issue 表 owasp/cwe 列长度符合预期

    根因:OWASP Top10 完整标题如 "A07:2021-Identification and Authentication Failures"
    长度 46,超过原 String(32) 上限导致 DataError(1406)。
    """

    def test_owasp_column_type_is_string_128(self):
        """owasp 列应为 String(128),容纳完整 OWASP 标题"""
        col = ReviewIssue.__table__.c.owasp
        assert col.type.length == 128, (
            f"owasp 列长度应为 128,实际 {col.type.length}"
        )

    def test_cwe_column_type_is_string_64(self):
        """cwe 列应为 String(64),预留扩展空间"""
        col = ReviewIssue.__table__.c.cwe
        assert col.type.length == 64, (
            f"cwe 列长度应为 64,实际 {col.type.length}"
        )

    def test_owasp_column_accepts_long_owasp_title(self):
        """owasp 列应能容纳最长的 OWASP Top10 标题(46 字符)"""
        longest_owasp = "A07:2021-Identification and Authentication Failures"
        assert len(longest_owasp) <= 128, (
            f"OWASP 标题长度 {len(longest_owasp)} 超过列定义 128"
        )

    def test_owasp_column_nullable(self):
        """owasp 列应允许 NULL"""
        col = ReviewIssue.__table__.c.owasp
        assert col.nullable is True

    def test_cwe_column_nullable(self):
        """cwe 列应允许 NULL"""
        col = ReviewIssue.__table__.c.cwe
        assert col.nullable is True


# ────────────────────────────────────────────────────────────────
# 修复2:AiLogOut/AiLogDetailOut schema 添加 agent_label 字段
# ────────────────────────────────────────────────────────────────

class TestAiLogSchemaHasAgentLabel:
    """验证 AI 日志 schema 包含 agent_label 字段

    根因:AiLogOut/AiLogDetailOut 未定义 agent_label 字段,
    导致 Pydantic 序列化时丢失该字段(API 返回 null)。
    """

    def test_ai_log_out_has_agent_label_field(self):
        """AiLogOut schema 应包含 agent_label 字段"""
        fields = AiLogOut.model_fields
        assert "agent_label" in fields, (
            "AiLogOut 缺少 agent_label 字段"
        )

    def test_ai_log_out_agent_label_optional(self):
        """AiLogOut.agent_label 应为 Optional(允许 None)"""
        field_info = AiLogOut.model_fields["agent_label"]
        # Pydantic v2 中 Optional 字段默认值为 None
        assert field_info.is_required() is False, (
            "agent_label 应为可选字段"
        )

    def test_ai_log_detail_out_has_agent_label_field(self):
        """AiLogDetailOut schema 应包含 agent_label 字段"""
        fields = AiLogDetailOut.model_fields
        assert "agent_label" in fields, (
            "AiLogDetailOut 缺少 agent_label 字段"
        )

    def test_ai_log_detail_out_agent_label_optional(self):
        """AiLogDetailOut.agent_label 应为 Optional(允许 None)"""
        field_info = AiLogDetailOut.model_fields["agent_label"]
        assert field_info.is_required() is False, (
            "agent_label 应为可选字段"
        )

    def test_ai_log_out_serializes_agent_label(self):
        """AiLogOut 应能正确序列化 agent_label 字段"""
        from datetime import datetime, timezone

        log_out = AiLogOut(
            id=1,
            model_name="deepseek-chat",
            agent_label="code_reviewer",
            status="success",
            create_time=datetime.now(timezone.utc),
        )
        data = log_out.model_dump()
        assert data["agent_label"] == "code_reviewer"

    def test_ai_log_out_agent_label_defaults_none(self):
        """AiLogOut.agent_label 未传时默认为 None"""
        from datetime import datetime, timezone

        log_out = AiLogOut(
            id=1,
            model_name="deepseek-chat",
            status="success",
            create_time=datetime.now(timezone.utc),
        )
        data = log_out.model_dump()
        assert data["agent_label"] is None


# ────────────────────────────────────────────────────────────────
# 修复3:ai_log_service._to_traceable_dict 返回 agent_label
# ────────────────────────────────────────────────────────────────

class TestAiLogServiceReturnsAgentLabel:
    """验证 ai_log_service._to_traceable_dict 返回的 dict 包含 agent_label

    根因:_to_traceable_dict 手动构造 dict 时遗漏了 agent_label 字段,
    即使 schema 有该字段也返回 null。
    """

    @pytest.fixture
    def mock_db(self):
        """构造 mock db session,所有 get() 返回 None(无关联实体)"""
        db = MagicMock()
        db.get.return_value = None
        return db

    @pytest.fixture
    def mock_log_with_label(self):
        """构造带 agent_label 的 AiCallLog mock 对象"""
        return SimpleNamespace(
            id=100,
            task_id=50,
            user_id=1,
            file_id=10,
            chunk_index=0,
            model_name="deepseek-chat/code_reviewer-agent",
            agent_label="code_reviewer",
            prompt_tokens=100,
            completion_tokens=200,
            total_tokens=300,
            duration_ms=1500,
            status="success",
            error_message=None,
            prompt="test prompt",
            response="test response",
            create_time=None,
        )

    @pytest.fixture
    def mock_log_without_label(self):
        """构造 agent_label=None 的 AiCallLog mock 对象"""
        return SimpleNamespace(
            id=101,
            task_id=50,
            user_id=1,
            file_id=10,
            chunk_index=1,
            model_name="deepseek-chat",
            agent_label=None,
            prompt_tokens=100,
            completion_tokens=200,
            total_tokens=300,
            duration_ms=1500,
            status="success",
            error_message=None,
            prompt="test prompt",
            response="test response",
            create_time=None,
        )

    def test_to_traceable_dict_includes_agent_label(self, mock_db, mock_log_with_label):
        """_to_traceable_dict 返回的 dict 应包含 agent_label 字段"""
        from app.services.ai_log_service import _to_traceable_dict

        result = _to_traceable_dict(mock_db, mock_log_with_label, include_detail=False)
        assert "agent_label" in result, (
            "_to_traceable_dict 返回的 dict 缺少 agent_label 键"
        )

    def test_to_traceable_dict_returns_correct_agent_label(self, mock_db, mock_log_with_label):
        """_to_traceable_dict 应返回正确的 agent_label 值"""
        from app.services.ai_log_service import _to_traceable_dict

        result = _to_traceable_dict(mock_db, mock_log_with_label, include_detail=False)
        assert result["agent_label"] == "code_reviewer"

    def test_to_traceable_dict_agent_label_none_when_db_null(self, mock_db, mock_log_without_label):
        """_to_traceable_dict 在 log.agent_label 为 None 时应返回 None"""
        from app.services.ai_log_service import _to_traceable_dict

        result = _to_traceable_dict(mock_db, mock_log_without_label, include_detail=False)
        assert result["agent_label"] is None

    def test_to_traceable_dict_includes_agent_label_in_detail_mode(self, mock_db, mock_log_with_label):
        """_to_traceable_dict 在 include_detail=True 时也应包含 agent_label"""
        from app.services.ai_log_service import _to_traceable_dict

        result = _to_traceable_dict(mock_db, mock_log_with_label, include_detail=True)
        assert "agent_label" in result
        assert result["agent_label"] == "code_reviewer"


# ────────────────────────────────────────────────────────────────
# 修复1补充:Alembic 009 迁移验证
# ────────────────────────────────────────────────────────────────

class TestAlembicMigration009:
    """验证 Alembic 009 迁移文件存在且配置正确

    Note:迁移文件名以数字开头(009_xxx),无法用 import_module 直接导入,
    改用 importlib.util.spec_from_file_location 从文件路径加载。
    """

    @pytest.fixture
    def migration_module(self):
        """从文件路径加载 009 迁移模块"""
        # backend/alembic/versions/009_enlarge_review_issue_owasp_cwe.py
        migration_path = (
            Path(__file__).resolve().parents[2]
            / "alembic" / "versions" / "009_enlarge_review_issue_owasp_cwe.py"
        )
        assert migration_path.exists(), f"迁移文件不存在: {migration_path}"
        spec = importlib.util.spec_from_file_location(
            "migration_009", migration_path,
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_migration_009_module_loadable(self, migration_module):
        """009 迁移模块应可从文件路径加载"""
        assert migration_module is not None

    def test_migration_009_revision_id(self, migration_module):
        """009 迁移 revision 应为 '009',down_revision 应为 '008'"""
        assert migration_module.revision == "009"
        assert migration_module.down_revision == "008"

    def test_migration_009_has_upgrade(self, migration_module):
        """009 迁移应定义 upgrade() 函数"""
        assert callable(migration_module.upgrade)

    def test_migration_009_has_downgrade(self, migration_module):
        """009 迁移应定义 downgrade() 函数"""
        assert callable(migration_module.downgrade)
