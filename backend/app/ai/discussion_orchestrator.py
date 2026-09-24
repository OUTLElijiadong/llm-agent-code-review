"""多 Agent 讨论编排器 (v2.3 M7 / v2.4 B1 MetaGPT 接入)

发言式讨论模式: Agent 逐一轮流发言,每个 Agent 能看到之前所有人的发言内容
(含其他 Agent 和用户的发言),并对前面的观点进行评价、质疑、反驳。讨论收敛时
由主持人汇总共识,并把讨论结论沉淀为一份可在「报告列表」查询的审查报告。

v2.4 B1 MetaGPT 接入:
- 在 start_discussion 中构建 MetaGPT Environment,作为讨论消息总线层
- 每轮发言/用户输入/主持人汇总都通过 env.publish() 广播到 Environment
- Environment 自动通过 AgentEventBus 发布 DISCUSS 事件,前端 SSE 可见结构化消息流
- 保留现有发言循环与 LLM 调用(system_prompt 定制),Environment 为非破坏性上层编排

使用方式:
    orch = DiscussionOrchestrator()
    await orch.start_discussion(session_id, profiles, code, language, file_name,
                                user_id, project_id, file_id, review_type)

数据真实性:
- 每次 LLM 发言都通过 log_deferred() 写入 AiCallLog(带 task_id/user_id/file_id +
  agent_label),使 Agent 中心统计与「Agent 调用日志」反映真实讨论调用;
- 全程通过 AgentEventBus 广播 dispatch/thinking/complete 事件,Agent 办公室工位卡
  实时点亮;
- 讨论结束创建 ReviewTask(review_type=discuss)+ ReviewIssue,出现在报告/任务列表。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import traceback
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import Lock
from types import SimpleNamespace
from typing import Callable, Optional

from loguru import logger

from app.agents.discussion_bus import DiscussionBus
from app.agents.event_bus import AgentEventBus
from app.agents.events import (
    AgentEvent,
    AgentEventType,
    DiscussionTurn,
    new_trace_id,
)
from app.agents.metagpt import build_discussion_environment, make_discussion_message
from app.agents.metagpt.environment import Environment
from app.ai.deepseek_agent import DeepSeekAgent, DeepSeekOutputTruncatedError, _clamp_max_tokens
from app.ai.multi_agent import ReviewAgentProfile
from app.ai.result_parser import Issue, normalize_severity
from app.ai.result_parser import parse as parse_issues
from app.ai.scoring import SCORING_VERSION, compute_score_breakdown
from app.ai.static_analyzer import scan as static_scan
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import ValidationError
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.services.agent_model_service import resolve_subagent_config
from app.services.ai_usage_context import current_attribution, model_attribution, usage_context
from app.services.deepseek_responses_runtime import estimate_tokens
from app.services.issue_merger import merge_findings_and_issues
from app.services.review_input_service import freeze_task_inputs, validate_review_input
from app.utils.api_resolver import resolve_api_config

# 讨论画像 code → 注册中心 BaseAgent code(与 review_service 保持一致),
# 用于向 Agent 办公室广播事件时点亮正确的工位卡。
_PROFILE_TO_AGENT_CODE: dict[str, str] = {
    "general": "code_reviewer",
    "security": "security_sentinel",
    "reliability": "code_reviewer",
    "performance": "code_reviewer",
    "maintainability": "code_reviewer",
}

_VALID_DECISION_ACTIONS = {"speak", "silent"}
_VALID_DECISION_STANCES = {
    "propose", "agree", "oppose", "question", "supplement", "neutral",
}
_ACTION_ALIASES = {
    "发言": "speak", "说话": "speak", "静音": "silent", "不发言": "silent",
}
_STANCE_ALIASES = {
    "提出": "propose", "提议": "propose",
    "赞同": "agree", "同意": "agree",
    "否认": "oppose", "反对": "oppose", "不同意": "oppose", "反驳": "oppose",
    "质疑": "question", "提问": "question",
    "补充": "supplement", "中立": "neutral",
}
_SILENT_DEFAULT = "本轮没有新增证据或不同观点，选择静音。"
_EXTRACTION_INITIAL_OUTPUT_TOKENS = 16_384
_EXTRACTION_MAX_REQUESTS = 32
_EXTRACTION_CODE_WINDOW_CHARS = 16_000
_SPEAKER_MAX_WINDOWS = 64
_ROUNDTABLE_MAX_MODEL_CALLS = 64


class _RoundtableCallBudget:
    """同一圆桌会话的模型逻辑请求上限，跨线程共享计数。"""

    def __init__(self, limit: int = _ROUNDTABLE_MAX_MODEL_CALLS) -> None:
        self.limit = limit
        self.used = 0
        self._lock = Lock()

    def ensure_capacity(self, required: int) -> None:
        with self._lock:
            if self.used + required > self.limit:
                raise RuntimeError(
                    f"圆桌本会话模型调用预算最多 {self.limit} 次，已使用 {self.used} 次，"
                    f"后续至少还需 {required} 次；原始证据保留，本轮不能标记完成"
                )

    def reserve(self) -> None:
        with self._lock:
            if self.used >= self.limit:
                raise RuntimeError(
                    f"圆桌本会话模型调用预算最多 {self.limit} 次，已使用 {self.used} 次；"
                    "原始证据保留，不能继续调用模型"
                )
            self.used += 1


_roundtable_call_budget: ContextVar[_RoundtableCallBudget | None] = ContextVar(
    "roundtable_call_budget", default=None,
)


def _roundtable_input_budget_error(*parts: str, max_output_tokens: int) -> Optional[str]:
    """在送给模型前保守估算输入；超限时返回可解释错误，不裁掉原文。"""
    estimated = sum(estimate_tokens(part) for part in parts) + 1024
    available = settings.deepseek_context_window_tokens - max_output_tokens
    if estimated <= available:
        return None
    return (
        f"圆桌输入预估 {estimated} tokens，超过当前可用预算 {available} tokens；"
        "完整原始发言已保留，无法在不丢失审查证据的情况下直接调用模型"
    )


def _roundtable_history_records(turns: list[DiscussionTurn]) -> list[tuple[str, str]]:
    """把完整发言转成有稳定来源序号的压缩输入。"""
    records = []
    for ordinal, turn in enumerate(turns, start=1):
        role = "用户" if turn.role == "user" else turn.agent_name
        metadata = "" if turn.role == "user" else (
            f" 动作:{getattr(turn, 'action', 'speak')}"
            f" 立场:{getattr(turn, 'stance', 'neutral')}"
            f" 回应:{getattr(turn, 'reply_to', None) or '无'}"
        )
        records.append((
            f"S{ordinal:04d}-T{turn.turn_id}",
            f"【{role}·{turn.agent_code}#{turn.turn_id}{metadata}】{turn.content}",
        ))
    return records


def _build_discussion_agents(user_id: int, profiles):
    """短连接读取用户/平台配置，按真实注册角色创建此次圆桌的客户端。"""
    db = SessionLocal()
    try:
        config = resolve_api_config(db, user_id)
        names = {"code_reviewer"} | {_PROFILE_TO_AGENT_CODE.get(profile.code, profile.code) for profile in profiles}
        clients = {
            name: DeepSeekAgent(api_config=resolve_subagent_config(db, config, agent_name=name))
            for name in names
        }
        return clients["code_reviewer"], clients
    finally:
        db.close()


class _DiscussionInactive(RuntimeError):
    """任务不再可运行时中断模型调用链，保留原有终态。"""

    def __init__(self, status: str):
        self.status = status
        super().__init__(f"圆桌任务已结束或不可运行（{status}），停止后续模型调用")


def _review_task_state(task_id: int) -> dict:
    """用独立短连接读取当前状态，避免长事务和身份映射返回旧值。"""
    db = SessionLocal()
    try:
        task = db.query(ReviewTask).filter_by(id=task_id).populate_existing().one_or_none()
        if task is None:
            return {"status": "deleted", "error": "圆桌任务不存在"}
        return {"status": task.status, "error": task.error_message, "coverage": task.coverage}
    finally:
        db.close()


def _ensure_running(task_id: int) -> None:
    """每次启动新的模型阶段前确认任务仍可运行。"""
    state = _review_task_state(task_id)
    if state["status"] != "running":
        raise _DiscussionInactive(state["status"])


def _notify_origin_session(
    *,
    user_id: int,
    surface: str,
    session_key: str,
    discussion_session_id: str,
    file_name: str,
    report_task_id: int,
    status: str,
    summary: str,
) -> None:
    """讨论结束后把结论作为协作消息回投发起讨论的小菱会话。

    消息走 queued→delivered 生命周期,前端 MeshBridge 会自动拉起小菱续跑汇报,
    与子 Agent 团队结果回投保持一致;失败不阻断报告落库。
    """
    if not surface or not session_key:
        return
    from app.models.user import User
    from app.schemas.agent_mesh import AgentMeshMessageIn
    from app.services import agent_mesh_service

    db = SessionLocal()
    try:
        user = db.get(User, int(user_id))
        if user is None or int(getattr(user, "status", 0) or 0) != 1:
            return
        # Agent Mesh 单条 payload 有 256 KiB 上限。超限时回投报告引用，
        # 不悄悄截断总结；完整内容仍在 ReviewTask.summary 和圆桌记录中。
        summary_for_message = summary or ""
        if len(summary_for_message.encode("utf-8")) > 200_000:
            summary_for_message = (
                f"圆桌总结内容较长，完整内容已保存至审查报告 #{report_task_id}；"
                f"请按圆桌会话 {discussion_session_id} 查看原文。"
            )
        message = AgentMeshMessageIn.model_validate({
            "schema_version": "1.0",
            "idempotency_key": f"discussion-result:{discussion_session_id}",
            "trace_id": discussion_session_id,
            "correlation_id": discussion_session_id,
            "causation_id": "",
            "sent_from": "agent:orchestrator",
            "send_to": f"session:{surface}:{session_key}",
            "message_type": "task.result",
            "priority": "normal",
            "subject": f"圆桌讨论结束:{file_name}"[:240],
            "payload": {
                "discussion_session_id": discussion_session_id,
                "file_name": file_name,
                "report_task_id": int(report_task_id or 0),
                "status": status,
                "summary": summary_for_message,
            },
            "context": {"run_id": discussion_session_id},
            "artifacts": [],
            "errors": [],
            "delivery": {"requires_ack": True, "max_attempts": 3},
        })
        agent_mesh_service.send_message(
            db,
            user,
            surface=surface,
            session_key=session_key,
            message=message,
            trusted_source=True,
        )
    except Exception as exc:  # noqa: BLE001 - 回投失败只记录,不打断讨论收尾
        logger.warning(f"[Discussion] 结论回投小菱会话失败: {exc}")
    finally:
        db.close()


@dataclass(frozen=True)
class SpeakerDecision:
    """单个审查 Agent 在一轮中的自主决策。

    Attributes:
        action: `speak` 表示发言，`silent` 表示本轮静音。
        stance: 发言立场；静音时固定为 `neutral`。
        reply_to: 可选的回应目标 Agent code。
        content: 发言正文或静音原因。
    """

    action: str
    stance: str
    reply_to: Optional[str]
    content: str


def _strip_json_fence(raw: str) -> str:
    """移除模型响应外层 Markdown 代码围栏。

    Args:
        raw: DeepSeek 返回的原始文本。

    Returns:
        str: 可交给 JSON 解析器的候选文本。
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _recover_last_content_field(text: str) -> Optional[dict]:
    """只恢复 `content` 为末字段且边界完整的非标准 JSON 发言。

    模型偶尔在正文中直接写 `"a"` 或 `["data"]`，使整个对象无法被
    ``json.loads`` 解析。仅解析正文之前的合法 JSON 对象前缀，并原样取出
    `content` 起始引号与对象末尾闭合引号之间的正文；有后续字段或边界
    不完整时拒绝恢复。
    """
    marker = re.search(r'"content"\s*:\s*"', text)
    ending = re.search(r'"\s*}\s*$', text)
    if not (text.startswith("{") and marker and ending):
        return None
    body = text[marker.end():ending.start()]
    if not body or ending.start() <= marker.end():
        return None
    # 末尾的引号若被反斜杠转义，响应并没有完整的 content 闭合边界。
    slash_count = len(body) - len(body.rstrip("\\"))
    if slash_count % 2:
        return None
    # 此时无法区分额外字段与正文片段，保守失败而不是发布歧义文本。
    if re.search(r'",\s*"[^"\r\n]+"\s*:', body):
        return None
    try:
        prefix = json.loads(text[:marker.end()] + '"}')
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(prefix, dict)
        or prefix.get("content") != ""
        or set(prefix) - {"action", "stance", "reply_to", "content"}
    ):
        return None
    return {**prefix, "content": body}


