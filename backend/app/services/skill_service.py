"""Skill 调用统一入口服务 — AgentSkill 自进化与总调度升级

提供 invoke_skill_with_record() 统一入口:
1. 从 SkillRegistry 获取指定 Agent 的 Skill
2. 注入 _db 到 params(Skill 钩子需要 db)
3. 调用 skill.run(params, ctx)
4. 写 agent_skill_record 表(记录触发类型/输入/输出/效果/耗时)
5. manual 模式额外写 audit_log
6. 异常捕获:Skill 抛异常时写 effect=failed 记录,不向上传播

供 API 层(手动触发)、scheduler_service(定时触发)、event_bus(事件触发)统一调用。
"""
import json
import time
from typing import TYPE_CHECKING, Any, Dict, Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.skills.registry import SkillRegistry
from app.models.agent_skill_record import AgentSkillRecord

if TYPE_CHECKING:
    from app.agents.base import AgentContext
    from app.models.user import User


def _truncate(text: str, max_len: int = 500) -> str:
    """截断文本到指定长度

    Args:
        text: 原始文本
        max_len: 最大长度(默认 500)

    Returns:
        str: 截断后的文本(超长追加 "...")
    """
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def _build_output_summary(result_data: Any) -> str:
    """从 Skill 调用结果构建输出摘要

    Args:
        result_data: SkillResult.data(任意类型)

    Returns:
        str: 输出摘要(限 500 字)
    """
    try:
        text = json.dumps(result_data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(result_data)
    return _truncate(text, 500)


def invoke_skill_with_record(
    db: Session,
    agent_name: str,
    skill_name: str,
    params: Dict[str, Any],
    trigger_type: str = "manual",
    trigger_source: str = "",
    user: Optional["User"] = None,
    ctx: Optional["AgentContext"] = None,
) -> Dict[str, Any]:
    """统一 Skill 调用入口,写 agent_skill_record

    Args:
        db: 数据库会话
        agent_name: Agent name(如 code_reviewer)
        skill_name: Skill name(如 code_reviewer.self_improve)
        params: Skill 参数(会额外注入 _db 供 Skill 钩子使用)
        trigger_type: 触发类型(manual/scheduled/event/proactive)
        trigger_source: 触发来源描述(如 scheduler_cron / event:REVIEW_ISSUE_STATUS_CHANGED)
        user: 触发用户(manual 模式填写,用于 audit_log)
        ctx: Agent 上下文(含 trace_id 等)

    Returns:
        dict: {
            "success": bool,
            "data": Any,         # SkillResult.data
            "error": str|None,   # 失败原因
            "effect": str,       # 效果标签
            "duration_ms": int,  # 执行耗时
            "record_id": int,    # agent_skill_record 记录 ID
        }
    """
    t0 = time.time()
    skill = SkillRegistry.instance().get(agent_name, skill_name)

    # Skill 不存在
    if skill is None:
        duration_ms = int((time.time() - t0) * 1000)
        record = AgentSkillRecord(
            agent_name=agent_name,
            skill_name=skill_name,
            trigger_type=trigger_type,
            trigger_source=trigger_source,
            input_params=_truncate(json.dumps(params, ensure_ascii=False, default=str)),
            output_summary=f"Skill {skill_name} 不存在",
            effect="failed",
            duration_ms=duration_ms,
            created_by_user_id=user.id if user else None,
        )
        db.add(record)
        db.commit()
        logger.warning(f"[skill_service] Skill {skill_name} 不存在(agent={agent_name})")
        return {
            "success": False,
            "data": None,
            "error": f"Skill {skill_name} 不存在",
            "effect": "failed",
            "duration_ms": duration_ms,
            "record_id": record.id,
        }

    # 注入 _db 到 params(Skill 钩子需要 db 读写数据)
    invoke_params = dict(params)
    invoke_params["_db"] = db

    success = False
    result_data: Any = None
    error_msg: Optional[str] = None
    effect = "failed"

    try:
        result = skill.run(invoke_params, ctx)
        success = result.success
        result_data = result.data
        error_msg = result.error
        effect = result.effect
    except Exception as e:
        logger.exception(f"[skill_service] Skill {skill_name} 调用异常")
        error_msg = f"Skill 调用异常: {e}"
        effect = "failed"

    duration_ms = int((time.time() - t0) * 1000)

    # 写 agent_skill_record
    try:
        record = AgentSkillRecord(
            agent_name=agent_name,
            skill_name=skill_name,
            trigger_type=trigger_type,
            trigger_source=trigger_source,
            input_params=_truncate(json.dumps(params, ensure_ascii=False, default=str)),
            output_summary=_build_output_summary(result_data) if success else _truncate(error_msg or "", 500),
            effect=effect,
            duration_ms=duration_ms,
            created_by_user_id=user.id if user else None,
        )
        db.add(record)
        db.commit()
        record_id = record.id
    except Exception as e:
        logger.exception(f"[skill_service] 写 agent_skill_record 失败")
        db.rollback()
        record_id = None

    # manual 模式写 audit_log
    if trigger_type == "manual":
        try:
            from app.services import audit_service

            audit_service.log(
                db,
                user,
                action="skill_invoke",
                target_type="agent_skill",
                target_id=str(record_id) if record_id else None,
                detail=(
                    f"手动触发 Skill: agent={agent_name} skill={skill_name} "
                    f"effect={effect} duration={duration_ms}ms"
                ),
                status="success" if success else "failed",
                commit=True,
            )
        except Exception as e:
            logger.warning(f"[skill_service] 写 audit_log 失败(不影响主流程): {e}")

    logger.info(
        f"[skill_service] {agent_name}.{skill_name} trigger={trigger_type} "
        f"effect={effect} duration={duration_ms}ms"
    )

    return {
        "success": success,
        "data": result_data,
        "error": error_msg,
        "effect": effect,
        "duration_ms": duration_ms,
        "record_id": record_id,
    }
