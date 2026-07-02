"""单元测试 (v3.1): Agent 助手按项目名/昵称模糊解析 + 命中后一句确认。

覆盖用户反馈:
  - 「帮我检查皮卡丘漏洞」应模糊命中项目「皮卡丘商城」,并在执行前确认。
  - 缺 project_id 且无法确信时,下拉候选永不为空(不再给空选项)。
"""
from app.agents.base import AgentResult
from app.agents.chat_agent import ChatAssistantAgent
from app.agents.clarify_store import ClarifyStore


class _FakeOrch:
    def __init__(self, items):
        self._items = items

    def list_projects(self, *a, **k):
        return AgentResult(
            success=True,
            data={"total": len(self._items), "items": self._items},
        )


_ITEMS = [
    {"id": 3, "project_name": "皮卡丘商城", "language": "python",
     "status": "active", "file_count": 12},
    {"id": 5, "project_name": "订单中心", "language": "java",
     "status": "active", "file_count": 8},
    {"id": 7, "project_name": "Pikachu API", "language": "go",
     "status": "active", "file_count": 4},
]


def _agent(items=_ITEMS):
    ClarifyStore._instance = None
    agent = ChatAssistantAgent()
    agent.set_orchestrator(_FakeOrch(items))
    return agent


def test_resolve_nickname_hits_full_name():
    """昵称「皮卡丘」命中全名「皮卡丘商城」且确信。"""
    agent = _agent()
    best, candidates, confident = agent._resolve_project("帮我检查皮卡丘漏洞", None)
    assert confident is True
    assert best is not None and best["id"] == 3
    # 候选里第一个应是命中项目
    assert candidates[0]["value"] == 3


def test_confident_match_becomes_confirmation_with_default():
    """命中确信 → 追问预填 default,消息是一句确认。"""
    agent = _agent()
    result = agent._maybe_clarify(
        "security_audit", {"scope": "project", "project_query": "皮卡丘"},
        ctx=None, user_message="帮我检查皮卡丘漏洞",
    )
    assert result is not None
    data = result.data
    assert "对吗" in data["content"]          # 一句确认
    q = data["clarify"]["questions"][0]
    assert q["key"] == "project_id"
    assert q.get("default") == 3               # 预填命中项目
    assert q.get("options")                    # 下拉带候选,永不为空


def test_ambiguous_still_populates_options():
    """无法确信时不预填,但候选下拉仍非空。"""
    agent = _agent()
    result = agent._maybe_clarify(
        "security_audit", {"scope": "project"},
        ctx=None, user_message="随便看看",
    )
    assert result is not None
    q = result.data["clarify"]["questions"][0]
    assert q["key"] == "project_id"
    assert q.get("default") is None
    assert len(q.get("options") or []) >= 1    # 关键:不再是空下拉


def test_no_orchestrator_falls_back_gracefully():
    """无 orchestrator(如纯单测)时不炸,退回普通 select_project。"""
    ClarifyStore._instance = None
    agent = ChatAssistantAgent()                # 未注入 orchestrator
    result = agent._maybe_clarify("delete_project", {}, ctx=None)
    assert result is not None
    q = result.data["clarify"]["questions"][0]
    assert q["key"] == "project_id"
    assert q["type"] == "select_project"
