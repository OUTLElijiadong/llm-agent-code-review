"""
DeepSeek Agent: HTTP调用、重试、日志记录

统一入口:
  chat()      — 主线程使用, 自带 AiCallLog 写入 (需要 db Session)
  call_raw()  — 并行线程使用, 不带 db, 返回 meta 供主线程事后 log_deferred() 补录
  两种方法共用同一套 HTTP/重试/超时/模型参数。

时区约定: 所有 create_time 均为 timezone-aware UTC (datetime.now(timezone.utc))。
"""
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import httpx
from loguru import logger
from sqlalchemy.orm import Session

from app.ai.exceptions import AiServiceError
from app.core.config import settings
from app.models.ai_call_log import AiCallLog

if TYPE_CHECKING:
    from app.utils.api_resolver import ApiConfig


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
        if api_config:
            self.base_url = api_config.base_url.rstrip("/")
            self.api_key = api_config.api_key
            self.model = api_config.model
        else:
            self.base_url = (base_url or settings.deepseek_base_url).rstrip("/")
            self.api_key = api_key or settings.deepseek_api_key
            self.model = model or settings.deepseek_model

        self.api_config = api_config  # 保留引用, 供协程/线程下游使用
        self.timeout = timeout or settings.deepseek_timeout
        self.max_retries = max_retries if max_retries is not None else settings.deepseek_max_retries

    # ── 公共 HTTP 请求构造 (chat / call_raw 共用) ──

    def _build_request(
        self, system_prompt: str, user_prompt: str, json_mode: bool = True,
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
            "temperature": 0.2,
            "max_tokens": 4096,
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
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, headers=headers, json=payload)
        return resp, int((time.time() - t0) * 1000)

    # ── 并行线程调用 (不带 db, 返回 meta 供事后补录) ──

    def call_raw(
        self,
        system_prompt: str,
        user_prompt: str,
        agent_label: str = "",
        json_mode: bool = True,
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

        for attempt in range(self.max_retries + 1):
            resp, duration_ms = self._do_request(url, headers, payload)

            if resp.status_code == 200:
                body = resp.json()
                usage = body.get("usage", {})
                content = body["choices"][0]["message"]["content"]
                return content, {
                    "model_tag": model_tag,
                    "model_name": self.model,
                    "agent_label": agent_label,
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                    "duration_ms": duration_ms,
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
    ) -> tuple:
        """调用 DeepSeek Chat API + 写 AiCallLog"""
        model_tag = (
            f"{self.model}/{agent_label}-agent"
            if agent_label and agent_label != "general"
            else self.model
        )
        url, headers, payload = self._build_request(system_prompt, user_prompt)

        attempt = 0
        last_err: Optional[Exception] = None
        while attempt <= self.max_retries:
            resp, duration_ms = self._do_request(url, headers, payload)

            if resp.status_code == 200:
                body = resp.json()
                content = body["choices"][0]["message"]["content"]
                usage = body.get("usage", {})
                meta = {
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                    "duration_ms": duration_ms,
                }
                self._log(
                    db, task_id=task_id, user_id=user_id, file_id=file_id,
                    chunk_index=chunk_index, prompt=user_prompt, response=content,
                    status="success", error=None, meta=meta,
                    model_name=model_tag,
                )
                return content, meta

            if resp.status_code == 429:
                err = AiServiceError("DeepSeek 限流", code=42900)
            elif resp.status_code >= 500:
                err = AiServiceError(
                    f"DeepSeek 服务异常 {resp.status_code}", code=50201,
                )
            else:
                err = AiServiceError(
                    f"DeepSeek 返回 {resp.status_code}: {resp.text[:200]}",
                    code=50201,
                )
            last_err = err
            self._log(
                db, task_id=task_id, user_id=user_id, file_id=file_id,
                chunk_index=chunk_index, prompt=user_prompt, response=None,
                status="retry" if attempt < self.max_retries else "failed",
                error=str(err)[:500],
                meta={"duration_ms": duration_ms},
                model_name=model_tag,
            )
            if attempt >= self.max_retries:
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

        meta 由 call_raw() 返回, 包含 create_time / model_tag / tokens 全量信息。
        """
        rec = AiCallLog(
            task_id=task_id,
            user_id=user_id,
            file_id=file_id,
            chunk_index=chunk_index,
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
    ):
        """chat() 专用的同步日志写入"""
        rec = AiCallLog(
            task_id=task_id, user_id=user_id, file_id=file_id,
            chunk_index=chunk_index,
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
