"""AC2 修复验证:ai_call_log.agent_label 落库测试

验证 DeepSeekAgent 的三个日志写入路径都正确写入 agent_label:
1. log_deferred() — 协作模式阶段1(call_raw + log_deferred)使用
2. _log()         — chat() 同步路径使用
3. _log_sequential_call() — 顺序模式(BaseAgent.call)使用

测试覆盖:
- agent_label 正确写入 AiCallLog.agent_label 字段
- meta 中无 agent_label 时字段为 None(兼容历史)
- 不同 agent_label 值(code_reviewer/security_sentinel/cross_review/consensus)均能落库
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.ai.deepseek_agent import DeepSeekAgent
from app.models.ai_call_log import AiCallLog

# ── log_deferred 路径(协作模式阶段1)──


def test_log_deferred_writes_agent_label_code_reviewer(db):
    """log_deferred 应将 meta.agent_label 写入 AiCallLog.agent_label(code_reviewer)"""
    meta = {
        "agent_label": "code_reviewer",
        "model_name": "deepseek-chat",
        "model_tag": "deepseek-chat/code_reviewer-agent",
        "user_prompt": "审查代码...",
        "response": '{"issues": []}',
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "duration_ms": 800,
        "create_time": datetime.now(timezone.utc),
    }
    DeepSeekAgent.log_deferred(
        db, task_id=1, user_id=1, file_id=1, chunk_index=0,
        meta=meta, status="success", error=None,
    )
    db.commit()

    row = db.query(AiCallLog).filter(AiCallLog.agent_label == "code_reviewer").first()
    assert row is not None
    assert row.agent_label == "code_reviewer"
    assert row.model_name == "deepseek-chat/code_reviewer-agent"
    assert row.prompt_tokens == 100
    assert row.completion_tokens == 50
    assert row.total_tokens == 150
    assert row.status == "success"


def test_log_deferred_writes_agent_label_security_sentinel(db):
    """log_deferred 应将 meta.agent_label 写入 AiCallLog.agent_label(security_sentinel)"""
    meta = {
        "agent_label": "security_sentinel",
        "model_name": "deepseek-chat",
        "model_tag": "deepseek-chat/security_sentinel-agent",
        "user_prompt": "安全扫描...",
        "response": '{"findings": []}',
        "prompt_tokens": 200,
        "completion_tokens": 100,
        "total_tokens": 300,
        "duration_ms": 1200,
        "create_time": datetime.now(timezone.utc),
    }
    DeepSeekAgent.log_deferred(
        db, task_id=2, user_id=1, file_id=2, chunk_index=0,
        meta=meta, status="success", error=None,
    )
    db.commit()

    row = db.query(AiCallLog).filter(AiCallLog.agent_label == "security_sentinel").first()
    assert row is not None
    assert row.agent_label == "security_sentinel"
    assert row.total_tokens == 300


def test_log_deferred_agent_label_null_when_meta_missing(db):
    """meta 中无 agent_label 时,AiCallLog.agent_label 应为 None(兼容历史数据)"""
    meta = {
        "model_name": "deepseek-chat",
        "model_tag": "deepseek-chat",
        "user_prompt": "...",
        "response": "...",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "duration_ms": 100,
        "create_time": datetime.now(timezone.utc),
    }
    DeepSeekAgent.log_deferred(
        db, task_id=3, user_id=1, file_id=3, chunk_index=0,
        meta=meta, status="success", error=None,
    )
    db.commit()

    row = db.query(AiCallLog).filter(AiCallLog.task_id == 3).first()
    assert row is not None
    assert row.agent_label is None


def test_log_deferred_failed_status_with_agent_label(db):
    """失败的调用也应记录 agent_label,便于统计 Agent 失败率"""
    meta = {
        "agent_label": "code_reviewer",
        "model_name": "deepseek-chat",
        "model_tag": "deepseek-chat/code_reviewer-agent",
        "user_prompt": "...",
        "response": "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "duration_ms": 50,
        "create_time": datetime.now(timezone.utc),
    }
    DeepSeekAgent.log_deferred(
        db, task_id=4, user_id=1, file_id=4, chunk_index=0,
        meta=meta, status="failed", error="DeepSeek 限流",
    )
    db.commit()

    row = db.query(AiCallLog).filter(AiCallLog.task_id == 4).first()
    assert row is not None
    assert row.agent_label == "code_reviewer"
    assert row.status == "failed"
    assert row.error_message == "DeepSeek 限流"


# ── _log 路径(chat 同步写入)──


def test_log_writes_agent_label_via_chat_path(db):
    """_log(chat 路径)应将 agent_label 写入 AiCallLog.agent_label"""
    meta = {
        "prompt_tokens": 80,
        "completion_tokens": 40,
        "total_tokens": 120,
        "duration_ms": 600,
    }
    DeepSeekAgent._log(
        db,
        task_id=10, user_id=1, file_id=10, chunk_index=0,
        prompt="审查 prompt", response='{"issues": []}',
        status="success", error=None, meta=meta,
        model_name="deepseek-chat/cross_review-agent",
        agent_label="cross_review",
    )
    db.commit()

    row = db.query(AiCallLog).filter(AiCallLog.task_id == 10).first()
    assert row is not None
    assert row.agent_label == "cross_review"
    assert row.model_name == "deepseek-chat/cross_review-agent"
    assert row.prompt_tokens == 80


def test_log_agent_label_none_when_not_provided(db):
    """_log 未传 agent_label 时,AiCallLog.agent_label 应为 None(兼容旧调用)"""
    meta = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "duration_ms": 100}
    DeepSeekAgent._log(
        db,
        task_id=11, user_id=1, file_id=11, chunk_index=0,
        prompt="...", response="...",
        status="success", error=None, meta=meta,
        model_name="deepseek-chat",
        agent_label=None,
    )
    db.commit()

    row = db.query(AiCallLog).filter(AiCallLog.task_id == 11).first()
    assert row is not None
    assert row.agent_label is None


# ── _log_sequential_call 路径(顺序模式补写)──


def _make_stub_task():
    """构造 task stub(避免依赖完整 ORM)"""
    task = MagicMock()
    task.id = 100
    return task


def _make_stub_user():
    """构造 user stub"""
    user = MagicMock()
    user.id = 1
    return user


def _make_stub_code_file():
    """构造 code_file stub"""
    code_file = MagicMock()
    code_file.id = 200
    code_file.file_name = "test.py"
    return code_file


def _make_stub_result(success=True, model="deepseek-chat", duration_ms=500):
    """构造 AgentResult stub"""
    result = MagicMock()
    result.success = success
    result.model = model
    result.duration_ms = duration_ms
    result.tokens = {"prompt": 100, "completion": 50, "total": 150}
    result.error = None if success else "调用失败"
    result.data = {"issues": [], "summary": "ok", "score": 90} if success else None
    return result


def test_log_sequential_call_success_writes_agent_label(db):
    """顺序模式成功路径:_log_sequential_call 应写入 agent_label=code_reviewer"""
    from app.services.review_service import _log_sequential_call

    task = _make_stub_task()
    user = _make_stub_user()
    code_file = _make_stub_code_file()
    result = _make_stub_result(success=True)

    _log_sequential_call(
        db, task, user, code_file,
        chunk_idx=0, agent_idx=0,
        agent_label="code_reviewer",
        result=result, status="success",
    )
    db.commit()

    row = db.query(AiCallLog).filter(AiCallLog.task_id == 100).first()
    assert row is not None
    assert row.agent_label == "code_reviewer"
    assert row.status == "success"
    assert row.prompt_tokens == 100
    assert row.completion_tokens == 50
    assert row.total_tokens == 150
    assert row.duration_ms == 500
    assert row.file_id == 200


def test_log_sequential_call_security_agent_label(db):
    """顺序模式 security 画像:_log_sequential_call 应写入 agent_label=security_sentinel"""
    from app.services.review_service import _log_sequential_call

    task = _make_stub_task()
    task.id = 101
    user = _make_stub_user()
    code_file = _make_stub_code_file()
    result = _make_stub_result(success=True, model="deepseek-chat")

    _log_sequential_call(
        db, task, user, code_file,
        chunk_idx=0, agent_idx=0,
        agent_label="security_sentinel",
        result=result, status="success",
    )
    db.commit()

    row = db.query(AiCallLog).filter(AiCallLog.task_id == 101).first()
    assert row is not None
    assert row.agent_label == "security_sentinel"


def test_log_sequential_call_failed_status(db):
    """顺序模式失败路径:_log_sequential_call 应写入 status=failed + agent_label"""
    from app.services.review_service import _log_sequential_call

    task = _make_stub_task()
    task.id = 102
    user = _make_stub_user()
    code_file = _make_stub_code_file()
    result = _make_stub_result(success=False)

    _log_sequential_call(
        db, task, user, code_file,
        chunk_idx=0, agent_idx=0,
        agent_label="code_reviewer",
        result=result, status="failed",
        error="LLM 超时",
    )
    db.commit()

    row = db.query(AiCallLog).filter(AiCallLog.task_id == 102).first()
    assert row is not None
    assert row.agent_label == "code_reviewer"
    assert row.status == "failed"
    assert row.error_message == "LLM 超时"


def test_log_sequential_call_result_none_on_exception(db):
    """顺序模式异常路径:result=None 时仍能写入 agent_label + status=failed"""
    from app.services.review_service import _log_sequential_call

    task = _make_stub_task()
    task.id = 103
    user = _make_stub_user()
    code_file = _make_stub_code_file()

    _log_sequential_call(
        db, task, user, code_file,
        chunk_idx=0, agent_idx=0,
        agent_label="security_sentinel",
        result=None, status="failed",
        error="Agent 执行异常",
    )
    db.commit()

    row = db.query(AiCallLog).filter(AiCallLog.task_id == 103).first()
    assert row is not None
    assert row.agent_label == "security_sentinel"
    assert row.status == "failed"
    assert row.error_message == "Agent 执行异常"
    assert row.prompt_tokens == 0
    assert row.duration_ms == 0
