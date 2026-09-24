"""已完成圆桌的限时追问：完整账本压缩、串行回答、重连续答。"""
from __future__ import annotations

import asyncio

from loguru import logger

from app.agents.discussion_bus import DiscussionBus, DiscussionSession
from app.agents.events import DiscussionTurn
from app.ai.deepseek_agent import DeepSeekOutputTruncatedError, _clamp_max_tokens
from app.core.config import settings

_workers: dict[str, asyncio.Task] = {}


def _all_turns(bus: DiscussionBus, session: DiscussionSession) -> list[DiscussionTurn]:
    """从账号归属账本翻完所有页；序号不连续时拒绝生成貌似完整的回答。"""
    pages: list[list[DiscussionTurn]] = []
    before: int | None = None
    while True:
        page = bus.get_turns_page(
            session.session_id, session.owner_user_id, limit=100, before_seq=before,
        )
        rows = page["turns"]
        if not rows:
            break
        pages.append(rows)
        if not page["has_more"]:
            break
        before = page["next_before_seq"]
        if not before:
            raise RuntimeError("圆桌历史分页缺少下一页游标")
    turns = [turn for page in reversed(pages) for turn in page]
    if [turn.seq for turn in turns] != list(range(1, session.last_turn_seq + 1)):
        raise RuntimeError("圆桌发言账本不连续，不能生成追问回答")
    return turns


def _pending_questions(turns: list[DiscussionTurn], start_seq: int) -> list[DiscussionTurn]:
    answered = {
        turn.turn_id for turn in turns
        if turn.agent_code == "orchestrator" and turn.round_index == -1
    }
    return [
        turn for turn in turns
        if turn.seq > start_seq and turn.role == "user" and turn.seq not in answered
    ]


