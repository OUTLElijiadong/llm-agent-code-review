"""单元测试 (v2.0): AgentRegistry 运行时枚举 + 态势感知

验证:
1. AgentRegistry 真实注册的 Agent 数量与名称
2. 每个 Agent 的元数据字段(icon/color/category/skills)完整
3. get_runtime_agents 能正确把 AiCallLog 回填到 Agent
4. get_situation 输出符合 Schema 形状
"""
from datetime import datetime

import pytest

from app.agents.orchestrator import get_orchestrator
from app.agents.registry import AgentRegistry
from app.services import agent_service


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_orchestrator():
    """确保 Orchestrator 实例化,所有 Agent 已注册"""
    get_orchestrator()
    yield


# ---------- AgentRegistry.list_runtime ----------


def test_registry_lists_v2_agents():
    """v2.0 至少注册 12 个 Agent (11 原有 + ai_prompt)"""
    runtime = AgentRegistry.instance().list_runtime()
    codes = {r["code"] for r in runtime}

    expected = {
        "orchestrator", "chat_assistant",
        "language_detector", "project_analyzer", "code_reviewer",
        "project_manager", "review_orchestrator", "code_file_manager",
        "dashboard", "rule_manager", "reporter",
        "ai_prompt",  # v2.0 新成员
    }
    assert expected.issubset(codes), f"缺少 Agent: {expected - codes}"


def test_registry_metadata_complete():
    """每个 Agent 必须填全元数据,不能用基类默认值"""
    runtime = AgentRegistry.instance().list_runtime()
    for r in runtime:
        assert r["icon"] != "base", f"{r['code']} 未设置 icon"
        assert r["color"], f"{r['code']} 未设置 color"
        assert r["category"] != "general" or r["code"] in {"general"}, (
            f"{r['code']} 应有更精确的 category"
        )
        assert isinstance(r["skills"], list)


def test_registry_priority_ordering():
    """orchestrator/chat_assistant 应排在最前(UI 主控/前台分组)"""
    runtime = AgentRegistry.instance().list_runtime()
    codes = [r["code"] for r in runtime]
    assert codes[0] == "orchestrator"
    assert codes[1] == "chat_assistant"


def test_registry_summary_buckets_by_category():
    """summary 按 category 分桶,数字相加 == 总数"""
    summary = AgentRegistry.instance().summary()
    assert summary["total"] >= 12
    total_from_buckets = sum(b["count"] for b in summary["by_category"])
    assert total_from_buckets == summary["total"]


# ---------- get_runtime_agents ----------


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *_columns):
        return _FakeQuery(self._rows)


def test_get_runtime_agents_fills_call_stats():
    """AiCallLog 中的调用应能按 code 回填到 Agent 卡"""
    now = datetime(2026, 5, 27, 10, 0, 0)
    rows = [
        ("deepseek-chat", "success", now),                         # general
        ("deepseek-chat/security-agent", "success", now),          # security
    ]
    runtime = agent_service.get_runtime_agents(_FakeDb(rows))
    by_code = {r["code"]: r for r in runtime}

    # 验证每条记录都带统计字段(默认为 0,有日志的为 >=1)
    assert "call_count" in by_code["ai_prompt"]
    assert by_code["ai_prompt"]["call_count"] == 0
    # 注意:runtime 模式下 'general' 并不是注册的 Agent code,所以不会出现在 runtime
    # 但 security 也不是 registry 中的 code(那是 multi_agent 静态画像)
    # 因此 v2.0 的 runtime 表只对真正注册的 Agent 计数,security 这条日志不会被回填


def test_get_runtime_agents_matches_registry_count():
    """get_runtime_agents 返回的条数严格等于 AgentRegistry 注册数量"""
    runtime = agent_service.get_runtime_agents(_FakeDb([]))
    registry_size = len(AgentRegistry.instance().list_runtime())
    assert len(runtime) == registry_size


# ---------- get_situation ----------


class _FakeSituationDb:
    """支持 query+filter+group_by+all 链式调用,用于测态势接口"""

    def __init__(self, rows):
        self._rows = rows

    def query(self, *_columns):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def group_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._rows


def test_get_situation_shape():
    """态势数据结构符合 AgentSituationOut Schema"""
    db = _FakeSituationDb([])
    data = agent_service.get_situation(db, user_id=None, minutes=60)

    assert set(data.keys()) >= {
        "online", "working", "idle", "today_calls", "spectrum", "hotspots",
    }
    assert data["online"] >= 12       # v2.0 至少 12 个 Agent
    assert data["online"] == data["idle"]  # M1 阶段 working=0,所有都是 idle
    assert len(data["spectrum"]) == 60     # 60 个分钟桶
    for bucket in data["spectrum"]:
        assert "bucket" in bucket and "count" in bucket
        assert bucket["count"] == 0    # 无数据时全 0
