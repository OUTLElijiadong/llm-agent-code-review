"""运维诊断必须处理全部事实来源，不能只发送开头或把缺失当成功。"""

import json
from unittest.mock import Mock

from app.agents.base import AgentResult
from app.agents.operations_agent import OperationsAgent
from app.core.config import settings


def test_long_operations_facts_are_source_compacted_before_diagnosis(db, monkeypatch):
    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 12_000)
    agent = OperationsAgent()
    agent._log_call = Mock()

    def allow_single_pass_compaction(message, **_kwargs):
        data = json.loads(message)
        if "source_manifest_sha256" in data:
            return message, False
        if "logs" in data:
            return message, True
        return message, False

    monkeypatch.setattr(agent, "_project_input", allow_single_pass_compaction)
    source_parts = []

    def compact(message, *_args, **_kwargs):
        data = json.loads(message)
        source_parts.append(data)
        return AgentResult(success=True, data={
            "source_id": data["source_id"],
            "summary": f"已读取来源 {data['part']}/{data['total_parts']}。" + "详情" * 70,
            "quote": data["content"][:20],
        })

    agent.call_json = Mock(side_effect=compact)
    agent.call = Mock(return_value=AgentResult(success=True, data="异常位于末尾"))
    facts = {"logs": "正常运行。" * 15_000 + "最后发现磁盘只读异常。"}
    result = agent.diagnose(db, None, facts, "trace-ops")

    assert result.success
    assert len(source_parts) > 2
    assert "".join(part["content"] for part in source_parts) == json.dumps(
        facts, ensure_ascii=False, default=str,
    )
    payload = json.loads(agent.call.call_args.args[0])
    assert payload["covered_source_ids"] == [part["source_id"] for part in source_parts]
    assert payload["source_summaries"]
    assert all(record.get("quote") for record in payload["source_summaries"])


def test_operations_fails_closed_instead_of_second_pass_summary_rewrite(db, monkeypatch):
    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 12_000)
    agent = OperationsAgent()
    agent._log_call = Mock()

    def keep_over_budget(message, *, system_prompt=None, **_kwargs):
        if system_prompt:
            return message, False
        return message, True

    monkeypatch.setattr(agent, "_project_input", keep_over_budget)

    def compact(message, *_args, **_kwargs):
        data = json.loads(message)
        return AgentResult(success=True, data={
            "source_id": data["source_id"], "summary": "原始来源摘要。",
            "quote": data["content"][:20],
        })

    agent.call_json = Mock(side_effect=compact)
    agent.call = Mock()
    result = agent.diagnose(db, None, {"logs": "服务运行正常。" * 15_000}, "trace-over-budget")

    assert result.success is False
    assert result.failure_kind == "semantic_budget_exhausted"
    assert "二次改写" in str(result.error)
    assert agent.call_json.call_count > 1
    assert all(not isinstance(json.loads(call.args[0]), list) for call in agent.call_json.call_args_list)
    agent.call.assert_not_called()


def test_operations_rejects_unsupported_source_summary(db, monkeypatch):
    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 12_000)
    agent = OperationsAgent()
    agent.call_json = Mock(side_effect=lambda message, *_args, **_kwargs: AgentResult(
        success=True,
        data={"source_id": json.loads(message)["source_id"],
              "summary": "声称完整", "quote": "原始输入没有这段证据"},
    ))
    agent.call = Mock()
    result = agent.diagnose(db, None, {"logs": "x" * 10_000}, "trace-ops")
    assert result.success is False
    assert result.failure_kind == "invalid_summary"
    agent.call.assert_not_called()
