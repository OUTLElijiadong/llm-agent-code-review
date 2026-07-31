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
        self.audit_calls = []

    def list_projects(self, *a, **k):
        return AgentResult(
            success=True,
            data={"total": len(self._items), "items": self._items},
        )

    def audit_security_for_project(self, **kw):
        # 记录被 handler 传入的参数,验证 None 字段被安全兜底
        self.audit_calls.append(kw)
        return AgentResult(
            success=True,
            data={"findings": [], "risk_score": 100, "summary": "ok"},
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


def test_int_or_none_safe():
    agent = ChatAssistantAgent()
    assert agent._int_or(None, 50) == 50
    assert agent._int_or("", 50) == 50
    assert agent._int_or("7", 50) == 7
    assert agent._int_or(7, 50) == 7
    assert agent._int_or("bad", 50) == 50


def test_security_audit_handler_survives_null_payload():
    """回归:意图分类器把 top_n/scan_depth/trace_dataflow 填成 null 时不得 500。

    复现线上 clarify 提交 500(int(None) TypeError)。
    """
    agent = _agent()
    intent = {
        "intent": "security_audit",
        "payload": {
            "scope": "project", "project_id": 3, "project_query": "皮卡丘",
            "task_id": None, "file_id": None, "scan_depth": None,
            "top_n": None, "trace_dataflow": None,
        },
    }
    result = agent._handle_security_audit(intent, ctx=None)
    assert result.success is True                       # 不再抛 TypeError
    call = agent._orchestrator.audit_calls[-1]
    assert call["project_id"] == 3
    assert call["top_n"] == 50                          # None → 默认 50
    assert call["trace_dataflow"] is True               # None → 默认 True


def test_dispatch_with_payload_null_fields_no_crash():
    """dispatch_with_payload 回填 null 字段后走到 handler 不崩。"""
    agent = _agent()
    result = agent.dispatch_with_payload(
        "security_audit",
        {"scope": "project", "project_id": 5, "top_n": None,
         "scan_depth": None, "trace_dataflow": None},
        ctx=None,
    )
    assert result.success is True


def test_dispatch_custom_project_name_returns_fuzzy_confirmation():
    """前端选择“其他”提交项目名称后，应进入后端模糊确认而非强转 ID。"""
    agent = _agent()

    result = agent.dispatch_with_payload(
        "security_audit",
        {
            "scope": "project",
            "project_id": "",
            "project_query": "皮卡丘",
        },
        ctx=None,
    )

    assert result.success is True
    clarify = result.data["clarify"]
    assert clarify["questions"][0]["key"] == "project_id"
    assert clarify["questions"][0]["default"] == 3
    assert "对吗" in result.data["content"]