def _parse_speaker_decision(raw: str) -> SpeakerDecision:
    """把模型响应规范化为可执行的发言或静音决策。

    Args:
        raw: 模型返回的 JSON 或兼容旧版本的纯文本发言。

    Returns:
        SpeakerDecision: 经过枚举校验和空值兜底的决策。旧版自然语言
        响应仍可作为纯文本发言；不完整的结构化响应不能算作有效发言。
    """
    text = _strip_json_fence(raw)
    data: object
    try:
        data = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        data = _recover_last_content_field(text)

    if not isinstance(data, dict) and text.startswith(("{", "[")):
        raise ValueError("模型返回结构化发言格式无效，未发布原始 JSON")

    if not isinstance(data, dict):
        if text:
            return SpeakerDecision("speak", "neutral", None, text)
        return SpeakerDecision("silent", "neutral", None, _SILENT_DEFAULT)

    action_raw = str(data.get("action") or "speak").strip().lower()
    action = _ACTION_ALIASES.get(action_raw, action_raw)
    if action not in _VALID_DECISION_ACTIONS:
        action = "speak"

    stance_raw = str(data.get("stance") or "neutral").strip().lower()
    stance = _STANCE_ALIASES.get(stance_raw, stance_raw)
    if stance not in _VALID_DECISION_STANCES:
        stance = "neutral"

    content_value = data.get("content")
    content = content_value.strip() if isinstance(content_value, str) else ""
    reply_value = data.get("reply_to")
    reply_to = reply_value.strip()[:64] if isinstance(reply_value, str) else None
    reply_to = reply_to or None

    if action == "silent" or not content:
        return SpeakerDecision(
            action="silent",
            stance="neutral",
            reply_to=None,
            content=content or _SILENT_DEFAULT,
        )
    return SpeakerDecision(
        action="speak",
        stance=stance,
        reply_to=reply_to,
        content=content,
    )


