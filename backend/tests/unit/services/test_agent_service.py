"""单元测试: Agent 中心服务

集中验证三件事:
1. 5 个画像按预期顺序导出
2. review_type → 代理映射与 multi_agent.get_agent_profiles 一致
3. get_usage 能正确把 model_name 解析回代理 (general / 专项 / multi-agent)
"""
from datetime import datetime

from app.services import agent_service


def test_list_profiles_returns_five_agents_in_fixed_order():
    """画像数量与顺序锁定;前端依赖 general 在最前展示"""
    profiles = agent_service.list_profiles()

    assert [p["code"] for p in profiles] == [
        "general", "security", "reliability", "performance", "maintainability",
    ]
    assert all(p["enabled"] for p in profiles)


def test_list_type_mappings_covers_all_review_types():
    """5 种审查类型都要有映射;agent_codes 必须非空"""
    mappings = agent_service.list_type_mappings()

    types = {m["review_type"] for m in mappings}
    assert types == {"quick", "standard", "security", "performance", "full"}
    for m in mappings:
        assert m["agent_codes"], f"{m['review_type']} 映射不能为空"


class _FakeQuery:
    """伪造的 SQLAlchemy Query: 支持 filter -> all"""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def query(self, *_columns):
        return _FakeQuery(self._rows)


def test_get_usage_buckets_calls_by_model_name():
    """通用代理 / 专项代理 / multi-agent 三种 model_name 都要能归位"""
    now = datetime(2026, 5, 27, 10, 0, 0)
    rows = [
        # 通用: 不带后缀 → general
        ("deepseek-chat", "success", now),
        # 专项: /security-agent → security
        ("deepseek-chat/security-agent", "success", now),
        ("deepseek-chat/security-agent", "failed", now),
        # multi-agent: 同时计入 security/reliability/performance/maintainability
        ("deepseek-chat/multi-agent", "success", now),
    ]
    usage = agent_service.get_usage(_FakeDb(rows))

    by_code = {u["code"]: u for u in usage}

    assert by_code["general"]["call_count"] == 1
    assert by_code["general"]["success_count"] == 1

    assert by_code["security"]["call_count"] == 3  # 2 直接 + 1 multi-agent
    assert by_code["security"]["success_count"] == 2
    assert by_code["security"]["failed_count"] == 1

    # multi-agent 同时计入其余三个
    for code in ("reliability", "performance", "maintainability"):
        assert by_code[code]["call_count"] == 1, code
        assert by_code[code]["success_count"] == 1, code
