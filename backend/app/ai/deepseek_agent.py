"""
DeepSeek Agent: HTTP调用、重试、日志记录

统一入口:
  chat()      — 主线程使用, 自带 AiCallLog 写入 (需要 db Session)
  call_raw()  — 并行线程使用, 不带 db, 返回 meta 供主线程事后 log_deferred() 补录
  两种方法共用同一套 HTTP/重试/超时/模型参数。

时区约定: 所有 create_time 均为 timezone-aware UTC (datetime.now(timezone.utc))。
"""
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import httpx
from loguru import logger
from sqlalchemy.orm import Session

from app.ai.exceptions import AiServiceError
from app.core.config import settings
from app.models.ai_call_log import AiCallLog
from app.utils.public_http import pin_public_http_url

if TYPE_CHECKING:
    from app.utils.api_resolver import ApiConfig

# 固定 IP 后 httpx 会以 IP 作为连接池 origin，因此必须同时按 Host/SNI 隔离池。
_SHARED_CLIENTS: dict[tuple[str, str], httpx.Client] = {}
_MAX_SHARED_CLIENTS = 32
_CLIENT_LOCK = threading.Lock()


class DeepSeekResponseError(RuntimeError):
    """DeepSeek 返回了无法安全消费的非流式响应。"""

    def __init__(self, message: str, *, finish_reason: Optional[str] = None):
        super().__init__(message)
        self.finish_reason = finish_reason


class DeepSeekOutputTruncatedError(DeepSeekResponseError):
    """DeepSeek 因 token 上限截断了响应。"""


def _parse_completion_response(resp: httpx.Response) -> tuple[str, dict, str]:
    """解析并校验非流式 Chat Completions 响应。

    这里必须在业务层解析 JSON 之前拒绝 ``finish_reason=length``。
    即使截断后的 ``content`` 恰好是合法 JSON，它也不是可信的完整结果。
    """
    try:
        body = resp.json()
    except (TypeError, ValueError) as exc:
        raise DeepSeekResponseError("DeepSeek 响应不是合法 JSON") from exc

    if not isinstance(body, dict):
        raise DeepSeekResponseError("DeepSeek 响应根节点必须是对象")

    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise DeepSeekResponseError("DeepSeek 响应缺少有效 choices[0]")

    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        raise DeepSeekOutputTruncatedError(
            "DeepSeek 输出被截断 (finish_reason=length)",
            finish_reason=finish_reason,
        )
    if not isinstance(finish_reason, str) or not finish_reason.strip():
        raise DeepSeekResponseError("DeepSeek 响应缺少有效 finish_reason")
    if finish_reason != "stop":
        raise DeepSeekResponseError(
            f"DeepSeek 输出以不完整终态结束 (finish_reason={finish_reason})",
            finish_reason=finish_reason,
        )

    message = choice.get("message")
    if not isinstance(message, dict):
        raise DeepSeekResponseError(
            "DeepSeek 响应缺少有效 message",
            finish_reason=finish_reason,
        )
    content = message.get("content")
    if not isinstance(content, str):
        raise DeepSeekResponseError(
            "DeepSeek 响应 content 必须是字符串",
            finish_reason=finish_reason,
        )
    if not content.strip():
        raise DeepSeekResponseError(
            "DeepSeek 响应 content 为空",
            finish_reason=finish_reason,
        )

    usage = body.get("usage")
    if usage is None:
        usage = {}
    if not isinstance(usage, dict):
        raise DeepSeekResponseError(
            "DeepSeek 响应 usage 必须是对象",
            finish_reason=finish_reason,
        )
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise DeepSeekResponseError(
                f"DeepSeek 响应 usage.{field} 必须是非负整数",
                finish_reason=finish_reason,
            )

    return content, usage, finish_reason


