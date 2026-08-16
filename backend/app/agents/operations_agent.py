"""独立全服管理 Agent。"""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.models.user import User
from app.services import ops_service
from app.utils.api_resolver import resolve_api_config


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
        result = self.call(prompt, ctx, api_config=resolve_api_config(db, None))
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
