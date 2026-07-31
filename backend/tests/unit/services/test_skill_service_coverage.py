"""Skill 服务的记录、审计、异常与查询路径补充测试。"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.agents.base import AgentContext
from app.agents.skills.base import SkillResult
from app.models.agent_governance import AgentProfile
from app.models.agent_skill_record import AgentSkillRecord
from app.services import skill_service as module


def _registry_with(skill: Any) -> MagicMock:
    """构造返回指定 Skill 的注册中心；参数 skill 可为 None，返回 MagicMock。"""
    return MagicMock(get=MagicMock(return_value=skill))


def _record(
    db: Any,
    *,
    agent_name: str = "reviewer",
    skill_name: str = "reviewer.self_improve",
    trigger_type: str = "manual",
    effect: str = "success",
    created_at: Any = None,
) -> AgentSkillRecord:
    """创建 Skill 调用记录；参数控制过滤字段，返回已持久化记录。"""
    record = AgentSkillRecord(
        agent_name=agent_name,
        skill_name=skill_name,
        trigger_type=trigger_type,
        trigger_source="test",
        input_params="{}",
        output_summary="done",
        effect=effect,
        duration_ms=10,
        create_time=created_at or datetime.utcnow(),
    )
    db.add(record)
    db.commit()
    return record


def test_truncate_and_output_summary_handle_empty_short_long_and_cycles() -> None:
    """摘要 helper 应处理空文本、短文本、长文本、JSON 数据与循环对象。"""
    assert module._truncate("") == ""
    assert module._truncate("abc", 3) == "abc"
    assert module._truncate("abcdef", 5) == "ab..."
    assert module._build_output_summary({"ok": "成功"}) == '{"ok": "成功"}'

    cyclic: list[Any] = []
    cyclic.append(cyclic)
    assert module._build_output_summary(cyclic) == "[[...]]"


def test_missing_skill_writes_failed_record(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """不存在的 Skill 应返回失败并写入含操作者的记录。"""
    registry = _registry_with(None)
    monkeypatch.setattr(module.SkillRegistry, "instance", MagicMock(return_value=registry))
    user = SimpleNamespace(id=7)

    result = module.invoke_skill_with_record(
        db,
        "reviewer",
        "missing.skill",
        {"path": "a.py"},
        trigger_type="event",
        trigger_source="event:test",
        user=user,
    )

    assert result["success"] is False
    assert result["effect"] == "failed"
    assert result["record_id"] is not None
    assert "不存在" in result["error"]
    record = db.get(AgentSkillRecord, result["record_id"])
    assert record.agent_name == "reviewer"
    assert record.skill_name == "missing.skill"
    assert record.created_by_user_id == 7
    assert '"path": "a.py"' in record.input_params


def test_disabled_agent_blocks_skill_before_registry_execution(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停用 Agent 后，手动、定时和事件 Skill 都必须在执行前被阻断。"""
    db.add(AgentProfile(
        code="reviewer",
        name="审查 Agent",
        category="quality",
        status="disabled",
        is_enabled=0,
    ))
    db.commit()
    skill = SimpleNamespace(run=MagicMock(return_value=SkillResult(success=True, effect="success")))
    registry = _registry_with(skill)
    monkeypatch.setattr(module.SkillRegistry, "instance", MagicMock(return_value=registry))

    result = module.invoke_skill_with_record(
        db,
        "reviewer",
        "reviewer.self_improve",
        {"action": "evolve"},
        trigger_type="scheduled",
    )

    assert result["success"] is False
    assert result["effect"] == "failed"
    assert "已停用" in result["error"]
    assert result["record_id"] is not None
    registry.get.assert_not_called()
    skill.run.assert_not_called()