def _get_http_client(pool_key: tuple[str, str]) -> tuple[httpx.Client, bool]:
    """返回按原域名和固定 IP 隔离的 HTTP 客户端。

    返回值的第二项表示是否为一次性客户端，调用方必须关闭。
    """
    client = _SHARED_CLIENTS.get(pool_key)
    if client is not None:
        return client, False
    with _CLIENT_LOCK:
        client = _SHARED_CLIENTS.get(pool_key)
        if client is not None:
            return client, False
        client = httpx.Client(
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=40,
            ),
            timeout=httpx.Timeout(settings.deepseek_timeout),
            trust_env=False,
        )
        if len(_SHARED_CLIENTS) < _MAX_SHARED_CLIENTS:
            _SHARED_CLIENTS[pool_key] = client
            return client, False
        return client, True


def _clamp_max_tokens(max_tokens: Optional[int]) -> int:
    """输出上限钳制: 默认 4096, 最高 8192(DeepSeek chat 系安全上限)。"""
    if max_tokens is None:
        return 4096
    return max(128, min(8192, int(max_tokens)))


class DeepSeekAgent:
    """API 统一调用封装 — 支持用户自定义配置"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        api_config: Optional["ApiConfig"] = None,
    ):
        # 用户自定义配置优先
        from app.utils.api_resolver import normalize_ai_base_url

        if api_config:
            self.base_url = normalize_ai_base_url(api_config.base_url)
            self.api_key = api_config.api_key
            self.model = api_config.model
        else:
            self.base_url = normalize_ai_base_url(base_url or settings.deepseek_base_url)
            self.api_key = api_key or settings.deepseek_api_key
            self.model = model or settings.deepseek_model

        self.api_config = api_config  # 保留引用, 供协程/线程下游使用
        config_timeout = api_config.timeout_seconds if api_config and api_config.timeout_seconds is not None else None
        config_retries = api_config.max_retries if api_config and api_config.max_retries is not None else None
        config_temperature = api_config.temperature if api_config and api_config.temperature is not None else None
        self.timeout = timeout if timeout is not None else config_timeout or settings.deepseek_timeout
        self.max_retries = (
            max_retries
            if max_retries is not None
            else config_retries if config_retries is not None else settings.deepseek_max_retries
        )
        self.temperature = (
            config_temperature if config_temperature is not None else settings.deepseek_temperature
        )

    # ── 公共 HTTP 请求构造 (chat / call_raw 共用) ──

    def _build_request(
        self, system_prompt: str, user_prompt: str, json_mode: bool = True,
        max_tokens: Optional[int] = None,
    ) -> tuple:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": getattr(self, "temperature", settings.deepseek_temperature),
            "max_tokens": _clamp_max_tokens(max_tokens),
        }
        # response_format=json_object 会强制模型输出 JSON; 讨论发言需要自然语言,
        # 此时必须关闭, 否则 DeepSeek 返回空内容(或因提示缺少 "json" 而 400)。
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return url, headers, payload

    def _do_request(self, url: str, headers: dict, payload: dict) -> tuple:
        """执行一次 HTTP 请求并返回 (resp, duration_ms)

        不处理重试, 由调用方控制重试策略。
        """
        t0 = time.time()
        target = pin_public_http_url(url)
        client, close_after = _get_http_client((target.host_header, target.ip_address))
        try:
            resp = client.post(
                target.request_url,
                headers={**headers, "Host": target.host_header},
                json=payload,
                timeout=self.timeout,
                extensions=target.request_extensions,
            )
        finally:
            if close_after:
                client.close()
        return resp, int((time.time() - t0) * 1000)

    # ── 并行线程调用 (不带 db, 返回 meta 供事后补录) ──

    def call_raw(
        self,
        system_prompt: str,
        user_prompt: str,
        agent_label: str = "",
        json_mode: bool = True,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> tuple:
        """线程安全的 DeepSeek 调用 — 不写 AiCallLog

        返回 (response_text, meta_dict)。
        meta_dict 可直接传入 log_deferred() 在主线程补写日志。

        Args:
            system_prompt: 系统提示
            user_prompt: 用户提示
            agent_label: Agent 标识码
            json_mode: 是否强制 JSON 输出。审查/汇总需 True;
                圆桌讨论发言需自然语言,必须传 False。

        Returns:
            (str, dict): (LLM 原始响应文本, 完整 meta 信息)

        Raises:
            RuntimeError: 全部重试均失败
        """
        model_tag = (
            f"{self.model}/{agent_label}-agent"
            if agent_label and agent_label != "general"
            else self.model
        )
        url, headers, payload = self._build_request(
            system_prompt, user_prompt, json_mode=json_mode,
        )
        if temperature is not None:
            payload["temperature"] = max(0.0, min(2.0, float(temperature)))
        if max_tokens is not None:
            payload["max_tokens"] = max(128, min(8192, int(max_tokens)))

        for attempt in range(self.max_retries + 1):
            try:
                resp, duration_ms = self._do_request(url, headers, payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                err = f"DeepSeek 网络请求失败: {exc}"
                logger.warning(f"[call_raw] {agent_label} attempt={attempt+1} {err}")
                if attempt >= self.max_retries:
                    raise RuntimeError(
                        f"[call_raw] {agent_label} 全部 {self.max_retries+1} 次重试均失败: {err}",
                    ) from exc
                time.sleep(min(60, 2 ** (attempt + 1)))
                continue

            if resp.status_code == 200:
                content, usage, finish_reason = _parse_completion_response(resp)
                return content, {
                    "model_tag": model_tag,
                    "model_name": self.model,
                    "agent_label": agent_label,
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                    "duration_ms": duration_ms,
                    "finish_reason": finish_reason,
                    "user_prompt": user_prompt,
                    "response": content,
                    "create_time": datetime.now(timezone.utc),
                }

            if resp.status_code == 429:
                err = "DeepSeek 限流"
            elif resp.status_code >= 500:
                err = f"DeepSeek 服务异常 {resp.status_code}"
            else:
                err = f"DeepSeek {resp.status_code}: {resp.text[:200]}"
                logger.warning(f"[call_raw] {agent_label} attempt={attempt+1} {err}")
                raise RuntimeError(f"[call_raw] {agent_label} 确定性请求失败: {err}")
            logger.warning(f"[call_raw] {agent_label} attempt={attempt+1} {err}")

            if attempt >= self.max_retries:
                raise RuntimeError(
                    f"[call_raw] {agent_label} 全部 {self.max_retries+1} 次重试均失败: {err}",
                )
            time.sleep(min(60, 2 ** (attempt + 1)))

        raise RuntimeError(f"[call_raw] {agent_label} 未知错误")  # unreachable

    # ── 主线程调用 (含 AiCallLog 写入) ──

    def chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        db: Session,
        task_id: Optional[int] = None,
        user_id: Optional[int] = None,
        file_id: Optional[int] = None,
        chunk_index: Optional[int] = None,
        agent_label: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> tuple:
        """调用 DeepSeek Chat API + 写 AiCallLog"""
        model_tag = (
            f"{self.model}/{agent_label}-agent"
            if agent_label and agent_label != "general"
            else self.model
        )
        url, headers, payload = self._build_request(
            system_prompt, user_prompt, max_tokens=max_tokens,
        )

        attempt = 0
        last_err: Optional[Exception] = None
        while attempt <= self.max_retries:
            try:
                resp, duration_ms = self._do_request(url, headers, payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                err = AiServiceError(f"DeepSeek 网络请求失败: {exc}", code=50201)
                last_err = err
                self._log(
                    db, task_id=task_id, user_id=user_id, file_id=file_id,
                    chunk_index=chunk_index, prompt=user_prompt, response=None,
                    status="retry" if attempt < self.max_retries else "failed",
                    error=str(err)[:500], meta={"duration_ms": 0},
                    model_name=model_tag, agent_label=agent_label,
                )
                if attempt >= self.max_retries:
                    break
                logger.warning(f"DeepSeek 调用失败({err}), 第 {attempt+1} 次重试")
                time.sleep(min(60, 2 ** (attempt + 1)))
                attempt += 1
                continue

            if resp.status_code == 200:
                try:
                    content, usage, finish_reason = _parse_completion_response(resp)
                except DeepSeekResponseError as exc:
                    self._log(
                        db, task_id=task_id, user_id=user_id, file_id=file_id,
                        chunk_index=chunk_index, prompt=user_prompt, response=None,
                        status="failed", error=str(exc)[:500],
                        meta={
                            "duration_ms": duration_ms,
                            "finish_reason": exc.finish_reason,
                        },
                        model_name=model_tag,
                        agent_label=agent_label,
                    )
                    raise AiServiceError(
                        f"DeepSeek 响应无效: {exc}", code=50201,
                    ) from exc
                meta = {
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                    "duration_ms": duration_ms,
                    "finish_reason": finish_reason,
                }
                self._log(
                    db, task_id=task_id, user_id=user_id, file_id=file_id,
                    chunk_index=chunk_index, prompt=user_prompt, response=content,
                    status="success", error=None, meta=meta,
                    model_name=model_tag,
                    agent_label=agent_label,
                )
                return content, meta

            if resp.status_code == 429:
                err = AiServiceError("DeepSeek 限流", code=42900)
                retryable = True
            elif resp.status_code >= 500:
                err = AiServiceError(
                    f"DeepSeek 服务异常 {resp.status_code}", code=50201,
                )
                retryable = True
            else:
                err = AiServiceError(
                    f"DeepSeek 返回 {resp.status_code}: {resp.text[:200]}",
                    code=50201,
                )
                retryable = False
            last_err = err
            self._log(
                db, task_id=task_id, user_id=user_id, file_id=file_id,
                chunk_index=chunk_index, prompt=user_prompt, response=None,
                status="retry" if retryable and attempt < self.max_retries else "failed",
                error=str(err)[:500],
                meta={"duration_ms": duration_ms},
                model_name=model_tag,
                agent_label=agent_label,
            )
            if not retryable or attempt >= self.max_retries:
                break
            logger.warning(f"DeepSeek 调用失败({err}), 第 {attempt+1} 次重试")
            time.sleep(min(60, 2 ** (attempt + 1)))
            attempt += 1

        raise AiServiceError(
            f"DeepSeek 调用失败: {last_err}", code=50201,
        ) from last_err

    # ── 日志 ──

    @staticmethod
    def log_deferred(
        db: Session,
        *,
        task_id: Optional[int] = None,
        user_id: Optional[int] = None,
        file_id: Optional[int] = None,
        chunk_index: Optional[int] = None,
        meta: dict,
        status: str = "success",
        error: Optional[str] = None,
    ):
        """补写 AiCallLog — 并行线程在主线程事后补录

        meta 由 call_raw() 返回, 包含 create_time / model_tag / tokens / agent_label 全量信息。
        agent_label 从 meta 中读取并写入 AiCallLog.agent_label,实现 Agent 调用归因。
        """
        rec = AiCallLog(
            task_id=task_id,
            user_id=user_id,
            file_id=file_id,
            chunk_index=chunk_index,
            agent_label=meta.get("agent_label") or None,
            model_name=meta.get("model_tag", meta.get("model_name", "")),
            prompt=(meta.get("user_prompt") or "")[:200_000],
            response=(meta.get("response") or "")[:200_000],
            status=status,
            error_message=error,
            prompt_tokens=meta.get("prompt_tokens"),
            completion_tokens=meta.get("completion_tokens"),
            total_tokens=meta.get("total_tokens"),
            duration_ms=meta.get("duration_ms"),
            create_time=meta.get("create_time", datetime.now(timezone.utc)),
        )
        db.add(rec)
        db.flush()

    @staticmethod
    def _log(
        db: Session, *, task_id, user_id, file_id, chunk_index,
        prompt, response, status, error, meta, model_name,
        agent_label: Optional[str] = None,
    ):
        """chat() 专用的同步日志写入

        Args:
            agent_label: Agent 标识码,写入 AiCallLog.agent_label 实现 Agent 调用归因
        """
        rec = AiCallLog(
            task_id=task_id, user_id=user_id, file_id=file_id,
            chunk_index=chunk_index,
            agent_label=agent_label or None,
            model_name=model_name,
            prompt=prompt[:200_000] if prompt else None,
            response=response[:200_000] if response else None,
            status=status,
            error_message=error,
            prompt_tokens=meta.get("prompt_tokens"),
            completion_tokens=meta.get("completion_tokens"),
            total_tokens=meta.get("total_tokens"),
            duration_ms=meta.get("duration_ms"),
            create_time=datetime.now(timezone.utc),
        )
        db.add(rec)
        db.flush()
