import json as json_lib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

import httpx
from loguru import logger

from app.core.config import settings
from app.utils.public_http import pin_public_http_url

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.agents.skills.base import BaseSkill
    from app.utils.api_resolver import ApiConfig


@dataclass
class AgentContext:
    user_id: Optional[int] = None
    task_id: Optional[int] = None
    project_id: Optional[int] = None
    file_id: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    success: bool
    data: Any = None
    error: Optional[str] = None
    model: str = ""
    duration_ms: int = 0
    tokens: Dict[str, int] = field(default_factory=dict)
    failure_kind: str = ""
    finish_reason: str = ""


class BaseAgent:
    """智能体基类

    封装 DeepSeek API 调用、重试、日志。
    每个继承的子类是一个独立智能体,负责单一职责。

    v2.0 在 v1.0 基础上新增统一元数据字段（icon/color/category/skills），
    供前端 Agent 办公室与态势感知面板渲染使用。
    """

    name: str = "base"
    description: str = ""

    # v2.0 元数据字段（前端展示用）
    icon: str = "base"
    color: str = "#5B58E8"
    category: str = "general"
    skills: tuple = ()

    def __init__(self, system_prompt: str = "", temperature: float = 0.3,
                 max_tokens: int = 4096, model: Optional[str] = None):
        self._system_prompt = system_prompt
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._model = model or settings.deepseek_model
        self._base_url = settings.deepseek_base_url
        self._api_key = settings.deepseek_api_key
        self._max_retries = settings.deepseek_max_retries
        self._timeout = settings.deepseek_timeout
        # AgentSkill 自进化与总调度升级:Skill 实例列表,由 _init_skills() 填充
        self._skills: list = []
        self._init_skills()

    def _trace_id(self, ctx: Optional[AgentContext]) -> str:
        """从 ctx 中读取调用链 trace_id,没有则生成一个孤儿 trace"""
        from app.agents.events import new_trace_id
        if ctx and ctx.extra and ctx.extra.get("trace_id"):
            return ctx.extra["trace_id"]
        return new_trace_id()

    def _emit(self, type_, ctx: Optional[AgentContext], message: str = "",
              payload: Optional[dict] = None, parent: str = "") -> None:
        """统一 emit 入口,避免每个 Agent 都 import"""
        try:
            from app.agents.event_bus import emit_event
            from app.agents.events import AgentEventType
            if not isinstance(type_, AgentEventType):
                type_ = AgentEventType(type_)
            emit_event(
                type_=type_,
                agent=self.name,
                trace_id=self._trace_id(ctx),
                parent=parent,
                message=message,
                payload=payload or {},
                user_id=ctx.user_id if ctx else None,
            )
        except Exception as e:
            logger.warning(f"[{self.name}] emit 事件失败: {e}")

    def _project_input(self, user_message: str) -> tuple[str, bool]:
        """按 1M 上下文窗口对输入做投影(超长时截断尾部并标记)。

        BaseAgent 直连 chat/completions,不经过 responses runtime 的压缩管线;
        这里按 settings.deepseek_context_window_tokens 做保守预算,防止单条
        user_message(如沙箱大日志/全量证据)顶爆模型上下文导致 400/截断。
        粗略按 1 token ≈ 2 个字符(中英混合)估算,留 8K token 头部余量。
        """
        window = int(getattr(settings, "deepseek_context_window_tokens", 1_000_000) or 1_000_000)
        budget_chars = max(8_000, (window - 8_192) * 2)
        system_len = len(self._system_prompt or "")
        if system_len + len(user_message) <= budget_chars:
            return user_message, False
        keep = max(0, budget_chars - system_len - 200)
        truncated = user_message[:keep] + "\n\n…[输入按 1M 上下文窗口投影截断]"
        return truncated, True

    def call(self, user_message: str, ctx: Optional[AgentContext] = None,
             json_mode: bool = False,
             api_config: Optional["ApiConfig"] = None,
             recover_truncation: bool = False,
             retry_reserver: Optional[Callable[[], bool]] = None,
             deadline_monotonic: Optional[float] = None,
             thinking: Optional[bool] = None) -> AgentResult:
        """调用 API (支持用户自定义配置)

        Args:
            user_message: 用户消息
            ctx: 上下文
            json_mode: 是否要求 JSON 输出
            api_config: 可选，用户自定义 API 配置；为 None 时使用系统默认
            thinking: 可选思考模式开关；None 时不写请求体，保留供应商默认

        Returns:
            AgentResult
        """
        # 解析最终使用的 API 配置：传入 > 实例默认
        from app.utils.api_resolver import normalize_ai_base_url

        base_url = normalize_ai_base_url(api_config.base_url if api_config else self._base_url)
        api_key = api_config.api_key if api_config else self._api_key
        model = api_config.model if api_config else self._model
        timeout = (
            api_config.timeout_seconds
            if api_config and api_config.timeout_seconds is not None
            else self._timeout
        )
        max_retries = (
            api_config.max_retries
            if api_config and api_config.max_retries is not None
            else self._max_retries
        )
        temperature = (
            api_config.temperature
            if api_config and api_config.temperature is not None
            else self._temperature
        )

        from app.agents.events import AgentEventType
        self._emit(AgentEventType.THINKING, ctx,
                   message=f"{self.name} 正在调用模型",
                   payload={"model": model, "json_mode": json_mode})

        messages = [{"role": "system", "content": self._system_prompt}]
        projected_message, input_truncated = self._project_input(user_message)
        messages.append({"role": "user", "content": projected_message})

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": self._max_tokens,
        }
        if input_truncated:
            self._emit(AgentEventType.PROGRESS, ctx,
                       message=f"{self.name} 输入按 1M 上下文窗口投影截断",
                       payload={"input_truncated": True})
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if thinking is not None:
            payload["thinking"] = {
                "type": "enabled" if thinking else "disabled",
            }

        last_error: Optional[str] = None
        last_failure_kind = "upstream_error"
        last_finish_reason = ""
        attempts_used = 0
        for attempt in range(max_retries + 1):
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                last_error = "模型调用超过语义审计全局时限"
                last_failure_kind = "semantic_budget_exhausted"
                break
            if attempt > 0 and retry_reserver is not None and not retry_reserver():
                last_error = "模型调用超过语义审计请求预算"
                last_failure_kind = "semantic_budget_exhausted"
                break
            attempts_used += 1
            t0 = time.time()
            retryable = True
            try:
                target = pin_public_http_url(f"{base_url}/chat/completions")
                request_timeout = float(timeout)
                if deadline_monotonic is not None:
                    request_timeout = min(
                        request_timeout,
                        max(0.1, deadline_monotonic - time.monotonic()),
                    )
                with httpx.Client(timeout=request_timeout, trust_env=False) as client:
                    resp = client.post(
                        target.request_url,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                            "Host": target.host_header,
                        },
                        json=payload,
                        extensions=target.request_extensions,
                    )
                duration_ms = int((time.time() - t0) * 1000)

                if resp.status_code == 200:
                    body = resp.json()
                    choice = body["choices"][0]
                    message_body = choice.get("message") or {}
                    finish_reason = str(choice.get("finish_reason") or "unknown")
                    last_finish_reason = finish_reason
                    usage = body.get("usage", {})
                    # reasoning_content 是模型内部推理，不是面向用户的答复。
                    # 任何 length 终止都是不完整输出，即使 content 恰好是可解析 JSON
                    # 也不得接纳，更不能原样重试同一超大请求。
                    if finish_reason == "length":
                        error = "模型输出因长度上限被截断(finish_reason=length)"
                        logger.warning(f"[{self.name}] {error}")
                        self._emit(
                            AgentEventType.PROGRESS if recover_truncation else AgentEventType.FAILED,
                            ctx,
                            message=(
                                f"{self.name} 模型输出被截断，准备缩小语义批次"
                                if recover_truncation
                                else f"{self.name} 模型输出被截断"
                            ),
                            payload={
                                "error": error,
                                "failure_kind": "output_truncated",
                                "finish_reason": finish_reason,
                                "recoverable_by_split": recover_truncation,
                            },
                        )
                        return AgentResult(
                            success=False,
                            error=f"[{self.name}] {error}",
                            model=body.get("model", self._model),
                            duration_ms=duration_ms,
                            tokens={
                                "prompt": usage.get("prompt_tokens", 0),
                                "completion": usage.get("completion_tokens", 0),
                                "total": usage.get("total_tokens", 0),
                            },
                            failure_kind="output_truncated",
                            finish_reason=finish_reason,
                        )
                    if finish_reason != "stop":
                        error = f"模型以不完整终态结束(finish_reason={finish_reason})"
                        logger.warning(f"[{self.name}] {error}")
                        self._emit(
                            AgentEventType.PROGRESS if recover_truncation else AgentEventType.FAILED,
                            ctx,
                            message=(
                                f"{self.name} 模型以不完整终态结束"
                                if recover_truncation
                                else f"{self.name} 模型输出不完整"
                            ),
                            payload={
                                "error": error,
                                "failure_kind": "incomplete_response",
                                "finish_reason": finish_reason,
                            },
                        )
                        return AgentResult(
                            success=False,
                            error=f"[{self.name}] {error}",
                            model=body.get("model", self._model),
                            duration_ms=duration_ms,
                            tokens={
                                "prompt": usage.get("prompt_tokens", 0),
                                "completion": usage.get("completion_tokens", 0),
                                "total": usage.get("total_tokens", 0),
                            },
                            failure_kind="incomplete_response",
                            finish_reason=finish_reason,
                        )
                    content = message_body.get("content")
                    if not isinstance(content, str):
                        raise TypeError("模型返回的 content 不是字符串")
                    if not content.strip():
                        raise RuntimeError(
                            f"模型未返回最终内容(finish_reason={finish_reason})"
                        )
                    logger.debug(
                        f"[{self.name}] 调用成功 duration={duration_ms}ms "
                        f"tokens={usage.get('total_tokens', '?')}"
                    )
                    # JSON 调用必须等 call_json() 完成结构解析后再广播唯一终态。
                    if not json_mode:
                        self._emit(AgentEventType.COMPLETE, ctx,
                                   message=f"{self.name} 调用完成",
                                   payload={
                                       "duration_ms": duration_ms,
                                       "total_tokens": usage.get("total_tokens", 0),
                                   })
                    return AgentResult(
                        success=True,
                        data=content,
                        model=body.get("model", self._model),
                        duration_ms=duration_ms,
                        tokens={
                            "prompt": usage.get("prompt_tokens", 0),
                            "completion": usage.get("completion_tokens", 0),
                            "total": usage.get("total_tokens", 0),
                        },
                        finish_reason=finish_reason,
                    )

                if resp.status_code == 429:
                    last_error = "请求过于频繁"
                    last_failure_kind = "rate_limited"
                elif resp.status_code >= 500:
                    last_error = f"服务异常({resp.status_code})"
                    last_failure_kind = "upstream_error"
                else:
                    last_error = f"调用失败({resp.status_code}): {resp.text[:200]}"
                    last_failure_kind = "http_error"
                    retryable = False

            except httpx.TimeoutException as e:
                last_error = str(e) or "模型请求超时"
                last_failure_kind = "timeout"
                duration_ms = int((time.time() - t0) * 1000)
            except httpx.TransportError as e:
                last_error = str(e) or "模型网络传输失败"
                last_failure_kind = "transport_error"
                duration_ms = int((time.time() - t0) * 1000)
            except Exception as e:
                last_error = str(e)
                last_failure_kind = "invalid_response"
                duration_ms = int((time.time() - t0) * 1000)
                retryable = False

            logger.warning(f"[{self.name}] 第 {attempt+1} 次尝试失败: {last_error}")
            if retryable and attempt < max_retries:
                retry_delay = float(2 ** (attempt + 1))
                if deadline_monotonic is not None:
                    remaining = deadline_monotonic - time.monotonic()
                    if remaining <= retry_delay:
                        last_error = "模型重试退避将超过语义审计全局时限"
                        last_failure_kind = "semantic_budget_exhausted"
                        break
                time.sleep(retry_delay)
            else:
                break

        self._emit(
            AgentEventType.PROGRESS if recover_truncation else AgentEventType.FAILED,
            ctx,
            message=(
                f"{self.name} 当前语义批次调用失败"
                if recover_truncation
                else f"{self.name} 调用失败"
            ),
            payload={"error": last_error, "failure_kind": last_failure_kind},
        )
        return AgentResult(
            success=False,
            error=f"[{self.name}] 调用失败({attempts_used} 次尝试): {last_error}",
            failure_kind=last_failure_kind,
            finish_reason=last_finish_reason,
        )

    def call_json(self, user_message: str, ctx: Optional[AgentContext] = None,
                  api_config: Optional["ApiConfig"] = None,
                  recover_truncation: bool = False,
                  retry_reserver: Optional[Callable[[], bool]] = None,
                  deadline_monotonic: Optional[float] = None,
                  thinking: Optional[bool] = None) -> AgentResult:
        """调用并解析为 JSON"""
        result = self.call(
            user_message,
            ctx,
            json_mode=True,
            api_config=api_config,
            recover_truncation=recover_truncation,
            retry_reserver=retry_reserver,
            deadline_monotonic=deadline_monotonic,
            thinking=thinking,
        )
        if not result.success:
            return result
        try:
            result.data = json_lib.loads(result.data)
        except (json_lib.JSONDecodeError, TypeError) as e:
            from app.agents.events import AgentEventType

            self._emit(
                AgentEventType.PROGRESS if recover_truncation else AgentEventType.FAILED,
                ctx,
                message=(
                    f"{self.name} 当前语义批次 JSON 解析失败"
                    if recover_truncation
                    else f"{self.name} JSON 解析失败"
                ),
                payload={"error": str(e), "failure_kind": "invalid_json"},
            )
            return AgentResult(
                success=False,
                error=f"[{self.name}] JSON 解析失败: {e}",
                model=result.model,
                duration_ms=result.duration_ms,
                tokens=result.tokens,
                failure_kind="invalid_json",
                finish_reason=result.finish_reason,
            )
        from app.agents.events import AgentEventType

        self._emit(
            AgentEventType.PROGRESS if recover_truncation else AgentEventType.COMPLETE,
            ctx,
            message=(
                f"{self.name} 模型响应已解析，等待审计契约校验"
                if recover_truncation
                else f"{self.name} 调用完成"
            ),
            payload={
                "duration_ms": result.duration_ms,
                "total_tokens": result.tokens.get("total", 0),
                "awaiting_contract_validation": recover_truncation,
            },
        )
        return result

    def _log_call(
        self,
        db: "Session",
        *,
        task_id: Optional[int] = None,
        user_id: Optional[int] = None,
        file_id: Optional[int] = None,
        chunk_index: Optional[int] = None,
        result: Optional[AgentResult] = None,
        status: str = "success",
        error: Optional[str] = None,
        user_prompt: str = "",
        response_text: str = "",
    ) -> None:
        """将本次 Agent 调用写入 ai_call_log 表,agent_label 填充为 self.name

        BaseAgent.call() 本身不写日志,调用方(如 review_service)在 Agent 调用
        完成后调用此方法补写 AiCallLog,实现 Agent 调用归因(AC6)。
        agent_label 字段固定为 self.name(如 code_reviewer / security_sentinel),
        使 SkillRegistry / 运维面板能按 Agent 维度统计调用情况。

        Args:
            db: 数据库会话(用于写入 AiCallLog)
            task_id: 审查任务 ID(可空)
            user_id: 发起用户 ID(可空)
            file_id: 关联文件 ID(可空)
            chunk_index: 分片索引(可空)
            result: AgentResult 对象(异常时可为 None)
            status: 日志状态(success/failed)
            error: 错误信息(失败时)
            user_prompt: 送入 LLM 的 user prompt(BaseAgent.call 路径可留空)
            response_text: LLM 原始响应文本(失败时留空)

        Returns:
            None
        """
        from app.models.ai_call_log import AiCallLog

        tokens_dict: Dict[str, int] = {}
        if result is not None and getattr(result, "tokens", None):
            tokens_dict = result.tokens if isinstance(result.tokens, dict) else {}

        model_name = (getattr(result, "model", None) if result else "") or self._model
        duration_ms = (getattr(result, "duration_ms", None) if result else None) or 0

        rec = AiCallLog(
            task_id=task_id,
            user_id=user_id,
            file_id=file_id,
            chunk_index=chunk_index,
            agent_label=self.name,
            model_name=model_name,
            prompt=(user_prompt or "")[:200_000] or None,
            response=(response_text or "")[:200_000] or None,
            status=status,
            error_message=error,
            prompt_tokens=tokens_dict.get("prompt", 0) if tokens_dict else None,
            completion_tokens=tokens_dict.get("completion", 0) if tokens_dict else None,
            total_tokens=tokens_dict.get("total", 0) if tokens_dict else None,
            duration_ms=duration_ms,
            create_time=datetime.now(timezone.utc),
        )
        try:
            db.add(rec)
            db.flush()
        except Exception as e:
            logger.debug(f"[{self.name}] _log_call 写入 AiCallLog 失败: {e}")

    # ── AgentSkill 自进化与总调度升级:Skill 挂载接口 ──

    def attach_skill(self, skill: "BaseSkill") -> None:
        """挂载 Skill 并注册到 SkillRegistry

        将 Skill 实例加入 self._skills,并同步注册到全局 SkillRegistry,
        供 Orchestrator.invoke_skill / ChatPlanner 查询调用。

        Args:
            skill: Skill 实例(BaseSkill 子类)
        """
        self._skills.append(skill)
        from app.agents.skills.registry import SkillRegistry

        SkillRegistry.instance().register(self.name, skill)

    def _init_skills(self) -> None:
        """子类 override:初始化并挂载专属 Skill

        默认空实现。子类按需 override,在方法内构造 SelfImprovement + Proactive Skill
        并调用 self.attach_skill() 挂载。此方法在 __init__ 末尾自动调用。

        Note:
            - 子类 override 时不应调用 super()._init_skills()(基类为空操作)
            - 挂载顺序建议:先 SelfImprovement,后 Proactive
        """
        pass
