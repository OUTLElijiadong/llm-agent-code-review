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
_MAX_COMPRESSION_CALLS = 96
_FACT_COMPACTION_PROMPT = (
    "你是运维事实压缩员。只归纳给定来源片段，不要推断未提供的数据。"
    "必须输出 JSON 对象，包含 source_id（与输入完全一致）、summary（不超过 350 字）、"
    "quote（从输入内容逐字复制的一段非空短引文）。保留异常、影响范围、数值和时间。"
)
_GROUP_COMPACTION_PROMPT = (
    "你是运维摘要压缩员。只合并给定的来源摘要，不要新增事实。"
    "必须输出 JSON 对象，包含 covered_source_ids（按输入顺序列出每个原始来源 ID）"
    "和 summary（不超过 600 字）；保留异常、影响范围、数值、时间及不确定性。"
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
                "你是 Prism 的全服管理 Agent。只分析真实工具结果，宿主机变更只能调用结构化运维工具；"
                "输出中文，按异常、影响、建议动作、验证方式四项说明。不得声称未执行的动作已完成。"
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
        calls = 0
        for index, part in enumerate(parts, start=1):
            digest = hashlib.sha256(part.encode("utf-8")).hexdigest()
            source_id = f"F{index:03d}-{digest[:12]}"
            expected.append(source_id)
            user_prompt = json.dumps({
                "source_id": source_id, "sha256": digest, "part": index,
                "total_parts": len(parts), "content": part,
            }, ensure_ascii=False)
            calls += 1
            result = self.call_json(
                user_prompt, ctx, api_config=config, max_tokens=2_048,
                system_prompt=_FACT_COMPACTION_PROMPT,
            )
            if not result.success:
                return result
            data = result.data
            if (not isinstance(data, dict) or data.get("source_id") != source_id
                    or not isinstance(data.get("summary"), str)
                    or not data["summary"].strip() or len(data["summary"]) > 350
                    or not isinstance(data.get("quote"), str)
                    or not data["quote"].strip() or data["quote"] not in part):
                return AgentResult(
                    success=False, error=f"运维事实来源 {source_id} 摘要缺失或引文无法核验",
                    failure_kind="invalid_summary",
                )
            records.append({
                "covered_source_ids": [source_id],
                "summary": data["summary"].strip(), "quote": data["quote"],
            })
        manifest = hashlib.sha256(original.encode("utf-8")).hexdigest()
        for _level in range(6):
            final = json.dumps({
                "source_manifest_sha256": manifest,
                "covered_source_ids": expected,
                "source_summaries": records,
            }, ensure_ascii=False)
            _projected, exceeds = self._project_input(final)
            if not exceeds:
                return AgentResult(success=True, data=final)
            next_records: list[dict[str, Any]] = []
            group: list[dict[str, Any]] = []
            size = 0
            groups: list[list[dict[str, Any]]] = []
            for record in records:
                record_size = len(json.dumps(record, ensure_ascii=False)) + 2
                if record_size > part_chars:
                    return AgentResult(
                        success=False, error="单条运维摘要超过压缩输入预算",
                        failure_kind="input_exceeds_context",
                    )
                if group and size + record_size > part_chars:
                    groups.append(group)
                    group, size = [], 0
                group.append(record)
                size += record_size
            if group:
                groups.append(group)
            if len(groups) >= len(records) or calls + len(groups) > _MAX_COMPRESSION_CALLS:
                return AgentResult(
                    success=False, error="运维事实无法在调用预算内完整压缩",
                    failure_kind="semantic_budget_exhausted",
                )
            for group in groups:
                source_ids = [sid for record in group for sid in record["covered_source_ids"]]
                calls += 1
                result = self.call_json(
                    json.dumps(group, ensure_ascii=False), ctx, api_config=config,
                    max_tokens=2_048, system_prompt=_GROUP_COMPACTION_PROMPT,
                )
                if not result.success:
                    return result
                data = result.data
                if (not isinstance(data, dict) or data.get("covered_source_ids") != source_ids
                        or not isinstance(data.get("summary"), str)
                        or not data["summary"].strip() or len(data["summary"]) > 600):
                    return AgentResult(success=False, error="运维事实层级压缩遗漏来源", failure_kind="invalid_summary")
                next_records.append({
                    "covered_source_ids": source_ids,
                    "summary": data["summary"].strip(),
                })
            if [sid for record in next_records for sid in record["covered_source_ids"]] != expected:
                return AgentResult(success=False, error="运维事实层级覆盖不完整", failure_kind="invalid_summary")
            records = next_records
        return AgentResult(
            success=False, error="运维事实摘要仍超过模型输入预算",
            failure_kind="semantic_budget_exhausted",
        )