def test_successful_manual_skill_injects_db_records_result_and_audits(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手动 Skill 成功时应注入 DB、记录结果并写成功审计。"""
    from app.services import audit_service

    skill = SimpleNamespace(
        run=MagicMock(
            return_value=SkillResult(
                success=True,
                data={"proposal_id": 9},
                effect="proposal_created",
            )
        )
    )
    registry = _registry_with(skill)
    audit = MagicMock()
    monkeypatch.setattr(module.SkillRegistry, "instance", MagicMock(return_value=registry))
    monkeypatch.setattr(audit_service, "log", audit)
    user = SimpleNamespace(id=1, username="admin")
    ctx = AgentContext(user_id=1, extra={"trace_id": "trace-1"})

    result = module.invoke_skill_with_record(
        db,
        "reviewer",
        "reviewer.self_improve",
        {"action": "evolve"},
        trigger_type="manual",
        trigger_source="api",
        user=user,
        ctx=ctx,
    )

    call_params, call_ctx = skill.run.call_args.args
    assert call_params["action"] == "evolve"
    assert call_params["_db"] is db
    assert call_ctx is ctx
    assert result["success"] is True
    assert result["data"] == {"proposal_id": 9}
    assert result["effect"] == "proposal_created"
    record = db.get(AgentSkillRecord, result["record_id"])
    assert record.output_summary == '{"proposal_id": 9}'
    assert record.created_by_user_id == 1
    audit.assert_called_once()
    assert audit.call_args.kwargs["action"] == "skill_invoke"
    assert audit.call_args.kwargs["status"] == "success"
    assert audit.call_args.kwargs["commit"] is True


def test_skill_exception_and_audit_failure_are_contained(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skill 与审计同时异常时主流程仍应返回失败并保存调用记录。"""
    from app.services import audit_service

    skill = SimpleNamespace(run=MagicMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(module.SkillRegistry, "instance", MagicMock(return_value=_registry_with(skill)))
    monkeypatch.setattr(audit_service, "log", MagicMock(side_effect=RuntimeError("audit down")))

    result = module.invoke_skill_with_record(
        db,
        "reviewer",
        "reviewer.self_improve",
        {},
        trigger_type="manual",
        user=SimpleNamespace(id=1),
    )

    assert result["success"] is False
    assert result["effect"] == "failed"
    assert result["data"] is None
    assert "Skill 调用异常: boom" == result["error"]
    assert result["record_id"] is not None
    record = db.get(AgentSkillRecord, result["record_id"])
    assert record.effect == "failed"
    assert record.output_summary == "Skill 调用异常: boom"


def test_record_write_failure_rolls_back_without_losing_skill_result(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调用记录提交失败时应回滚，并仍返回 Skill 的业务结果。"""
    skill = SimpleNamespace(run=MagicMock(return_value=SkillResult(success=True, data="ok", effect="success")))
    monkeypatch.setattr(module.SkillRegistry, "instance", MagicMock(return_value=_registry_with(skill)))
    commit = MagicMock(side_effect=RuntimeError("db down"))
    rollback = MagicMock(wraps=db.rollback)
    monkeypatch.setattr(db, "commit", commit)
    monkeypatch.setattr(db, "rollback", rollback)

    result = module.invoke_skill_with_record(
        db,
        "reviewer",
        "reviewer.self_improve",
        {},
        trigger_type="event",
    )

    assert result["success"] is True
    assert result["data"] == "ok"
    assert result["record_id"] is None
    rollback.assert_called_once()


def test_list_recent_records_applies_filters_order_and_limit_validation(
    db: Any,
) -> None:
    """记录查询应应用全部过滤条件、倒序返回并把越界 limit 恢复为 10。"""
    older = _record(db, created_at=datetime.utcnow() - timedelta(days=1))
    newest = _record(db, effect="proposal_created", created_at=datetime.utcnow())
    _record(db, agent_name="other", trigger_type="event")
    _record(db, skill_name="reviewer.proactive", trigger_type="proactive")

    filtered = module.list_recent_records(
        db,
        agent_name="reviewer",
        skill_name="reviewer.self_improve",
        trigger_type="manual",
        limit=0,
    )

    assert [item["id"] for item in filtered] == [newest.id, older.id]
    assert filtered[0]["effect"] == "proposal_created"
    assert filtered[0]["success"] is False
    assert filtered[0]["trigger_source"] == "test"
    assert filtered[0]["create_time"] is not None

    all_rows = module.list_recent_records(db, limit=101)
    assert len(all_rows) == 4
