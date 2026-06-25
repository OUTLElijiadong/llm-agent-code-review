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
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

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
from app.ai.deepseek_agent import DeepSeekAgent
from app.ai.multi_agent import ReviewAgentProfile
from app.ai.result_parser import parse as parse_issues
from app.ai.scoring import compute_score
from app.core.database import SessionLocal
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile

# 讨论画像 code → 注册中心 BaseAgent code(与 review_service 保持一致),
# 用于向 Agent 办公室广播事件时点亮正确的工位卡。
_PROFILE_TO_AGENT_CODE: dict[str, str] = {
    "general": "code_reviewer",
    "security": "security_sentinel",
    "reliability": "code_reviewer",
    "performance": "code_reviewer",
    "maintainability": "code_reviewer",
}


class DiscussionOrchestrator:
    """发言式讨论编排器 — 每个 Agent 讲话时能看到前面所有发言并可反驳"""

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
        self._trace_id = new_trace_id()
        all_turns: list[DiscussionTurn] = []
        deferred_logs: list[dict] = []
        turn_counter = 0
        summary_text = ""
        agent = DeepSeekAgent()
        loop = asyncio.get_running_loop()

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
        try:
            task_id = await loop.run_in_executor(
                None,
                lambda: _create_review_task(
                    user_id=user_id, project_id=project_id, file_name=file_name,
                    file_id=file_id, review_type=review_type,
                    model_name=agent.model, profiles=profiles,
                ),
            )
        except Exception:
            logger.warning(f"[Discussion] 创建 ReviewTask 失败: {traceback.format_exc()}")
        self._task_id = task_id

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
                f"共 {max_rounds} 轮讨论。请各位 Agent 畅所欲言,"
                f"对他人的观点不必客气,有不同意见请直接指出并说明理由。"
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
            await asyncio.sleep(0.5)

            for round_idx in range(max_rounds):
                if session.status != "active":
                    break
                await self._wait_if_paused(session_id)

                bus.publish_control(session_id, "round_start", {
                    "round": round_idx + 1,
                    "total_rounds": max_rounds,
                })

                # ── 逐一轮流发言 (类聊天室) ──
                for speaker_idx, profile in enumerate(profiles):
                    if session.status != "active":
                        break
                    await self._wait_if_paused(session_id)

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

                    content, meta, ok = await self._speaker_turn(
                        agent=agent,
                        profile=profile,
                        code=code,
                        language=language,
                        file_name=file_name,
                        all_turns=all_turns,
                        user_inputs=self._user_inputs,
                        round_idx=round_idx,
                        speaker_idx=speaker_idx,
                    )
                    if meta:
                        deferred_logs.append({
                            "meta": meta,
                            "status": "success" if ok else "failed",
                            "chunk_index": round_idx * 100 + speaker_idx,
                            "agent_label": profile.code,
                        })
                    self._emit(
                        AgentEventType.COMPLETE if ok else AgentEventType.FAILED,
                        target_code,
                        f"{profile.name} 完成第 {round_idx+1} 轮发言",
                    )

                    turn_counter += 1
                    turn = DiscussionTurn(
                        turn_id=turn_counter,
                        agent_code=profile.code,
                        agent_name=profile.name,
                        role="agent",
                        content=content.strip() or "(本轮未发现问题)",
                    )
                    all_turns.append(turn)
                    bus.publish_turn(session_id, turn)
                    # v2.4 B1: Agent 发言 publish 到 MetaGPT Environment
                    self._publish_to_env(
                        speaker=profile.code,
                        content=turn.content,
                        turn_id=turn_counter,
                    )

                self._user_inputs = []

            # ── 主持人汇总 (线程池执行,避免阻塞事件循环) ──
            stopped = session.status != "active"
            self._emit(AgentEventType.THINKING, "orchestrator", "主持人正在汇总讨论共识")
            summary_text, summary_meta = await loop.run_in_executor(
                None,
                lambda: self._summarize(
                    all_turns, code, language, file_name, agent, stopped,
                ),
            )
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
            self._emit(AgentEventType.COMPLETE, "orchestrator", "主持人已汇总共识")

        except Exception:
            logger.error(f"[Discussion] 异常: {traceback.format_exc()}")
        finally:
            # ── 沉淀报告: 写调用日志 + 抽取问题 + 收尾 ReviewTask ──
            report_task_id = 0
            if task_id:
                stopped = session.status != "active"
                try:
                    report_task_id = await loop.run_in_executor(
                        None,
                        lambda: _finalize_review(
                            task_id=task_id,
                            user_id=user_id,
                            file_id=file_id,
                            file_name=file_name,
                            all_turns=all_turns,
                            code=code,
                            language=language,
                            deferred_logs=deferred_logs,
                            agent=agent,
                            stopped=stopped,
                            consensus=summary_text,
                        ),
                    )
                except Exception:
                    logger.error(f"[Discussion] 收尾报告失败: {traceback.format_exc()}")
            sess = bus.get_session(session_id)
            if sess:
                sess.report_task_id = report_task_id
            bus.publish_control(session_id, "done", {"task_id": report_task_id})
            bus.close_session(session_id)

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
    ) -> tuple[str, Optional[dict], bool]:
        """一个 Agent 的一轮发言 — 能看到之前全部发言并可反驳

        Returns:
            (发言文本, AiCallLog meta, 是否成功)
        """
        loop = asyncio.get_running_loop()
        is_first = not all_turns or all(t.role == "user" for t in all_turns)

        system = (
            f"你是代码审查专家「{profile.name}」,正在参加代码审查圆桌讨论。\n\n"
            f"文件: {file_name}({language})\n"
            f"你的专业领域: {profile.focus}\n"
            f"你的重点问题类型: {' / '.join(profile.issue_types)}\n\n"
            f"发言规则:\n"
            f"1. 每次只输出一段中文发言(100-300字),用自然语言说出你的观点;\n"
            f"2. 如果你是第一个发言者: 直接报告你发现的 ≤3 个最严重的问题,带上代码行号;\n"
            f"3. 如果前面已有其他人发言: **必须先对他们的观点表态** —— 明确指出你"
            f"「同意 / 补充 / 质疑 / 反驳」其中哪一个,并给出理由(例如「我不同意XX代理"
            f"把这条定为严重,因为…」「XX代理漏掉了…」);然后再补充你的独立发现;\n"
            f"4. 鼓励良性争论: 发现别人的判断有误、严重度评估过高或过低、定位不准时,"
            f"请直接反驳并说明依据,不要为了客气而附和;\n"
            f"5. 用具体代码行号和内容支撑你的观点;\n"
            f"6. 不要逐字重复别人已说清的问题,可在其基础上深化;\n"
            f"7. 输出纯文本,不要 JSON,不要 Markdown 标题。"
        )

        history_text = self._build_history(all_turns)
        user_text = ""
        if user_inputs:
            user_text = "\n## ⚡ 用户最新指示\n" + "\n".join(
                f"- {u}" for u in user_inputs[-3:]
            ) + "\n请优先回应用户的指示。"

        stance_hint = (
            "你是第一位发言者,请直接给出你的发现。"
            if is_first
            else "请先对上面其他人的发言表态(同意/补充/质疑/反驳并说明理由),再补充你的独立发现。"
        )
        user_prompt = (
            f"## 待审查代码\n```{language}\n{code}\n```\n\n"
            f"{history_text}\n"
            f"{user_text}\n\n"
            f"## 现在轮到你「{profile.name}」发言了\n{stance_hint}"
        )

        try:
            content, meta = await loop.run_in_executor(
                None,
                lambda: agent.call_raw(
                    system_prompt=system,
                    user_prompt=user_prompt,
                    agent_label=profile.code,
                    json_mode=False,
                ),
            )
            return content.strip(), meta, True
        except Exception as e:
            logger.warning(f"[Discuss] {profile.code} 发言失败: {e}")
            return f"(抱歉,发言时遇到技术问题: {str(e)[:80]})", None, False

    def _build_history(self, turns: list[DiscussionTurn]) -> str:
        if not turns:
            return "## 讨论记录\n(你是第一位发言者,开始你的表演吧！)"

        parts = ["## 讨论记录 (按发言时间顺序)"]
        for t in turns:
            role_label = "用户" if t.role == "user" else f"{t.agent_name}({t.agent_code})"
            parts.append(
                f"\n### {role_label}\n{t.content}",
            )
        return "\n".join(parts)

    def _summarize(self, turns: list[DiscussionTurn], code: str,
                   language: str, file_name: str, agent: DeepSeekAgent,
                   stopped: bool = False) -> tuple[str, Optional[dict]]:
        """生成主持人共识小结。Returns (文本, AiCallLog meta)。"""
        prefix = "🛑 讨论已被用户终止。\n\n" if stopped else ""
        agent_turns = [t for t in turns if t.role == "agent"]
        if not agent_turns:
            return prefix + "本次讨论没有产生有效发言。", None

        history = "\n\n".join(
            f"【{'用户' if t.role == 'user' else t.agent_name}】{t.content}"
            for t in turns
        )
        try:
            raw, meta = agent.call_raw(
                system_prompt=(
                    "你是代码审查圆桌讨论的主持人。请用简洁的中文自然语言汇总各位专家的发言,"
                    "形成一份结论性总结(不要输出 JSON)。要求:\n"
                    "1. 先用一句话给出代码总体评价;\n"
                    "2. 然后用 Markdown 有序列表列出大家达成共识的关键问题"
                    "(每条注明所属维度、严重程度,并尽量带上代码行号);\n"
                    "3. 如讨论中存在分歧,用一句话说明争议点与结论;\n"
                    "4. 最后给出 1-2 句改进优先级建议。\n"
                    "总长度控制在 450 字以内。"
                ),
                user_prompt=(
                    f"## 审查文件: {file_name}({language})\n```{language}\n{code}\n```\n\n"
                    f"## 讨论发言记录\n{history}\n\n请汇总共识。"
                ),
                agent_label="general",
                json_mode=False,
            )
            body = raw.strip()
            if body:
                return f"{prefix}📋 **讨论共识小结**\n\n{body[:2000]}", meta
        except Exception as e:
            logger.warning(f"[Discussion] 共识失败: {e}")

        # 回退: 简单统计
        lines = [f"{prefix}📋 讨论结束 (共 {len(agent_turns)} 条发言):", ""]
        agent_issues: dict[str, list[str]] = {}
        for t in agent_turns:
            agent_issues.setdefault(t.agent_name, []).append(
                t.content[:100] + ("..." if len(t.content) > 100 else ""),
            )
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
        """向 Agent 办公室广播讨论事件(失败静默,不影响讨论)"""
        try:
            AgentEventBus.instance().publish(AgentEvent(
                type=type_,
                agent=agent_code,
                trace_id=self._trace_id,
                message=message,
                payload={"task_id": getattr(self, "_task_id", 0),
                         "user_id": getattr(self, "_user_id", 0),
                         "source": "discussion"},
            ))
        except Exception:
            pass

    # ── 用户控制 ──

    _session_id: str = ""
    _paused: bool = False
    _paused_event: Optional[asyncio.Event] = None
    _user_inputs: list[str] = []
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
            self._bus.publish_control(self._session_id, "stopping", {})
            if self._paused_event:
                self._paused_event.set()

    async def _wait_if_paused(self, session_id: str):
        while self._paused:
            ev = self._paused_event or asyncio.Event()
            await ev.wait()
            # 被唤醒后清空事件, 若仍处于暂停状态则继续等待
            if self._paused:
                self._paused_event = asyncio.Event()