def _answer(session: DiscussionSession, turns: list[DiscussionTurn], question: DiscussionTurn) -> str:
    """原始发言全量送入有来源校验的语义压缩，保留提问原文。"""
    from app.ai.discussion_orchestrator import (
        _build_discussion_agents,
        _call_raw_for_task,
        _compress_roundtable_history,
        _roundtable_history_records,
        _roundtable_input_budget_error,
    )

    agent, _ = _build_discussion_agents(session.owner_user_id, ())
    records = _roundtable_history_records(turns)
    ceiling = _clamp_max_tokens(settings.deepseek_max_output_tokens)
    output_tokens = min(8192, ceiling)
    context_tokens = min(12000, max(1024, settings.deepseek_context_window_tokens // 4))
    history = _compress_roundtable_history(
        records, agent=agent, task_id=session.report_task_id,
        user_id=session.owner_user_id, file_id=session.file_id,
        target_tokens=context_tokens,
    )
    system_prompt = (
        "你是已完成代码审查圆桌的主持人。根据附有来源编号的完整历史投影"
        "回答当前账号的追问。保留前文中的用户要求、反驳、代码位置和证据；"
        "不能凭空声称重新审查了代码、调用了工具或修改了已完成的报告。"
        "若证据不足，明确指出缺口。回答清晰、适度简洁，引用相关来源编号。"
    )
    user_prompt = (
        f"圆桌文件：{session.file_name}\n"
        f"完整历史的来源压缩投影：\n{history}\n\n"
        f"当前追问（发言序号 {question.seq}，原文）：\n{question.content}"
    )
    budgets = list(dict.fromkeys((output_tokens, min(16384, ceiling), min(32768, ceiling))))
    if len(budgets) == 1:
        budgets.append(budgets[0])  # 旧模型预算固定时，改用更简短的回答要求重试一次。
    for attempt, budget in enumerate(budgets):
        prompt = system_prompt + (
            "本次重试请压缩表述，优先保留结论、关键依据和来源编号，避免冗长逐字复述。"
            if attempt else ""
        )
        budget_error = _roundtable_input_budget_error(
            prompt, user_prompt, max_output_tokens=budget,
        )
        if budget_error:
            raise RuntimeError(budget_error)
        try:
            reply, _ = _call_raw_for_task(
                agent, session.report_task_id, session.owner_user_id,
                usage_file_id=session.file_id,
                usage_chunk_index=9300 + question.seq * 3 + attempt,
                system_prompt=prompt, user_prompt=user_prompt,
                agent_label="general", json_mode=False, max_tokens=budget,
            )
        except DeepSeekOutputTruncatedError:
            if attempt + 1 == len(budgets):
                raise
            continue
        if not reply.strip():
            raise RuntimeError("模型返回空回答")
        return reply.strip()
    raise RuntimeError("圆桌追问未能生成完整回答")


async def _drain(bus: DiscussionBus, session_id: str, owner_user_id: int) -> None:
    """顺序处理已持久化的追问；问在期限内接受，答可在期限后完成。"""
    while True:
        session = bus.get_session(session_id, owner_user_id=owner_user_id)
        if session is None or not bus.followup_until(session):
            return
        try:
            turns = _all_turns(bus, session)
        except Exception:
            logger.exception("圆桌追问账本读取失败 session={}", session_id)
            return
        pending = _pending_questions(
            turns, int(session.progress.get("followup_start_seq") or 0),
        )
        if not pending:
            return
        question = pending[0]
        try:
            reply = await asyncio.to_thread(_answer, session, turns, question)
        except Exception as exc:
            logger.exception("圆桌追问生成失败 session={} seq={}", session_id, question.seq)
            reply = f"这条追问未能得到完整回答（{type(exc).__name__}）。原问题已保存，请重新发送以重试。"
        try:
            bus.publish_turn(session_id, DiscussionTurn(
                turn_id=question.seq, agent_code="orchestrator",
                agent_name="主持人", role="agent", content=reply,
                reply_to="user", round_index=-1,
            ))
        except Exception:
            logger.exception("圆桌追问回答落库失败 session={} seq={}", session_id, question.seq)
            return


def ensure_followup_worker(bus: DiscussionBus, session_id: str, owner_user_id: int) -> None:
    """后台任务独立于 WebSocket；重连会继续未回答的已接受追问。"""
    session = bus.get_session(session_id, owner_user_id=owner_user_id)
    if session is None or not bus.followup_until(session):
        return
    existing = _workers.get(session_id)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(_drain(bus, session_id, owner_user_id))
    _workers[session_id] = task

    def done(completed: asyncio.Task) -> None:
        if _workers.get(session_id) is completed:
            _workers.pop(session_id, None)
        if not completed.cancelled():
            try:
                completed.result()
            except Exception:
                logger.exception("圆桌追问后台任务异常 session={}", session_id)

    task.add_done_callback(done)


async def resume_pending_followups(bus: DiscussionBus | None = None) -> int:
    """进程重启后续答已接受的问题，不要求用户保持或重建 WebSocket。"""
    from app.core.database import SessionLocal
    from app.models.roundtable import RoundtableSession

    current_bus = bus or DiscussionBus.instance()

    def candidates() -> list[tuple[str, int]]:
        # 先只读取元数据；正式发言仍由 _all_turns 按归属分页复核。
        with SessionLocal() as db:
            return [
                (str(row.session_id), int(row.owner_user_id))
                for row in db.query(RoundtableSession).filter(
                    RoundtableSession.status == "concluded",
                    RoundtableSession.last_turn_seq > 0,
                ).yield_per(100)
                if row.report_task_id
                and (row.progress or {}).get("phase") == "completed"
                and "followup_start_seq" in (row.progress or {})
                and row.last_turn_seq > int(row.progress["followup_start_seq"])
            ]

    pending = await asyncio.to_thread(candidates)
    resumed = 0
    for session_id, owner_user_id in pending:
        session = current_bus.get_session(session_id, owner_user_id=owner_user_id)
        if session is None:
            continue
        try:
            turns = await asyncio.to_thread(_all_turns, current_bus, session)
            if not _pending_questions(
                turns, int(session.progress.get("followup_start_seq") or 0),
            ):
                continue
        except Exception:
            logger.exception("圆桌待续答账本检查失败 session={}", session_id)
            continue
        ensure_followup_worker(current_bus, session_id, owner_user_id)
        resumed += 1
    return resumed