class DiscussionOrchestrator:
    """共享群聊编排器，每个 Agent 可发言、静音并回应其他参会者。"""

    def __init__(self):
        self._bus = DiscussionBus.instance()

    async def start_discussion(
        self,
        session_id: str,
        profiles: tuple[ReviewAgentProfile, ...],
        code: str,
        language: str,
        file_name: str,
        user_id: int,
        project_id: int,
        file_id: int,
        review_type: str = "full",
        max_rounds: int = 2,
        origin_surface: str = "",
        origin_session_key: str = "",
        continuation_context: str = "",
        continued_from_session_id: str = "",
        usage_origin: Optional[dict[str, int]] = None,
    ):
        bus = self._bus
        session = bus.get_session(session_id)
        if not session:
            logger.error(f"[Discussion] session={session_id} 不存在")
            return

        bus.set_controller(session_id, self._handle_control)

        self._session_id = session_id
        self._paused = False
        self._paused_event = asyncio.Event()
        self._user_inputs: list[str] = []
        self._user_id = user_id
        self._file_id = file_id
        self._task_id = 0
        self._origin_surface = str(origin_surface or "")[:24]
        self._origin_session_key = str(origin_session_key or "")[:128]
        self._continued_from_session_id = str(continued_from_session_id or "")[:64]
        self._trace_id = new_trace_id()
        all_turns: list[DiscussionTurn] = []
        self._all_turns = all_turns
        deferred_logs: list[dict] = []
        turn_counter = 0
        summary_text = ""
        loop = asyncio.get_running_loop()
        coverage = {
            "expected_turns": max_rounds * len(profiles), "attempted_turns": 0,
            "successful_turns": 0, "failed_turns": 0, "valid_speeches": 0,
            "silent_turns": 0, "summary_status": "pending", "extraction_status": "pending",
            "model_calls_limit": _ROUNDTABLE_MAX_MODEL_CALLS, "model_calls_used": 0,
            "errors": [],
        }
        progress_total = coverage["expected_turns"] + 3  # 汇总、至少一批抽取、报告落库

        def publish_progress(phase: str, completed: int, total: int, *,
                             current_round: int = 0, speaker_code: str = "") -> None:
            try:
                bus.publish_control(session_id, "progress", {
                    "phase": phase,
                    "completed_units": completed,
                    "total_units": total,
                    "current_round": current_round,
                    "speaker_code": speaker_code,
                })
            except Exception as exc:
                logger.warning(f"[Discussion] 进度发布失败: {exc}")

        def publish_progress_from_worker(phase: str, completed: int, total: int) -> None:
            try:
                loop.call_soon_threadsafe(publish_progress, phase, completed, total)
            except RuntimeError as exc:
                logger.warning(f"[Discussion] 进度回投失败: {exc}")
        try:
            validate_review_input(SimpleNamespace(content=code, file_name=file_name, is_binary=0))
            if not profiles or max_rounds <= 0:
                raise ValidationError("圆桌讨论必须包含审查专家和有效轮次", code=40001)
            agent, speaker_agents = await loop.run_in_executor(
                None, copy_context().run, lambda: _build_discussion_agents(user_id, profiles),
            )
        except Exception as exc:
            self._publish_terminal(session_id, 0, "failed", str(exc))
            return

        # ── v2.4 B1: 构建 MetaGPT Environment 作为讨论消息总线层 ──
        # Environment 接收所有发言/用户输入/主持人汇总的 Message,
        # 自动通过 AgentEventBus 发布 DISCUSS 事件到 SSE,前端可见结构化消息流。
        # 不调用 env.run(),避免与现有发言循环重复触发 LLM 调用。
        agent_codes = [
            _PROFILE_TO_AGENT_CODE.get(p.code, p.code) for p in profiles
        ]
        env: Optional[Environment] = None
        try:
            env = build_discussion_environment(
                trace_id=self._trace_id,
                user_id=user_id,
                project_id=project_id,
                file_id=file_id,
                agent_codes=list(dict.fromkeys(agent_codes)),  # 去重保序
                max_depth=max_rounds * len(profiles) + 4,
            )
            self._env = env
            logger.info(
                f"[Discussion] MetaGPT Environment 已构建: "
                f"roles={env.list_roles()}, trace_id={self._trace_id}",
            )
        except Exception as e:
            logger.warning(f"[Discussion] 构建 MetaGPT Environment 失败,降级为无总线模式: {e}")
            self._env = None

        # ── 讨论开始即创建 ReviewTask(running),拿到 task_id 供日志/问题/报告 ──
        task_id = 0
        cancelled = False
        usage_origin = usage_origin if usage_origin is not None else current_attribution(user_id)
        create_future = loop.run_in_executor(
            None, copy_context().run,
            lambda: _create_review_task(
                user_id=user_id, project_id=project_id, file_name=file_name,
                file_id=file_id, review_type=review_type,
                code=code, language=language, model_name=agent.model, profiles=profiles,
                usage_origin=usage_origin,
            ),
        )
        try:
            task_id = await asyncio.shield(create_future)
        except asyncio.CancelledError:
            # shield 保证数据库创建原子完成；随后立即标记取消，避免留下 running 任务。
            try:
                task_id = await create_future
            except Exception:
                task_id = 0
            if task_id:
                await loop.run_in_executor(None, copy_context().run, lambda: _cancel_review_task(task_id))
            self._publish_terminal(session_id, task_id, "cancelled", "圆桌讨论已取消")
            raise
        except Exception as exc:
            logger.warning(f"[Discussion] 创建 ReviewTask 失败: {traceback.format_exc()}")
            self._publish_terminal(session_id, 0, "failed", f"无法创建可信圆桌任务：{exc}")
            return
        if not task_id:
            self._publish_terminal(session_id, 0, "failed", "无法创建可信圆桌任务，讨论未执行")
            return
        self._task_id = task_id
        session.task_id = task_id
        final_status = "failed"
        final_error = ""
        final_partial = False
        summary_turns: tuple[DiscussionTurn, ...] | None = None
        call_budget = _RoundtableCallBudget()
        budget_token = _roundtable_call_budget.set(call_budget)
        publish_progress("speaking", 0, progress_total)

        # 广播调度事件,点亮参会工位卡
        for code_ in {_PROFILE_TO_AGENT_CODE.get(p.code, p.code) for p in profiles}:
            self._emit(AgentEventType.DISPATCH, code_,
                       f"圆桌讨论启动: {file_name}")

        try:
            # ── 开场白 ──
            opening_text = (
                f"🎤 欢迎来到代码审查圆桌会议！\n\n"
                f"审查文件: {file_name} ({language})\n"
                f"参会Agent: {', '.join(p.name for p in profiles)}\n"
                f"共 {max_rounds} 轮讨论。每位 Agent 都会读取共享聊天记录,"
                f"可选择发言或静音;发言时可赞同、否认、质疑或补充他人观点。"
            )
            bus.publish_turn(session_id, DiscussionTurn(
                turn_id=0,
                agent_code="orchestrator",
                agent_name="主持人",
                role="agent",
                content=opening_text,
            ))
            # v2.4 B1: 开场白 publish 到 MetaGPT Environment
            self._publish_to_env(
                speaker="orchestrator",
                content=opening_text,
                turn_id=0,
                cause_by="StartDiscussion",
            )
            continuation_text = str(continuation_context or "").strip()
            if continuation_text:
                turn_counter += 1
                continuation_turn = DiscussionTurn(
                    turn_id=turn_counter,
                    agent_code="user",
                    agent_name="你",
                    role="user",
                    content=continuation_text,
                )
                all_turns.append(continuation_turn)
                # 续会包同时包含上一轮结论与已入账的原始纠正；仅作模型上下文，
                # 不再发布成第二条用户聊天气泡，也不写入圆桌公开发言账本。
                self._publish_to_env(
                    speaker="user",
                    content=continuation_text,
                    turn_id=turn_counter,
                    cause_by="DiscussionContinuation",
                )
            await asyncio.sleep(0.5)

            consumed_user_input_count = 0
            for round_idx in range(max_rounds):
                await self._wait_if_paused(session_id)
                await self._check_active()

                bus.publish_control(session_id, "round_start", {
                    "round": round_idx + 1,
                    "total_rounds": max_rounds,
                })

                # ── 逐一轮流发言 (类聊天室) ──
                for speaker_idx, profile in enumerate(profiles):
                    await self._wait_if_paused(session_id)
                    await self._check_active()

                    target_code = _PROFILE_TO_AGENT_CODE.get(profile.code, profile.code)
                    bus.publish_control(session_id, "speaker", {
                        "speaker_code": profile.code,
                        "speaker_name": profile.name,
                        "speaker_index": speaker_idx + 1,
                        "total_speakers": len(profiles),
                    })
                    self._emit(AgentEventType.THINKING, target_code,
                               f"{profile.name} 正在组织第 {round_idx+1} 轮发言")

                    logger.info(
                        f"[Discussion] R{round_idx+1} 轮到 {profile.name}"
                        f"({speaker_idx+1}/{len(profiles)}) 发言",
                    )

                    coverage["attempted_turns"] += 1
                    input_snapshot_count = len(self._user_inputs)
                    decision, meta, ok = await self._speaker_turn(
                        agent=speaker_agents[target_code],
                        profile=profile,
                        code=code,
                        language=language,
                        file_name=file_name,
                        all_turns=all_turns,
                        user_inputs=self._user_inputs[consumed_user_input_count:input_snapshot_count],
                        round_idx=round_idx,
                        speaker_idx=speaker_idx,
                        remaining_turns=coverage["expected_turns"] - coverage["attempted_turns"],
                    )
                    await self._check_active()
                    # 模型生成期间抵达的用户输入留待下一位 Agent；不得在轮末清空。
                    consumed_user_input_count = input_snapshot_count
                    coverage["successful_turns" if ok else "failed_turns"] += 1
                    if ok and decision.action == "speak" and decision.content.strip():
                        coverage["valid_speeches"] += 1
                    elif ok:
                        coverage["silent_turns"] += 1
                    else:
                        coverage["errors"].append(f"{profile.name} 第 {round_idx + 1} 轮发言失败")
                    if meta:
                        metered_calls = meta if isinstance(meta, list) else [
                            (round_idx * 100 + speaker_idx, meta)
                        ]
                        for chunk_index, call_meta in metered_calls:
                            deferred_logs.append({
                                "meta": call_meta,
                                "status": "success" if ok else "failed",
                                "chunk_index": chunk_index,
                                "agent_label": profile.code,
                            })
                    self._emit(
                        AgentEventType.COMPLETE if ok else AgentEventType.FAILED,
                        target_code,
                        (
                            f"{profile.name} 第 {round_idx+1} 轮选择静音"
                            if decision.action == "silent" and ok
                            else f"{profile.name} 完成第 {round_idx+1} 轮发言"
                        ),
                    )

                    turn_counter += 1
                    turn = DiscussionTurn(
                        turn_id=turn_counter,
                        agent_code=profile.code,
                        agent_name=profile.name,
                        role="agent",
                        content=decision.content,
                        action=decision.action,
                        stance=decision.stance,
                        reply_to=decision.reply_to,
                        round_index=round_idx + 1,
                    )
                    if ok:
                        all_turns.append(turn)
                    bus.publish_turn(session_id, turn)
                    # v2.4 B1: Agent 发言 publish 到 MetaGPT Environment
                    self._publish_to_env(
                        speaker=profile.code,
                        content=turn.content,
                        turn_id=turn_counter,
                    )
                    publish_progress(
                        "speaking", coverage["attempted_turns"], progress_total,
                        current_round=round_idx + 1, speaker_code=profile.code,
                    )



            # 最后一位专家生成期间抵达的消息已被接收，但未进入该次模型输入。
            # 最多追加两次专家补答；持续发言则明确告知剩余消息需续会。
            rescue_count = 0
            rescue_answered = True
            while len(self._user_inputs) > consumed_user_input_count and rescue_count < 2:
                await self._wait_if_paused(session_id)
                await self._check_active()
                rescue_count += 1
                profile = next((item for item in profiles if item.code == "general"), profiles[-1])
                target_code = _PROFILE_TO_AGENT_CODE.get(profile.code, profile.code)
                input_snapshot_count = len(self._user_inputs)
                coverage["expected_turns"] += 1
                coverage["attempted_turns"] += 1
                progress_total += 1
                self._emit(AgentEventType.THINKING, target_code, f"{profile.name} 正在回应最新用户消息")
                decision, meta, ok = await self._speaker_turn(
                    agent=speaker_agents[target_code], profile=profile,
                    code=code, language=language, file_name=file_name,
                    all_turns=all_turns,
                    user_inputs=self._user_inputs[consumed_user_input_count:input_snapshot_count],
                    round_idx=max_rounds - 1,
                    speaker_idx=len(profiles) + rescue_count,
                    remaining_turns=0,
                )
                await self._check_active()
                consumed_user_input_count = input_snapshot_count
                coverage["successful_turns" if ok else "failed_turns"] += 1
                if ok and decision.action == "speak" and decision.content.strip():
                    coverage["valid_speeches"] += 1
                elif ok:
                    coverage["silent_turns"] += 1
                    rescue_answered = False
                else:
                    coverage["errors"].append(f"{profile.name} 用户消息补答失败")
                    rescue_answered = False
                if meta:
                    metered_calls = meta if isinstance(meta, list) else [
                        ((max_rounds - 1) * 100 + len(profiles) + rescue_count, meta)
                    ]
                    for chunk_index, call_meta in metered_calls:
                        deferred_logs.append({
                            "meta": call_meta, "status": "success" if ok else "failed",
                            "chunk_index": chunk_index, "agent_label": profile.code,
                        })
                turn_counter += 1
                turn = DiscussionTurn(
                    turn_id=turn_counter, agent_code=profile.code, agent_name=profile.name,
                    role="agent", content=decision.content, action=decision.action,
                    stance=decision.stance, reply_to=decision.reply_to,
                    round_index=max_rounds,
                )
                if ok:
                    all_turns.append(turn)
                bus.publish_turn(session_id, turn)
                self._publish_to_env(speaker=profile.code, content=turn.content, turn_id=turn_counter)
                self._emit(
                    AgentEventType.COMPLETE if ok else AgentEventType.FAILED,
                    target_code,
                    f"{profile.name} 已回应最新用户消息" if ok else f"{profile.name} 用户消息补答失败",
                )
                publish_progress(
                    "speaking", coverage["attempted_turns"], progress_total,
                    current_round=max_rounds, speaker_code=profile.code,
                )

            remaining_user_inputs = len(self._user_inputs) - consumed_user_input_count
            if remaining_user_inputs or (rescue_count and not rescue_answered):
                status_text = (
                    f"已收到 {remaining_user_inputs} 条专家尚未处理的补充消息。"
                    "本轮专家补答已达到上限；主持会纳入结论，若需专家进一步回应请在结论后续会。"
                    if remaining_user_inputs else
                    "已收到补充消息，但专家本轮未能给出有效补答；主持会纳入结论，"
                    "若需专家进一步回应请在结论后续会。"
                )
                turn_counter += 1
                bus.publish_turn(session_id, DiscussionTurn(
                    turn_id=turn_counter, agent_code="orchestrator", agent_name="主持人",
                    role="agent", content=status_text,
                ))
                if remaining_user_inputs:
                    coverage["pending_user_inputs"] = remaining_user_inputs
                    coverage["errors"].append("最后阶段有用户消息未获专家补答，报告仅可作为部分结果")

            # ── 主持人汇总 (线程池执行,避免阻塞事件循环) ──
            await self._check_active()
            if not coverage["valid_speeches"]:
                coverage["summary_status"] = "skipped"
                raise RuntimeError("圆桌讨论没有产生有效审查发言，不能生成成功报告")
            stopped = False
            self._emit(AgentEventType.THINKING, "orchestrator", "主持人正在汇总讨论共识")
            publish_progress("summarizing", coverage["attempted_turns"], progress_total)
            # 此阶段总线拒绝新发言；主持人与结构化抽取使用同一不可变边界。
            summary_turns = tuple(all_turns)
            summary_text, summary_meta = await loop.run_in_executor(
                None, copy_context().run,
                lambda: self._summarize(
                    list(summary_turns), code, language, file_name, agent, stopped,
                ),
            )
            await self._check_active()
            coverage["summary_status"] = "success" if summary_meta is not None else "failed"
            publish_progress("summarizing", coverage["attempted_turns"] + 1, progress_total)
            if summary_meta is None:
                coverage["errors"].append("主持人模型汇总失败，仅保留发言摘录")
            if summary_meta:
                deferred_logs.append({
                    "meta": summary_meta,
                    "status": "success",
                    "chunk_index": 9000,
                    "agent_label": "general",
                })
            bus.publish_turn(session_id, DiscussionTurn(
                turn_id=turn_counter + 1,
                agent_code="orchestrator",
                agent_name="主持人",
                role="agent",
                content=summary_text,
            ))
            # v2.4 B1: 主持人汇总 publish 到 MetaGPT Environment
            self._publish_to_env(
                speaker="orchestrator",
                content=summary_text,
                turn_id=turn_counter + 1,
                cause_by="DiscussionSummary",
            )
            self._emit(
                AgentEventType.COMPLETE if summary_meta is not None else AgentEventType.FAILED,
                "orchestrator", "主持人已汇总共识" if summary_meta is not None else "主持人汇总失败",
            )

        except asyncio.CancelledError:
            cancelled = True
            logger.info(f"[Discussion] 会话任务已取消 session={session_id} task={task_id}")
            raise
        except _DiscussionInactive as exc:
            cancelled = exc.status == "cancelled"
            final_status = exc.status
            coverage["errors"].append(str(exc))
        except Exception as exc:
            coverage["errors"].append(str(exc))
            logger.error(f"[Discussion] 异常: {traceback.format_exc()}")
        finally:
            # ── 沉淀报告: 写调用日志 + 抽取问题 + 收尾 ReviewTask ──
            stopped = False
            report_task_id = task_id
            cancelled_during_finalization = False
            if task_id:
                try:
                    if cancelled:
                        report_task_id = await loop.run_in_executor(
                            None, copy_context().run,
                            lambda: _cancel_review_task(task_id),
                        )
                    else:
                        stopped = session.status != "active"
                        finalize_future = loop.run_in_executor(
                            None, copy_context().run,
                            lambda: _finalize_review(
                                task_id=task_id,
                                user_id=user_id,
                                file_id=file_id,
                                file_name=file_name,
                                all_turns=list(summary_turns) if summary_turns is not None else list(all_turns),
                                code=code,
                                language=language,
                                deferred_logs=deferred_logs,
                                agent=agent,
                                stopped=stopped,
                                consensus=summary_text,
                                coverage=coverage,
                                progress_callback=publish_progress_from_worker,
                            ),
                        )
                        report_task_id = await asyncio.shield(finalize_future)
                    state = await loop.run_in_executor(None, copy_context().run, lambda: _review_task_state(task_id))
                    final_status = state["status"]
                    final_error = state.get("error") or ""
                    final_partial = (state.get("coverage") or {}).get("stage") == "partial"
                except asyncio.CancelledError:
                    cancelled_during_finalization = True
                    report_task_id = await loop.run_in_executor(
                        None, copy_context().run, lambda: _cancel_review_task(task_id)
                    )
                    state = await loop.run_in_executor(None, copy_context().run, lambda: _review_task_state(task_id))
                    final_status = state["status"]
                    final_error = state.get("error") or "圆桌讨论已取消，丢弃未完成的报告结果"
                except Exception as exc:
                    final_status = "failed"
                    final_error = f"圆桌报告收尾失败：{exc}"
                    logger.error(f"[Discussion] 收尾报告失败: {traceback.format_exc()}")
            coverage["model_calls_used"] = call_budget.used
            _roundtable_call_budget.reset(budget_token)
            self._publish_terminal(
                session_id, report_task_id, final_status,
                final_error or ("；".join(coverage["errors"]) if final_status != "success" else ""),
                partial=final_partial,
            )
            # 对齐团队派发逻辑:结论回投发起会话,小菱自动续跑汇报,无需用户手动追问。
            if self._origin_surface and self._origin_session_key:
                await loop.run_in_executor(
                    None,
                    lambda: _notify_origin_session(
                        user_id=self._user_id,
                        surface=self._origin_surface,
                        session_key=self._origin_session_key,
                        discussion_session_id=session_id,
                        file_name=file_name,
                        report_task_id=report_task_id,
                        status="concluded" if final_status == "success" else final_status,
                        summary=summary_text,
                    ),
                )
            if cancelled_during_finalization:
                raise asyncio.CancelledError

    async def _check_active(self) -> None:
        """同时校验讨论会话和持久化任务，停止、删除后不再继续发言。"""
        session = self._bus.get_session(self._session_id)
        if not session or session.status != "active":
            raise _DiscussionInactive("cancelled")
        await asyncio.get_running_loop().run_in_executor(
            None, copy_context().run, lambda: _ensure_running(self._task_id)
        )

    def _publish_terminal(
        self, session_id: str, task_id: int, status: str, error: str = "",
        *, partial: bool = False,
    ) -> None:
        """复用发言和 done 控制帧发布可见失败原因，再关闭会话。"""
        session = self._bus.get_session(session_id)
        if session:
            session.report_task_id = task_id
        if status != "success":
            message = error or f"圆桌讨论已结束（{status}），没有生成完整成功报告。"
            self._bus.publish_turn(session_id, DiscussionTurn(
                turn_id=len(session.turns) + 1 if session else 0,
                agent_code="orchestrator", agent_name="主持人", role="agent", content=message,
            ))
            self._emit(AgentEventType.FAILED, "orchestrator", message)
        payload = {
            "task_id": task_id, "status": status, "error": error,
            "partial": bool(partial and status == "failed"),
        }
        if status == "cancelled":
            self._bus.publish_control(session_id, "cancelled", payload)
        self._bus.publish_control(session_id, "done", payload)
        self._bus.close_session(session_id)
        if status == "success":
            # 报告整理期间用户仍可发言；报告封存后立即处理这些已入账追问，
            # 不要求浏览器再发一条消息或重新连接才触发主持人。
            from app.services.roundtable_followup_service import ensure_followup_worker

            ensure_followup_worker(self._bus, session_id, self._user_id)

    # ── 发言逻辑 ──

    async def _speaker_turn(
        self,
        agent: DeepSeekAgent,
        profile: ReviewAgentProfile,
        code: str,
        language: str,
        file_name: str,
        all_turns: list[DiscussionTurn],
        user_inputs: list[str],
        round_idx: int,
        speaker_idx: int,
        remaining_turns: int = 0,
    ) -> tuple[SpeakerDecision, Optional[dict | list[tuple[int, dict]]], bool]:
        """让一个 Agent 基于共享历史决定发言；超长源码逐窗审查后合并一轮。

        Args:
            agent: 当前讨论共用的 DeepSeek 调用客户端。
            profile: 当前审查子 Agent 画像。
            code: 待审查源代码。
            language: 代码语言。
            file_name: 文件名。
            all_turns: 此前全部 Agent 决策与用户发言。
            user_inputs: 尚未完成一轮消费的用户最新指示。
            round_idx: 从零开始的讨论轮次。
            speaker_idx: 从零开始的本轮发言顺序。
            remaining_turns: 本轮之后还需发言的 Agent 次数，用于预留会话调用量。

        Returns:
            tuple[SpeakerDecision, Optional[dict | list[tuple[int, dict]]], bool]:
            自主决策、逐窗日志元数据和全部窗口是否成功。
        """
        loop = asyncio.get_running_loop()
        call_budget = _roundtable_call_budget.get()
        is_first = not any(
            t.role == "agent" and getattr(t, "action", "speak") == "speak"
            for t in all_turns
        )

        system = (
            f"你是代码审查专家「{profile.name}」,正在参加代码审查圆桌讨论。\n\n"
            f"文件: {file_name}({language})\n"
            f"你的专业领域: {profile.focus}\n"
            f"你的重点问题类型: {' / '.join(profile.issue_types)}\n\n"
            f"决策规则:\n"
            f"1. 先自主选择发言或静音。存在独立发现、新证据、不同判断或需要回应用户时"
            f"选择 speak;现有观点已完整覆盖且没有新增价值时选择 silent;\n"
            f"2. 发言时从 propose/agree/oppose/question/supplement/neutral 中选择立场。"
            f"发现判断有误时直接 oppose,不要为了客气附和;\n"
            f"3. reply_to 填被回应者的 agent_code;没有明确对象时填 null;\n"
            f"4. speak 的 content 使用 100-300 字中文自然语言,报告不超过 3 个关键问题,"
            f"并用代码行号和内容支撑;silent 的 content 简述静音原因;\n"
            f"5. 只输出 JSON 对象,格式为:"
            f'{{"action":"speak|silent","stance":"propose|agree|oppose|question|'
            f'supplement|neutral","reply_to":"agent_code或null","content":"正文或静音原因"}}。'
        )

        history_text = self._build_history(all_turns)
        user_text = ""
        if user_inputs:
            user_text = "\n## ⚡ 用户最新指示\n" + "\n".join(
                f"- {u}" for u in user_inputs
            ) + "\n请优先回应用户的指示。"

        stance_hint = (
            "你是第一位有效发言者;有独立发现时用 propose 发言,未发现问题也可静音。"
            if is_first
            else "请基于完整记录选择发言或静音;发言时明确赞同、否认、质疑或补充及回应对象。"
        )
        fence = chr(96) * 3

        def speaker_prompt(history: str, code_view: str, scope: str = "") -> str:
            return (
                f"## 待审查代码{scope}\n{fence}{language}\n{code_view}\n{fence}\n\n"
                f"{history}\n{user_text}\n\n"
                f"## 现在轮到你「{profile.name}」发言了\n{stance_hint}"
            )

        history_for_prompt = history_text
        user_prompt = speaker_prompt(history_for_prompt, code)
        # 推理模型的思考内容与 JSON 正文共享输出预算。生产中默认 4096 曾让
        # 第二轮专家以 finish_reason=length 结束；按预算递增重试同一完整提示。
        output_ceiling = min(
            _clamp_max_tokens(settings.deepseek_max_output_tokens),
            # 为源码与发言历史至少留出约三分之二上下文；小窗口兼容旧预算。
            max(_clamp_max_tokens(None), settings.deepseek_context_window_tokens // 3),
        )
        output_budgets = tuple(sorted({
            min(output_ceiling, _clamp_max_tokens(value))
            for value in (8192, 16_384, 32_768)
        }))
        output_budget = output_budgets[-1]
        budget_error = _roundtable_input_budget_error(
            system, user_prompt, max_output_tokens=output_budget,
        )
        windows: list[str] = []
        try:
            if call_budget:
                call_budget.ensure_capacity(
                    len(output_budgets) + max(0, remaining_turns) + 2,
                )
            if budget_error and all_turns:
                available = settings.deepseek_context_window_tokens - output_budget - 1024
                # 先为源码留下最低证据窗口容量，再压缩历史；不能让历史吃掉全部输入。
                reserved_code_tokens = min(4000, max(512, available // 4))
                history_budget = (
                    available - estimate_tokens(system)
                    - estimate_tokens(speaker_prompt("", ""))
                    - reserved_code_tokens
                )
                if history_budget > 0 and estimate_tokens(history_text) > history_budget:
                    projected = await loop.run_in_executor(
                        None, copy_context().run,
                        lambda: _compress_roundtable_history(
                            _roundtable_history_records(all_turns),
                            agent=agent, task_id=self._task_id, user_id=self._user_id,
                            file_id=self._file_id, target_tokens=history_budget,
                        ),
                    )
                    history_for_prompt = (
                        "## 讨论记录（语义投影；原文保存在会话中）\n" + projected
                    )
                user_prompt = speaker_prompt(history_for_prompt, code)
                budget_error = _roundtable_input_budget_error(
                    system, user_prompt, max_output_tokens=output_budget,
                )
            if budget_error:
                available = settings.deepseek_context_window_tokens - output_budget - 1024
                code_budget = (
                    available - estimate_tokens(system)
                    - estimate_tokens(speaker_prompt(history_for_prompt, "", "（绝对行号窗口）"))
                )
                if code_budget <= 128:
                    raise RuntimeError(
                        f"{budget_error}；压缩历史后也没有可用源码窗口容量"
                    )
                max_chars = min(_EXTRACTION_CODE_WINDOW_CHARS, max(128, code_budget * 2))
                for _attempt in range(12):
                    windows = _discussion_code_windows(code, max_chars=max_chars)
                    if len(windows) > _SPEAKER_MAX_WINDOWS:
                        raise RuntimeError(
                            f"源码需 {len(windows)} 个发言窗口，超过 {_SPEAKER_MAX_WINDOWS} 个上限；"
                            "所有源码仍保留，不能标记本轮完成"
                        )
                    if all(
                        _roundtable_input_budget_error(
                            system,
                            speaker_prompt(
                                history_for_prompt, window,
                                f"（源码窗口 {index}/{len(windows)}，行号为原文件绝对行号；"
                                "仅判断当前窗口证据，不推断其他窗口）",
                            ),
                            max_output_tokens=output_budget,
                        ) is None
                        for index, window in enumerate(windows, start=1)
                    ):
                        if call_budget:
                            call_budget.ensure_capacity(
                                len(windows) + max(0, remaining_turns) + 3,
                            )
                        break
                    max_chars //= 2
                    if max_chars < 128:
                        raise RuntimeError(
                            "最小源码窗口仍超模型输入预算；原始源码未截断，不能标记本轮完成"
                        )
                else:
                    raise RuntimeError("无法在有界重试内形成完整源码窗口")
        except _DiscussionInactive:
            raise
        except Exception as exc:
            logger.warning(f"[Discuss] {profile.code} 准备发言上下文失败: {exc}")
            return SpeakerDecision(
                action="speak", stance="neutral", reply_to=None,
                content=f"(抱歉，发言上下文准备失败：{exc})",
            ), None, False

        def call_speaker(prompt: str, chunk_index: int):
            if self._task_id:
                _ensure_running(self._task_id)
            for index, budget in enumerate(output_budgets):
                budget_error = _roundtable_input_budget_error(
                    system, prompt, max_output_tokens=budget,
                )
                if budget_error:
                    raise RuntimeError(budget_error)
                try:
                    return _call_raw_for_task(agent, self._task_id, self._user_id,
                        usage_file_id=self._file_id, usage_chunk_index=chunk_index,
                        system_prompt=system, user_prompt=prompt,
                        agent_label=profile.code, json_mode=True,
                        max_tokens=budget,
                    )
                except DeepSeekOutputTruncatedError:
                    if index == len(output_budgets) - 1:
                        raise
            raise RuntimeError("专家发言未获得完整模型响应")

        try:
            if not windows:
                chunk_index = round_idx * 100 + speaker_idx
                content, meta = await loop.run_in_executor(
                    None, copy_context().run,
                    lambda: call_speaker(user_prompt, chunk_index),
                )
                if not isinstance(content, str) or not content.strip():
                    raise RuntimeError("模型返回空内容，未完成本轮审查")
                return _parse_speaker_decision(content), meta, True

            window_decisions: list[SpeakerDecision] = []
            window_metas: list[tuple[int, dict]] = []
            for index, window in enumerate(windows, start=1):
                scope = (
                    f"（源码窗口 {index}/{len(windows)}，行号为原文件绝对行号；"
                    "仅判断当前窗口证据，不推断其他窗口）"
                )
                prompt = speaker_prompt(history_for_prompt, window, scope)
                chunk_index = round_idx * 10_000 + speaker_idx * 100 + index
                content, meta = await loop.run_in_executor(
                    None, copy_context().run,
                    lambda prompt=prompt, chunk_index=chunk_index: call_speaker(prompt, chunk_index),
                )
                if not isinstance(content, str) or not content.strip():
                    raise RuntimeError(f"源码窗口 {index}/{len(windows)} 返回空内容")
                decision = _parse_speaker_decision(content)
                window_decisions.append(decision)
                if meta:
                    window_metas.append((chunk_index, meta))

            records = []
            for index, (window, decision) in enumerate(zip(windows, window_decisions), start=1):
                line_numbers = [int(value) for value in re.findall(r"(?m)^(\d+): ", window)]
                line_scope = (
                    f"第 {line_numbers[0]}–{line_numbers[-1]} 行"
                    if line_numbers else "行号未知"
                )
                records.append((
                    f"W{index:04d}",
                    f"【源码窗口 {index}/{len(windows)}·{line_scope}·{decision.action}】"
                    f"{decision.content}",
                ))
            projection = await loop.run_in_executor(
                None, copy_context().run,
                lambda: _compress_roundtable_history(
                    records, agent=agent, task_id=self._task_id, user_id=self._user_id,
                    file_id=self._file_id,
                    target_tokens=min(16_000, max(2_048, settings.deepseek_context_window_tokens // 4)),
                ),
            )
            spoke = any(decision.action == "speak" for decision in window_decisions)
            return SpeakerDecision(
                action="speak" if spoke else "silent",
                stance="neutral", reply_to=None,
                content=f"完整源码已分 {len(windows)} 个连续窗口审查；逐窗结论：\n{projection}",
            ), window_metas, True
        except _DiscussionInactive:
            raise
        except Exception as e:
            logger.warning(f"[Discuss] {profile.code} 发言失败: {e}")
            return SpeakerDecision(
                action="speak",
                stance="neutral",
                reply_to=None,
                content=f"(抱歉,发言时遇到技术问题: {e})",
            ), None, False

    def _build_history(self, turns: list[DiscussionTurn]) -> str:
        """按时间顺序构造所有参会者共享的聊天记录。

        Args:
            turns: 此前的 Agent 决策和用户发言。

        Returns:
            str: 包含发言、静音、立场和回应对象的 Markdown 上下文。
        """
        if not turns:
            return "## 讨论记录\n(你是第一位发言者,开始你的表演吧！)"

        parts = ["## 讨论记录 (按发言时间顺序)"]
        for t in turns:
            role_label = "用户" if t.role == "user" else f"{t.agent_name}({t.agent_code})"
            action = getattr(t, "action", "speak")
            stance = getattr(t, "stance", "neutral")
            reply_to = getattr(t, "reply_to", None)
            metadata = ""
            if t.role == "agent":
                metadata = f" [动作:{action}; 立场:{stance}"
                if reply_to:
                    metadata += f"; 回应:{reply_to}"
                metadata += "]"
            parts.append(
                f"\n### {role_label}{metadata}\n{t.content}",
            )
        return "\n".join(parts)

    def _summarize(self, turns: list[DiscussionTurn], code: str,
                   language: str, file_name: str, agent: DeepSeekAgent,
                   stopped: bool = False) -> tuple[str, Optional[dict]]:
        """生成主持人共识小结。Returns (文本, AiCallLog meta)。"""
        prefix = "🛑 讨论已被用户终止。\n\n" if stopped else ""
        agent_turns = [
            t for t in turns
            if t.role == "agent" and getattr(t, "action", "speak") == "speak"
        ]
        if not agent_turns:
            return prefix + "本次讨论没有产生有效发言。", None

        history = self._build_history(turns)
        fence = chr(96) * 3
        system_prompt = (
            "你是代码审查圆桌讨论的主持人。请用简洁的中文自然语言汇总各位专家的发言,"
            "形成一份结论性总结(不要输出 JSON)。要求:\n"
            "1. 先用一句话给出代码总体评价;\n"
            "2. 然后用 Markdown 有序列表列出大家达成共识的关键问题"
            "(每条注明所属维度、严重程度,并尽量带上代码行号);\n"
            "3. 单列尚未解决的分歧,说明赞同、否认或质疑双方的依据;\n"
            "4. 明确回应用户在群聊中的关注点;没有用户插话时省略此项;\n"
            "5. 最后给出 1-2 句改进优先级建议。\n"
            "静音记录只表示该 Agent 没有新增观点,不得当成问题或共识。\n"
            "总长度控制在 450 字以内。"
        )
        def summary_prompt(discussion_history: str, code_view: str, *, projected_code: bool = False) -> str:
            code_section = (
                "## 完整源码窗口的证据投影（绝对行号，原文保存在审查任务中）\n"
                f"{code_view}\n\n"
                if projected_code else
                f"{fence}{language}\n{code_view}\n{fence}\n\n"
            )
            return (
                f"## 审查文件: {file_name}({language})\n{code_section}"
                f"## 讨论发言记录\n{discussion_history}\n\n请汇总共识。"
            )

        history_for_prompt = history
        user_prompt = summary_prompt(history_for_prompt, code)
        ceiling = _clamp_max_tokens(settings.deepseek_max_output_tokens)
        first_budget = min(16_384, ceiling, max(512, settings.deepseek_context_window_tokens // 4))
        retry_budget = min(first_budget * 2, ceiling)
        budgets = [first_budget] + ([retry_budget] if retry_budget > first_budget else [])
        try:
            if self._task_id:
                _ensure_running(self._task_id)
            budget_error = _roundtable_input_budget_error(
                system_prompt, user_prompt, max_output_tokens=budgets[-1],
            )
            if budget_error and turns:
                available = settings.deepseek_context_window_tokens - budgets[-1] - 1024
                reserved_code_tokens = min(estimate_tokens(code), max(512, available // 3))
                history_budget = (
                    available - estimate_tokens(system_prompt)
                    - estimate_tokens(summary_prompt("", ""))
                    - reserved_code_tokens
                )
                if history_budget > 0 and estimate_tokens(history) > history_budget:
                    projected = _compress_roundtable_history(
                        _roundtable_history_records(turns),
                        agent=agent, task_id=self._task_id, user_id=self._user_id,
                        file_id=self._file_id, target_tokens=history_budget,
                    )
                    history_for_prompt = (
                        "语义投影（完整原始发言保存在圆桌会话中）：\n" + projected
                    )
                user_prompt = summary_prompt(history_for_prompt, code)
                budget_error = _roundtable_input_budget_error(
                    system_prompt, user_prompt, max_output_tokens=budgets[-1],
                )
            if budget_error:
                available = settings.deepseek_context_window_tokens - budgets[-1] - 1024
                code_budget = (
                    available - estimate_tokens(system_prompt)
                    - estimate_tokens(summary_prompt(history_for_prompt, "", projected_code=True))
                )
                if code_budget <= 0:
                    raise RuntimeError(f"{budget_error}；主持没有可用源码投影预算")
                windows = _discussion_code_windows(
                    code, max_chars=min(_EXTRACTION_CODE_WINDOW_CHARS, max(512, code_budget)),
                )
                records = []
                for index, window in enumerate(windows, start=1):
                    line_numbers = [int(value) for value in re.findall(r"(?m)^(\d+): ", window)]
                    records.append((
                        f"C{index:04d}",
                        f"【源码窗口 {index}/{len(windows)}·第 {line_numbers[0]}–{line_numbers[-1]} 行】"
                        f"{window}",
                    ))
                code_projection = _compress_roundtable_history(
                    records, agent=agent, task_id=self._task_id, user_id=self._user_id,
                    file_id=self._file_id, target_tokens=code_budget,
                )
                user_prompt = summary_prompt(
                    history_for_prompt, code_projection, projected_code=True,
                )
            for budget in budgets:
                try:
                    budget_error = _roundtable_input_budget_error(
                        system_prompt, user_prompt, max_output_tokens=budget,
                    )
                    if budget_error:
                        raise RuntimeError(budget_error)
                    raw, meta = _call_raw_for_task(
                        agent, self._task_id, self._user_id,
                        usage_file_id=self._file_id, usage_chunk_index=9000,
                        system_prompt=system_prompt, user_prompt=user_prompt,
                        agent_label="general", json_mode=False, max_tokens=budget,
                    )
                except DeepSeekOutputTruncatedError:
                    continue
                body = raw.strip()
                if body:
                    return f"{prefix}📋 **讨论共识小结**\n\n{body}", meta
        except _DiscussionInactive:
            raise
        except Exception as exc:
            logger.warning(f"[Discussion] 共识失败: {exc}")

        # 回退: 简单统计
        lines = [f"{prefix}📋 讨论结束 (共 {len(agent_turns)} 条发言):", ""]
        agent_issues: dict[str, list[str]] = {}
        for t in agent_turns:
            agent_issues.setdefault(t.agent_name, []).append(t.content)
        for name, items in agent_issues.items():
            lines.append(f"**{name}**:")
            for item in items:
                lines.append(f"  · {item}")
        return "\n".join(lines), None

    # ── 事件广播 ──

    def _publish_to_env(
        self,
        speaker: str,
        content: str,
        turn_id: Optional[int] = None,
        cause_by: str = "DiscussTurn",
    ) -> None:
        """v2.4 B1: 把讨论发言 publish 到 MetaGPT Environment

        Environment 接收 Message 后会:
            1. 记录到 _history(完整讨论历史)
            2. 通过 AgentEventBus 发布 DISCUSS 事件(前端 SSE 可见)
        不调用 env.run(),避免与现有发言循环重复触发 LLM 调用。
        Environment 构建失败或为 None 时静默降级,不影响讨论主流程。

        Args:
            speaker: 发言者 code(user / orchestrator / code_reviewer 等)
            content: 发言内容文本
            turn_id: 讨论轮次 ID(可选)
            cause_by: 消息动作类型,默认 "DiscussTurn";
                      开场白用 "StartDiscussion",汇总用 "DiscussionSummary"
        """
        if not self._env or not content:
            return
        try:
            msg = make_discussion_message(
                speaker=speaker,
                content=content,
                user_id=self._user_id or None,
                project_id=None,
                file_id=self._file_id or None,
                trace_id=self._trace_id,
                turn_id=turn_id,
            )
            # 覆盖默认 cause_by(make_discussion_message 默认 "DiscussTurn")
            msg.cause_by = cause_by
            self._env.publish(msg)
        except Exception as e:
            logger.debug(f"[Discussion] publish 到 Environment 失败(不影响主流程): {e}")

    def _emit(self, type_: AgentEventType, agent_code: str, message: str) -> None:
        """向 Agent 办公室广播当前用户所属的讨论事件。

        Args:
            type_: 事件类型。
            agent_code: 产生事件的 Agent 编码。
            message: 面向前端展示的事件说明。

        Returns:
            None: 事件发布失败时静默降级，不影响讨论主流程。
        """
        try:
            user_id = getattr(self, "_user_id", 0) or None
            AgentEventBus.instance().publish(AgentEvent(
                type=type_,
                agent=agent_code,
                trace_id=self._trace_id,
                message=message,
                payload={"task_id": getattr(self, "_task_id", 0),
                         "user_id": getattr(self, "_user_id", 0),
                         "source": "discussion"},
                user_id=user_id,
            ))
        except Exception:
            pass

    # ── 用户控制 ──

    _session_id: str = ""
    _paused: bool = False
    _paused_event: Optional[asyncio.Event] = None
    _user_inputs: list[str] = []
    _all_turns: list[DiscussionTurn] = []
    _task_id: int = 0
    _user_id: int = 0
    _file_id: int = 0
    _trace_id: str = ""
    # v2.4 B1: MetaGPT Environment 引用(可选,构建失败时为 None)
    _env: Optional[Environment] = None

    def _handle_control(self, action: str, payload: dict):
        if action == "user_input":
            content = payload.get("content", "")
            logger.info(f"[Discussion] 用户发言: {content[:60]}...")
            self._user_inputs.append(content)
            if content:
                self._all_turns.append(DiscussionTurn(
                    turn_id=int(payload.get("turn_id") or -1),
                    agent_code="user",
                    agent_name="你",
                    role="user",
                    content=content,
                    action="speak",
                    stance="neutral",
                ))
            # v2.4 B1: 用户发言 publish 到 MetaGPT Environment
            self._publish_to_env(
                speaker="user",
                content=content,
                turn_id=payload.get("turn_id"),
            )
            # 用户发言不再强制取消暂停; 仅唤醒等待以便尽快读取指示
            if self._paused_event:
                self._paused_event.set()
        elif action == "pause":
            self._paused = True
            self._paused_event = asyncio.Event()
            self._bus.publish_control(self._session_id, "paused", {})
        elif action == "resume":
            self._paused = False
            self._bus.publish_control(self._session_id, "resumed", {})
            if self._paused_event:
                self._paused_event.set()
        elif action == "stop":
            self._paused = False
            self._bus.request_stop(self._session_id)
            if self._task_id:
                self._bus.cancel_discussion_task(self._session_id)
            self._bus.publish_control(self._session_id, "stopping", {})
            if self._paused_event:
                self._paused_event.set()

    async def _wait_if_paused(self, session_id: str):
        while self._paused:
            ev = self._paused_event or asyncio.Event()
            try:
                await asyncio.wait_for(ev.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                if self._task_id:
                    await self._check_active()
            # 被唤醒后清空事件, 若仍处于暂停状态则继续等待
            if self._paused:
                self._paused_event = asyncio.Event()


# ════════════════ 报告沉淀(同步,运行在线程池) ════════════════

def _call_raw_for_task(agent: DeepSeekAgent, task_id: int, user_id: int, *,
                       usage_file_id: Optional[int] = None, usage_chunk_index: Optional[int] = None, **kwargs):
    """线程池从持久任务恢复来源；独立提交用量，不被报告解析失败回滚。"""
    call_budget = _roundtable_call_budget.get()
    if not task_id:
        if call_budget:
            call_budget.reserve()
        return agent.call_raw(**kwargs)
    log_db = SessionLocal()
    try:
        task = log_db.get(ReviewTask, task_id)
        if task is None or task.user_id != user_id:
            raise RuntimeError("圆桌模型调用缺少可信任务来源")
        fields = {
            **model_attribution(task), "_review_task_id": task_id,
            "_file_id": usage_file_id, "_chunk_index": usage_chunk_index,
        }
        with usage_context(user_id, fields, db=log_db):
            try:
                if call_budget:
                    call_budget.reserve()
                return agent.call_raw(**kwargs)
            finally:
                log_db.commit()
    finally:
        log_db.close()


def _create_review_task(
    *, user_id: int, project_id: int, file_id: int, file_name: str,
    code: str, language: str, review_type: str, model_name: str,
    profiles: tuple[ReviewAgentProfile, ...],
    usage_origin: Optional[dict[str, int]] = None,
) -> int:
    """把实际输入匹配的历史版本与 running 任务原子保存，禁止改用新内容。"""
    db = SessionLocal()
    try:
        validate_review_input(SimpleNamespace(content=code, file_name=file_name, is_binary=0))
        code_file = db.query(CodeFile).filter_by(
            id=file_id, project_id=project_id, status="active",
        ).with_for_update().one_or_none()
        if code_file is None:
            raise ValidationError("圆桌扫描文件不存在或已删除，请重新创建扫描", code=40001)
        versions = db.query(CodeVersion).filter_by(file_id=file_id, content=code).order_by(
            CodeVersion.version_no.desc(),
        ).all()
        version = next((item for item in versions if item.content == code), None)
        if version is None:
            raise ValidationError("圆桌输入缺少匹配的历史版本证据，请保存后重新扫描", code=40001)
        snapshot = SimpleNamespace(
            id=file_id, project_id=project_id, content=code, version_no=version.version_no,
            file_name=file_name, file_path=code_file.file_path, language=language,
            line_count=len(code.splitlines()), is_binary=code_file.is_binary,
        )
        task = ReviewTask(
            user_id=user_id,
            **(usage_origin if usage_origin is not None else current_attribution(user_id)),
            project_id=project_id,
            task_name=f"{file_name} · 圆桌讨论审",
            review_type="discuss",
            status="running",
            total_files=1,
            processed_files=0,
            model_name=f"{model_name}/discuss",
            rules_snapshot=[{"code": p.code, "name": p.name} for p in profiles],
            coverage={"stage": "running", "input_files": 1, "source_chars": len(code)},
            start_time=datetime.now(timezone.utc),
        )
        db.add(task)
        db.flush()
        freeze_task_inputs(db, task.id, [snapshot])
        db.commit()
        return task.id
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _cancel_review_task(task_id: int) -> int:
    """登录失效取消圆桌时只记录终态，不生成问题、报告或调用日志。"""

    db = SessionLocal()
    try:
        task = (
            db.query(ReviewTask).filter_by(id=task_id, status="running")
            .with_for_update().populate_existing().one_or_none()
        )
        if task:
            task.status = "cancelled"
            task.summary = "圆桌讨论已取消，未生成完整审查结论。"
            task.coverage = {**(task.coverage or {}), "stage": "cancelled"}
            task.end_time = datetime.now(timezone.utc)
            db.commit()
        return task_id
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _finalize_review(
    *, task_id: int, user_id: int, file_id: int, file_name: str,
    all_turns: list[DiscussionTurn], code: str, language: str,
    deferred_logs: list[dict], agent: DeepSeekAgent, stopped: bool,
    consensus: str = "", coverage: Optional[dict] = None,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
) -> int:
    """讨论结束: 补写 AiCallLog + 抽取结构化问题 + 收尾 ReviewTask。返回 task_id。"""
    db = SessionLocal()
    t0 = time.time()
    valid_turns = [turn for turn in all_turns if turn.role == "agent"
                   and turn.agent_code != "orchestrator" and turn.action == "speak" and turn.content.strip()]
    failed_turns = sum(log.get("status") == "failed" for log in deferred_logs)
    metrics = dict(coverage) if coverage is not None else {
        "expected_turns": len(valid_turns), "attempted_turns": len(valid_turns),
        "successful_turns": max(0, len(valid_turns) - failed_turns), "failed_turns": failed_turns,
        "valid_speeches": len(valid_turns) if not failed_turns else max(0, len(valid_turns) - failed_turns),
        "summary_status": "success" if consensus else "unknown",
    }
    call_budget = _roundtable_call_budget.get()
    if call_budget:
        metrics.update(model_calls_limit=call_budget.limit, model_calls_used=call_budget.used)
    progress_base = int(metrics.get("attempted_turns") or 0) + 1
    extraction_progress = {"completed": 0, "total": 1}

    def report_progress(phase: str, completed: int, total: int) -> None:
        if progress_callback:
            try:
                progress_callback(phase, completed, total)
            except Exception as exc:
                logger.warning(f"[Discussion] 报告进度回投失败: {exc}")

    def on_extraction_progress(completed: int, total: int) -> None:
        extraction_progress.update(completed=completed, total=total)
        report_progress("extracting", progress_base + completed, progress_base + total + 1)

    try:
        task = db.query(ReviewTask).filter_by(id=task_id).populate_existing().one_or_none()
        if task is None or task.status != "running":
            return task_id
        db.rollback()
        if stopped:
            return _cancel_review_task(task_id)
        if not metrics.get("valid_speeches"):
            raise RuntimeError("圆桌讨论没有产生有效审查发言，不能生成成功报告")
        _ensure_running(task_id)
        extracted_issues = _extract_issues(
            all_turns, code, language, file_name, agent, db,
            task_id, user_id, file_id, progress_callback=on_extraction_progress,
        )
        metrics["extraction_status"] = "success"
        report_progress(
            "reporting", progress_base + extraction_progress["total"],
            progress_base + extraction_progress["total"] + 1,
        )
        issues = _normalize_discussion_issues(
            extracted_issues,
            code=code,
            language=language,
            file_name=file_name,
            file_id=file_id,
        )
        task = db.query(ReviewTask).filter_by(id=task_id).with_for_update().populate_existing().one_or_none()
        if task is None or task.status != "running":
            db.rollback()
            return task_id
        # 1) 补写讨论期间的全部 LLM 调用日志(真实数据)
        for log_info in deferred_logs:
            try:
                meta = dict(log_info["meta"])
                # 让 model_name 带 agent_label 后缀,便于 Agent 中心按代理归因
                label = log_info.get("agent_label", "")
                if label and label != "general":
                    meta["model_tag"] = f"{meta.get('model_name', agent.model)}/{label}-agent"
                DeepSeekAgent.log_deferred(
                    db, task_id=task_id, user_id=user_id, file_id=file_id,
                    chunk_index=log_info.get("chunk_index", 0),
                    meta=meta, status=log_info.get("status", "success"),
                )
            except Exception as e:
                logger.debug(f"[Discussion] 补写 AiCallLog 失败: {e}")

        # 2) 抽取结构化问题
        issue_rows: list[ReviewIssue] = []
        for it in issues:
            issue_rows.append(ReviewIssue(
                task_id=task_id,
                file_id=file_id,
                file_name=file_name,
                line_number=it.line_number or 0,
                end_line=it.end_line,
                issue_type=it.issue_type,
                severity=normalize_severity(it.severity),
                title=it.title or "",
                description=it.description or "",
                suggestion=it.suggestion or "",
                fixed_code=it.fixed_code or "",
                status="unfixed",
                owasp=it.owasp,
                cwe=it.cwe,
                evidence=it.evidence,
                exploit_scenario=it.exploit_scenario,
                references_json=it.references if it.references else None,
                confidence=it.confidence,
                source=it.source,
                source_details=it.source_details if it.source_details else None,
                confirmation_count=it.confirmation_count,
                finding_fingerprint=it.finding_fingerprint or None,
                cvss_score=it.cvss_score,
                cvss_vector=it.cvss_vector,
                cvss_version=it.cvss_version,
                cvss_source=it.cvss_source,
                compliance_mapping=it.compliance_mapping if it.compliance_mapping else None,
                remediation=it.remediation,
                static_rule_hits=it.static_rule_hits,
            ))
        if issue_rows:
            db.add_all(issue_rows)

        # 3) 统计 + 收尾任务
        sev_count = {"严重": 0, "高": 0, "中": 0, "低": 0}
        for r in issue_rows:
            severity = normalize_severity(r.severity)
            sev_count[severity] += 1

        # 共识小结作为任务 summary(由编排器直接传入主持人汇总文本)
        complete = (
            metrics.get("expected_turns", 0) > 0
            and metrics.get("attempted_turns") == metrics.get("expected_turns")
            and metrics.get("successful_turns") == metrics.get("expected_turns")
            and not metrics.get("failed_turns") and not metrics.get("errors")
            and metrics.get("summary_status") == "success"
        )
        task.processed_files = 1 if complete else 0
        task.total_issues = len(issue_rows)
        task.severe_issues = sev_count["严重"]
        task.high_issues = sev_count["高"]
        task.medium_issues = sev_count["中"]
        task.low_issues = sev_count["低"]
        score_breakdown = compute_score_breakdown(sev_count)
        task.score = int(score_breakdown["score"]) if complete else 0
        task.score_version = SCORING_VERSION if complete else None
        task.score_breakdown = score_breakdown if complete else None
        task.summary = consensus or "圆桌讨论已完成。"
        task.error_message = None if complete else "圆桌审查覆盖不完整，已保存有效部分，不能作为完整审查结论。"
        task.status = "success" if complete else "failed"
        if call_budget:
            metrics["model_calls_used"] = call_budget.used
        task.coverage = {**(task.coverage or {}), **metrics, "stage": "complete" if complete else "partial"}
        task.end_time = datetime.now(timezone.utc)
        task.duration_ms = int((time.time() - t0) * 1000)
        # 问题记录与任务成功状态必须原子提交，避免生成半份报告。
        db.commit()
        report_progress(
            "reporting", progress_base + extraction_progress["total"] + 1,
            progress_base + extraction_progress["total"] + 1,
        )
        return task_id
    except Exception as exc:
        logger.error(f"[Discussion] _finalize_review 异常: {traceback.format_exc()}")
        db.rollback()
        readable_error = str(exc).strip() or exc.__class__.__name__
        if metrics.get("extraction_status") != "success":
            metrics["extraction_status"] = (
                "partial" if extraction_progress["completed"] else "failed"
            )
        metrics["extraction_batches"] = dict(extraction_progress)
        if call_budget:
            metrics["model_calls_used"] = call_budget.used
        try:
            task = db.query(ReviewTask).filter_by(id=task_id).with_for_update().populate_existing().one_or_none()
            if task and task.status == "running":
                task.status = "failed"
                task.summary = "圆桌讨论已完成，但报告整理失败。"
                task.error_message = f"圆桌报告整理失败：{readable_error}"[:500]
                task.score = 0
                task.score_version = None
                task.score_breakdown = None
                task.coverage = {
                    **(task.coverage or {}), **metrics, "stage": "failed",
                    "error": readable_error[:500],
                }
                task.end_time = datetime.now(timezone.utc)
                task.duration_ms = int((time.time() - t0) * 1000)
                db.commit()
                report_progress(
                    "failed", progress_base + extraction_progress["completed"],
                    progress_base + extraction_progress["total"] + 1,
                )
        except Exception as status_exc:
            db.rollback()
            logger.error(f"[Discussion] 标记圆桌报告失败状态异常: {status_exc}")
        return task_id
    finally:
        db.close()


def _discussion_code_windows(code: str, *, max_chars: int = _EXTRACTION_CODE_WINDOW_CHARS) -> list[str]:
    """按完整源码行构建有绝对行号的证据窗口；不丢弃任何一行。"""
    if max_chars < 128:
        raise RuntimeError("圆桌代码窗口小于最小可用容量")
    lines = code.splitlines()
    if not lines:
        return [code]
    windows: list[str] = []
    current: list[str] = []
    current_size = 0
    for line_number, line in enumerate(lines, start=1):
        prefix = f"{line_number}: "
        # 单个超长源码行也分连续片段；每段都带同一绝对行号和片段序号。
        width = max_chars - len(prefix) - 32
        if width <= 0:
            raise RuntimeError("圆桌代码窗口容量不足以记录绝对行号")
        fragments = [line[offset:offset + width] for offset in range(0, len(line), width)] or [""]
        for index, fragment in enumerate(fragments, start=1):
            suffix = f" [片段 {index}/{len(fragments)}]" if len(fragments) > 1 else ""
            entry = f"{prefix}{fragment}{suffix}"
            if current and current_size + len(entry) + 1 > max_chars:
                windows.append("\n".join(current))
                current = []
                current_size = 0
            current.append(entry)
            current_size += len(entry) + 1
    if current:
        windows.append("\n".join(current))
    return windows


def _compress_roundtable_history(
    records: list[tuple[str, str]],
    *,
    agent: DeepSeekAgent,
    task_id: int,
    user_id: int,
    file_id: int,
    target_tokens: int,
) -> str:
    """按来源分块做有引文校验的语义摘要；原始发言保持在会话记录中。"""
    if target_tokens <= 0:
        raise RuntimeError("圆桌输入没有可用的讨论历史预算")
    requested_tokens = target_tokens
    latest_user = next(
        ((key, content) for key, content in reversed(records) if "【用户·" in content),
        None,
    )
    preserved: list[tuple[str, str]] = []
    latest_user_projection = (
        f"【来源 {latest_user[0]}】\n{latest_user[1]}" if latest_user else ""
    )
    if latest_user and estimate_tokens(latest_user_projection) <= target_tokens // 3:
        preserved = [latest_user]
        records = [record for record in records if record[0] != latest_user[0]]
        target_tokens -= estimate_tokens(latest_user_projection)
    context_window = settings.deepseek_context_window_tokens
    initial_budget = min(2048, max(256, context_window // 6))
    ceiling = _clamp_max_tokens(settings.deepseek_max_output_tokens)
    budgets = [min(initial_budget, ceiling)]
    next_budget = min(budgets[0] * 2, ceiling, max(256, context_window // 4))
    if next_budget > budgets[0]:
        budgets.append(next_budget)
    input_limit = min(8000, max(512, (context_window - budgets[-1]) // 3))
    part_chars = max(128, input_limit // 3)
    pieces: list[tuple[str, str]] = []
    for source_id, content in records:
        if estimate_tokens(content) <= input_limit:
            pieces.append((source_id, content))
            continue
        # 单条发言也按连续片段覆盖；每段保留相同来源及 part 序号。
        for part_index, offset in enumerate(range(0, len(content), part_chars), start=1):
            pieces.append((f"{source_id}.{part_index}", content[offset:offset + part_chars]))

    groups: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    current_tokens = 0
    for piece in pieces:
        cost = estimate_tokens(piece)
        if current and current_tokens + cost > input_limit:
            groups.append(current)
            current = []
            current_tokens = 0
        current.append(piece)
        current_tokens += cost
    if current:
        groups.append(current)

    system_prompt = (
        "你是代码审查圆桌的历史压缩器。逐条保留每个来源的核心问题、代码行号、"
        "证据、用户要求、赞同与反驳关系；不得自行新增事实。"
        "输出 JSON 对象 {\"entries\":[{\"source_id\":\"原 ID\",\"summary\":\"简洁中文摘要\","
        "\"quotes\":[\"从对应原文逐字复制的短引文\"]}]}。"
        "每个输入 ID 恰好出现一次，至少给一段可逐字核验的引文。"
    )
    request_count = 0

    original_pieces = dict(pieces)

    def summarize(group: list[tuple[str, str]], *, level: int = 1) -> list[tuple[str, str]]:
        nonlocal request_count
        payload = "\n\n".join(f"【来源 {key}】\n{text}" for key, text in group)
        level_prompt = system_prompt + (
            "请把上一层摘要进一步压紧为每来源一句话，并保留原始引文；来源 ID 不得合并或删减。"
            if level > 1 else ""
        )
        last_error: Exception | None = None
        for budget in budgets:
            request_count += 1
            if request_count > _EXTRACTION_MAX_REQUESTS:
                raise RuntimeError("圆桌历史语义压缩达到请求上限，未生成完整投影")
            budget_error = _roundtable_input_budget_error(
                level_prompt, payload, max_output_tokens=budget,
            )
            if budget_error:
                last_error = RuntimeError(budget_error)
                break
            try:
                raw, _meta = _call_raw_for_task(
                    agent, task_id, user_id,
                    usage_file_id=file_id,
                    usage_chunk_index=9200 + request_count,
                    system_prompt=level_prompt,
                    user_prompt=payload,
                    agent_label="general",
                    json_mode=True,
                    max_tokens=budget,
                )
                parsed = json.loads(raw)
                entries = parsed.get("entries") if isinstance(parsed, dict) else None
                expected = {key: text for key, text in group}
                if not isinstance(entries, list) or len(entries) != len(expected):
                    raise ValueError("压缩摘要未覆盖全部来源")
                result: list[tuple[str, str]] = []
                seen: set[str] = set()
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ValueError("压缩摘要条目格式无效")
                    source_id = str(entry.get("source_id") or "")
                    summary = str(entry.get("summary") or "").strip()
                    quotes = entry.get("quotes")
                    if source_id not in expected or source_id in seen or not summary:
                        raise ValueError("压缩摘要来源缺失、重复或内容为空")
                    if not isinstance(quotes, list) or not quotes or not all(
                        isinstance(quote, str) and quote.strip()
                        and quote in expected[source_id]
                        and quote in original_pieces[source_id] for quote in quotes
                    ):
                        raise ValueError("压缩摘要证据引文无法从原发言核验")
                    seen.add(source_id)
                    source_label = original_pieces[source_id].split("】", 1)[0].lstrip("【")
                    result.append((source_id, f"【来源 {source_id} · {source_label}】{summary} 原文引文："
                                   + "；".join(f"「{quote}」" for quote in quotes)))
                if seen != set(expected):
                    raise ValueError("压缩摘要遗漏来源")
                return result
            except (DeepSeekOutputTruncatedError, ValueError, TypeError) as exc:
                last_error = exc
        if len(group) > 1:
            midpoint = len(group) // 2
            return summarize(group[:midpoint], level=level) + summarize(group[midpoint:], level=level)
        raise RuntimeError(
            f"圆桌历史来源 {group[0][0]} 无法完整语义压缩：{last_error}"
        ) from last_error

    projected_entries = [item for group in groups for item in summarize(group)]
    # 首层是逐条压缩；仍超预算时再压缩摘要本身。每层都核验 ID 和原文引文。
    for level in (2, 3):
        projected = "\n".join(text for _key, text in projected_entries)
        if estimate_tokens(projected) <= target_tokens:
            break
        next_entries: list[tuple[str, str]] = []
        current_group: list[tuple[str, str]] = []
        current_tokens = 0
        for entry in projected_entries:
            cost = estimate_tokens(entry)
            if current_group and current_tokens + cost > input_limit:
                next_entries.extend(summarize(current_group, level=level))
                current_group = []
                current_tokens = 0
            current_group.append(entry)
            current_tokens += cost
        if current_group:
            next_entries.extend(summarize(current_group, level=level))
        if {key for key, _text in next_entries} != set(original_pieces):
            raise RuntimeError("圆桌分层摘要遗漏来源；原始记录已保留")
        projected_entries = next_entries

    projected = "\n".join(text for _key, text in projected_entries)
    if estimate_tokens(projected) > target_tokens:
        raise RuntimeError(
            f"圆桌历史经三层语义摘要仍需 {estimate_tokens(projected)} tokens，"
            f"超过可用 {target_tokens} tokens；原始记录已保留，不能静默删减证据"
        )
    result = "\n".join(
        [projected] + [f"【来源 {key}】\n{content}" for key, content in preserved]
    ).strip()
    if estimate_tokens(result) > requested_tokens:
        raise RuntimeError(
            f"圆桌历史投影仍需 {estimate_tokens(result)} tokens，"
            f"超过可用 {requested_tokens} tokens；原始记录已保留"
        )
    return result


def _extract_issues(
    all_turns, code, language, file_name, agent, db,
    task_id, user_id, file_id,
    progress_callback: Optional[Callable[[int, int], None]] = None,
):
    """分批抽取圆桌结论；截断时加预算或拆分，所有批次完成才返回成功。"""
    agent_turns = [
        turn for turn in all_turns
        if turn.role == "agent"
        and turn.agent_code != "orchestrator"
        and getattr(turn, "action", "speak") == "speak"
    ]
    if not agent_turns:
        return []
    eligible_turn_ids = {id(turn) for turn in agent_turns}

    system = (
        "你是代码审查记录员。请把下面圆桌讨论中各专家达成共识的问题,整理成结构化 "
        "JSON。只保留确有依据的问题,合并重复项,以专家最终判断为准。"
        "每批只抽取标为本批目标的专家发言提及的问题；其余发言用于核对后续反驳和最终判断，"
        "被后续证据否定的问题不要输出。用户发言仅作为判断约束，不要凭用户提问新增问题。"
        "代码窗口的行号为源文件绝对行号；未出现在窗口中的证据不得编造。\n"
        "严格输出 JSON 对象,格式: "
        '{"issues":[{"issue_type":"安全漏洞|潜在Bug|性能问题|代码规范|命名规范|'
        '异常处理|可维护性|注释完整性|其他","severity":"严重|高|中|低",'
        '"title":"简短标题","line_number":行号或0,"end_line":结束行号或null,'
        '"description":"问题说明","suggestion":"修复建议","fixed_code":"修复示例或空",'
        '"owasp":"OWASP编号或空","cwe":"CWE编号或空","evidence":"直接来自代码的证据行",'
        '"exploit_scenario":"攻击场景或空","references":[],"confidence":0.9,'
        '"cvss_score":9.8或null,"cvss_vector":"完整CVSS v3.1基础向量或空",'
        '"remediation":"详细修复步骤或空"}]}。'
        '有合法向量时必须透传;缺失时保持 null/空,不得按严重度猜分。'
        '不要输出 JSON 以外的任何内容。'
    )
    ceiling = _clamp_max_tokens(settings.deepseek_max_output_tokens)
    first_budget = min(
        _EXTRACTION_INITIAL_OUTPUT_TOKENS, ceiling,
        max(512, settings.deepseek_context_window_tokens // 4),
    )
    retry_budget = min(first_budget * 2, ceiling)
    budgets = [first_budget] + ([retry_budget] if retry_budget > first_budget else [])
    request_count = 0
    planned_batches = 1
    completed_batches = 0
    collected: list[Issue] = []

    def announce() -> None:
        if progress_callback is not None:
            progress_callback(completed_batches, planned_batches)

    def records_for(group: list[DiscussionTurn]) -> list[tuple[str, str]]:
        selected = {id(turn) for turn in group}
        return [
            (
                f"S{ordinal:04d}-T{turn.turn_id}",
                f"【{'用户' if turn.role == 'user' else '本批目标' if id(turn) in selected else '其他专家上下文'}"
                f"·{turn.agent_name}#{turn.turn_id}】{turn.content}",
            )
            for ordinal, turn in enumerate(all_turns, start=1)
            if turn.role == "user" or id(turn) in eligible_turn_ids
        ]

    def run_batch(group: list[DiscussionTurn], code_view: str, *, windowed: bool = False) -> None:
        nonlocal request_count, planned_batches, completed_batches
        records = records_for(group)
        history = "\n\n".join(f"【来源 {source_id}】\n{content}" for source_id, content in records)
        selected_ids = {id(turn) for turn in group}
        target_sources = [
            f"S{ordinal:04d}-T{turn.turn_id}"
            for ordinal, turn in enumerate(all_turns, start=1)
            if id(turn) in selected_ids
        ]
        fence = chr(96) * 3
        scope = "代码片段（行号是源文件绝对行号）" if windowed else "完整代码"

        def prompt_for(projected_history: str) -> str:
            return (
                f"## 审查文件: {file_name}({language}) · {scope}\n"
                f"{fence}{language}\n{code_view}\n{fence}\n\n"
                f"## 完整讨论记录\n{projected_history}\n\n"
                f"本批目标来源 ID：{', '.join(target_sources)}。"
                "请结合后续反驳，只抽取本批目标发言中最终成立的问题为 JSON。"
            )

        user_prompt = prompt_for(history)
        budget_error = _roundtable_input_budget_error(
            system, user_prompt, max_output_tokens=budgets[-1],
        )
        if budget_error:
            if not windowed and len(code_view) > _EXTRACTION_CODE_WINDOW_CHARS:
                windows = _discussion_code_windows(code_view)
                if len(windows) > 1:
                    planned_batches += len(windows) - 1
                    announce()
                    for window in windows:
                        run_batch(group, window, windowed=True)
                    return
            available = settings.deepseek_context_window_tokens - budgets[-1] - 1024
            history_budget = (
                available - estimate_tokens(system)
                - estimate_tokens(user_prompt) + estimate_tokens(history)
            )
            try:
                history = _compress_roundtable_history(
                    records, agent=agent, task_id=task_id, user_id=user_id,
                    file_id=file_id, target_tokens=history_budget,
                )
            except Exception as exc:
                raise RuntimeError(f"结构化问题抽取失败：{exc}") from exc
            user_prompt = prompt_for(history)
            remaining_error = _roundtable_input_budget_error(
                system, user_prompt, max_output_tokens=budgets[-1],
            )
            if remaining_error:
                raise RuntimeError(f"结构化问题抽取失败：{remaining_error}")
        last_truncation: DeepSeekOutputTruncatedError | None = None
        for budget in budgets:
            request_count += 1
            if request_count > _EXTRACTION_MAX_REQUESTS:
                raise RuntimeError(
                    f"结构化问题抽取失败：达到 {_EXTRACTION_MAX_REQUESTS} 次请求上限；"
                    f"已完成 {completed_batches}/{planned_batches} 批，不能标记完整报告"
                )
            try:
                raw, meta = _call_raw_for_task(
                    agent, task_id, user_id,
                    usage_file_id=file_id,
                    usage_chunk_index=9100 + completed_batches,
                    system_prompt=system,
                    user_prompt=user_prompt,
                    agent_label="general",
                    json_mode=True,
                    max_tokens=budget,
                )
            except DeepSeekOutputTruncatedError as exc:
                last_truncation = exc
                continue
            except Exception as exc:
                raise RuntimeError(
                    f"结构化问题抽取失败：已完成 {completed_batches}/{planned_batches} 批；{exc}"
                ) from exc

            try:
                parsed = parse_issues(raw).issues
            except Exception as exc:
                raise RuntimeError(f"结构化问题解析失败：{exc}") from exc
            try:
                DeepSeekAgent.log_deferred(
                    db, task_id=task_id, user_id=user_id, file_id=file_id,
                    chunk_index=9100 + completed_batches, meta=meta, status="success",
                )
            except Exception as exc:
                logger.debug(f"[Discussion] 补写结构化抽取日志失败: {exc}")
            collected.extend(parsed)
            completed_batches += 1
            announce()
            return

        if len(group) > 1:
            midpoint = len(group) // 2
            planned_batches += 1
            announce()
            run_batch(group[:midpoint], code_view, windowed=windowed)
            run_batch(group[midpoint:], code_view, windowed=windowed)
            return
        if not windowed and len(code_view) > _EXTRACTION_CODE_WINDOW_CHARS:
            windows = _discussion_code_windows(code_view)
            if len(windows) > 1:
                planned_batches += len(windows) - 1
                announce()
                for window in windows:
                    run_batch(group, window, windowed=True)
                return
        raise RuntimeError(
            "结构化问题抽取失败：模型输出仍被截断，"
            f"已完成 {completed_batches}/{planned_batches} 批；不能标记完整报告"
        ) from last_truncation

    announce()
    run_batch(agent_turns, code)
    if completed_batches != planned_batches:
        raise RuntimeError(
            f"结构化问题抽取覆盖不完整：{completed_batches}/{planned_batches} 批"
        )
    return collected


def _normalize_discussion_issues(
    extracted: list[Issue],
    *,
    code: str,
    language: str,
    file_name: str,
    file_id: int,
) -> list[Issue]:
    """Send roundtable and static results through the canonical N-way merger."""
    roundtable_issues = [
        replace(
            issue,
            severity=normalize_severity(issue.severity),
            source="llm:roundtable",
            static_rule_hits=0,
            finding_fingerprint="",
        )
        for issue in extracted
    ]
    static_findings = static_scan(
        content=code,
        file_name=file_name,
        language=language,
    )
    return merge_findings_and_issues(
        static_findings,
        roundtable_issues,
        file_id,
        code=code,
        language=language,
    )