# ════════════════ 报告沉淀(同步,运行在线程池) ════════════════

def _create_review_task(
    *, user_id: int, project_id: int, file_id: int, file_name: str,
    review_type: str, model_name: str,
    profiles: tuple[ReviewAgentProfile, ...],
) -> int:
    """讨论开始时创建 ReviewTask(running),返回 task_id。"""
    db = SessionLocal()
    try:
        task = ReviewTask(
            user_id=user_id,
            project_id=project_id,
            task_name=f"{file_name} · 圆桌讨论审",
            review_type="discuss",
            status="running",
            total_files=1,
            processed_files=0,
            model_name=f"{model_name}/discuss",
            rules_snapshot=[{"code": p.code, "name": p.name} for p in profiles],
            start_time=datetime.now(timezone.utc),
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        db.add(ReviewTaskFile(task_id=task.id, file_id=file_id))
        db.commit()
        return task.id
    finally:
        db.close()


def _finalize_review(
    *, task_id: int, user_id: int, file_id: int, file_name: str,
    all_turns: list[DiscussionTurn], code: str, language: str,
    deferred_logs: list[dict], agent: DeepSeekAgent, stopped: bool,
    consensus: str = "",
) -> int:
    """讨论结束: 补写 AiCallLog + 抽取结构化问题 + 收尾 ReviewTask。返回 task_id。"""
    db = SessionLocal()
    t0 = time.time()
    try:
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
        issues = _extract_issues(all_turns, code, language, file_name, agent, db,
                                 task_id, user_id, file_id)
        issue_rows: list[ReviewIssue] = []
        for it in issues:
            issue_rows.append(ReviewIssue(
                task_id=task_id,
                file_id=file_id,
                file_name=file_name,
                line_number=it.line_number or 0,
                end_line=it.end_line,
                issue_type=it.issue_type,
                severity=it.severity,
                title=it.title or "",
                description=it.description or "",
                suggestion=it.suggestion or "",
                fixed_code=it.fixed_code or "",
                status="unfixed",
            ))
        if issue_rows:
            db.add_all(issue_rows)
            db.commit()

        # 3) 统计 + 收尾任务
        sev_count = {"严重": 0, "高": 0, "中": 0, "低": 0}
        for r in issue_rows:
            sev_count[r.severity] = sev_count.get(r.severity, 0) + 1

        # 共识小结作为任务 summary(由编排器直接传入主持人汇总文本)
        task = db.get(ReviewTask, task_id)
        if task:
            task.processed_files = 1
            task.total_issues = len(issue_rows)
            task.severe_issues = sev_count["严重"]
            task.high_issues = sev_count["高"]
            task.medium_issues = sev_count["中"]
            task.low_issues = sev_count["低"]
            task.score = compute_score(sev_count)
            task.summary = (consensus or "圆桌讨论已完成。")[:2000]
            task.status = "success"  # 终止也算成功产出报告
            task.end_time = datetime.now(timezone.utc)
            task.duration_ms = int((time.time() - t0) * 1000)
            db.commit()
        return task_id
    except Exception:
        logger.error(f"[Discussion] _finalize_review 异常: {traceback.format_exc()}")
        db.rollback()
        # 尽量把任务标记为成功(已有发言),否则报告列表不显示
        try:
            task = db.get(ReviewTask, task_id)
            if task and task.status == "running":
                task.status = "success"
                task.summary = "圆桌讨论已完成(报告整理部分失败)。"
                task.end_time = datetime.now(timezone.utc)
                db.commit()
        except Exception:
            pass
        return task_id
    finally:
        db.close()


def _extract_issues(all_turns, code, language, file_name, agent, db,
                    task_id, user_id, file_id):
    """用 LLM 把讨论共识抽取为结构化问题列表(复用 result_parser)。"""
    agent_turns = [t for t in all_turns if t.role == "agent"
                   and t.agent_code != "orchestrator"]
    if not agent_turns:
        return []
    history = "\n\n".join(
        f"【{t.agent_name}】{t.content}" for t in all_turns
    )
    system = (
        "你是代码审查记录员。请把下面圆桌讨论中各专家达成共识的问题,整理成结构化 "
        "JSON。只保留确有依据的问题,合并重复项,以专家最终判断为准。\n"
        "严格输出 JSON 对象,格式: "
        '{"issues":[{"issue_type":"安全漏洞|潜在Bug|性能问题|代码规范|命名规范|'
        '异常处理|可维护性|注释完整性|其他","severity":"严重|高|中|低",'
        '"title":"简短标题","line_number":行号或0,"description":"问题说明",'
        '"suggestion":"修复建议"}]}。不要输出 JSON 以外的任何内容。'
    )
    user_prompt = (
        f"## 审查文件: {file_name}({language})\n```{language}\n{code}\n```\n\n"
        f"## 讨论记录\n{history}\n\n请抽取共识问题为 JSON。"
    )
    try:
        raw, meta = agent.call_raw(
            system_prompt=system, user_prompt=user_prompt,
            agent_label="general", json_mode=True,
        )
        try:
            DeepSeekAgent.log_deferred(
                db, task_id=task_id, user_id=user_id, file_id=file_id,
                chunk_index=9100, meta=meta, status="success",
            )
        except Exception:
            pass
        return parse_issues(raw).issues
    except Exception as e:
        logger.warning(f"[Discussion] 抽取问题失败: {e}")
        return []
