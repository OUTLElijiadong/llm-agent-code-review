"""独立全服管理 Agent。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.models.user import User
from app.services import ops_service
from app.services.agent_model_service import resolve_subagent_config
from app.utils.api_resolver import resolve_api_config

_MAX_FACT_PARTS = 64
_FACT_COMPACTION_PROMPT = (
    "你是运维事实压缩员。只记录给定来源片段中可直接观察到的内容，不要推断、补全或写建议。"
    "必须输出 JSON 对象，包含 source_id（与输入完全一致）、summary（不超过 180 字）、"
    "quote（从输入内容逐字复制的一段非空引文，最多 160 字）。保留异常、影响范围、数值和时间；"
    "证据不足时明确写未知，不要把相关性写成因果。"
)


class OperationsAgent(BaseAgent):
    name = "operations"
    description = "运维特工:服务器巡检、防火墙/服务/软件变更(全部需管理员批准)、事后回滚"
    icon = "operations"
    color = "#2A9D8F"
    category = "operations"
    skills = (
        "主机巡检",
        "容器处置",
        "Nginx 与证书",
        "MySQL 与 Redis",
        "备份恢复",
        "配置维护",
        "模型接口监测",
        "任务队列监测",
        "Agent 故障诊断",
        "自进化运行态",
        "systemd 服务管理",
        "宿主机文件与软件包",
        "防火墙、账户与 SSH 公钥",
    )

    def __init__(self):
        super().__init__(
            system_prompt=(
                "你是 Prism 唯一主 Agent 小菱调用的管理员运维子 Agent。只分析本次管理员会话提供的真实结构化工具结果；"
                "上下文压缩摘要必须连同可核验的原始引文和来源 ID 使用，摘要与引文冲突时以引文为准，证据不足就说明未知。"
                "不得把建议写成已执行动作，不得声称工具没有返回的状态、变更或验证已经完成。"
                "宿主机变更只能由小菱调用结构化运维工具并通过既有审批流程；子 Agent 团队中的运维任务只读。"
                "输出中文，按异常、影响、证据来源、建议动作、验证方式说明。"
            ),
            temperature=0.1,
            max_tokens=1200,
        )

    def execute_action(
        self,
        db: Session,
        actor: Optional[User],
        *,
        action: str,
        params: Optional[dict[str, Any]] = None,
        request_id: str = "",
        session_db_id: Optional[int] = None,
        source: str = "admin_copilot",
    ) -> AgentResult:
        try:
            data = ops_service.execute(
                db,
                actor,
                action=action,
                params=params,
                request_id=request_id,
                session_db_id=session_db_id,
                source=source,
            )
            return AgentResult(success=data.get("status") == "success", data=data, error=data.get("error"))
        except Exception as exc:  # noqa: BLE001 - 转为 AgentResult 交给聊天层
            return AgentResult(success=False, error=str(exc))

    def diagnose(self, db: Session, actor: Optional[User], facts: dict[str, Any], trace_id: str) -> AgentResult:
        ctx = AgentContext(
            user_id=actor.id if actor else None,
            extra={"trace_id": trace_id, "source": "ops_diagnose"},
        )
        prompt = json.dumps(facts, ensure_ascii=False, default=str)
        self.bind_usage_source(db, actor)
        config = resolve_subagent_config(db, resolve_api_config(db, None), agent_name=self.name)
        _projected, exceeds = self._project_input(prompt)
        if exceeds:
            compacted = self._compact_facts(facts, ctx, config)
            if not compacted.success:
                return compacted
            prompt = compacted.data
        result = self.call(prompt, ctx, api_config=config)
        self._log_call(
            db,
            user_id=actor.id if actor else None,
            result=result,
            status="success" if result.success else "failed",
            error=result.error,
            user_prompt=prompt,
            response_text=str(result.data or "") if result.success else "",
        )
        return result

    def _fact_part_chars(self) -> int:
        """给系统提示词、压缩说明和输出预留窗口；不从事实中抽样。"""
        from app.core.config import settings

        window = int(settings.deepseek_context_window_tokens or 0)
        available = (window - max(8_192, self._max_tokens)) * 2 - 2_048
        return min(12_000, max(0, available - len(_FACT_COMPACTION_PROMPT) - 1_024))

    def _compact_facts(self, facts: dict[str, Any], ctx: AgentContext, config: Any) -> AgentResult:
        part_chars = self._fact_part_chars()
        if part_chars < 256:
            return AgentResult(
                success=False, error="运维事实没有足够的模型输入预算",
                failure_kind="input_exceeds_context",
            )
        original = json.dumps(facts, ensure_ascii=False, default=str)
        parts: list[str] = []
        offset = 0
        while offset < len(original):
            take = min(part_chars, len(original) - offset)
            # 日志中的反斜杠或控制字符再次 JSON 编码会膨胀；以实际请求体
            # 的窗口估算切分，而不是只按原字符串长度估算。
            while take:
                probe = json.dumps({
                    "source_id": "F000-000000000000", "sha256": "0" * 64,
                    "part": 999, "total_parts": 999,
                    "content": original[offset:offset + take],
                }, ensure_ascii=False)
                if not self._project_input(
                    probe, system_prompt=_FACT_COMPACTION_PROMPT, output_tokens=2_048,
                )[1]:
                    break
                take //= 2
            if not take:
                return AgentResult(
                    success=False, error="运维事实单字符也超过压缩输入预算",
                    failure_kind="input_exceeds_context",
                )
            parts.append(original[offset:offset + take])
            offset += take
        if len(parts) > _MAX_FACT_PARTS:
            return AgentResult(
                success=False, error="运维事实超过分片压缩调用预算",
                failure_kind="semantic_budget_exhausted",
            )
        records: list[dict[str, Any]] = []
        expected: list[str] = []
        for index, part in enumerate(parts, start=1):
            digest = hashlib.sha256(part.encode("utf-8")).hexdigest()
            source_id = f"F{index:03d}-{digest[:12]}"
            expected.append(source_id)
            user_prompt = json.dumps({
                "source_id": source_id, "sha256": digest, "part": index,
                "total_parts": len(parts), "content": part,
            }, ensure_ascii=False)
            result = self.call_json(
                user_prompt, ctx, api_config=config, max_tokens=2_048,
                system_prompt=_FACT_COMPACTION_PROMPT,
            )
            if not result.success:
                return result
            data = result.data
            if (not isinstance(data, dict) or data.get("source_id") != source_id
                    or not isinstance(data.get("summary"), str)
                    or not data["summary"].strip() or len(data["summary"]) > 180
                    or not isinstance(data.get("quote"), str)
                    or not data["quote"].strip() or len(data["quote"]) > 160
                    or data["quote"] not in part):
                return AgentResult(
                    success=False, error=f"运维事实来源 {source_id} 摘要缺失或引文无法核验",
                    failure_kind="invalid_summary",
                )
            records.append({
                "covered_source_ids": [source_id],
                "summary": data["summary"].strip(), "quote": data["quote"],
            })
        manifest = hashlib.sha256(original.encode("utf-8")).hexdigest()
        final = json.dumps({
            "source_manifest_sha256": manifest,
            "covered_source_ids": expected,
            "source_summaries": records,
        }, ensure_ascii=False)
        _projected, exceeds = self._project_input(final)
        if exceeds:
            return AgentResult(
                success=False,
                error="运维事实单轮压缩后仍超出模型输入预算；为避免二次改写造成失真，已拒绝继续诊断",
                failure_kind="semantic_budget_exhausted",
            )
        return AgentResult(success=True, data=final)
