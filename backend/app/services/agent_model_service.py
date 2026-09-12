"""统一解析 Agent 模型，避免 SSE 元数据与实际请求各自维护分配逻辑。"""

from app.core.config import settings
from app.services.system_config_service import resolve_model_assignment


def resolve_agent_model(db, *, surface, config):
    """用户自有配置优先；平台配置使用管理员角色分配，未分配保留原分层。"""
    default = config.model if config.source in {"user", "global"} else settings.deepseek_orchestrator_model
    if config.source == "user":
        return default
    return resolve_model_assignment(db, "orchestrator" if surface == "admin" else "chat", default)


def resolve_subagent_config(db, config, *, agent_name=""):
    """返回新配置对象，避免子 Agent 的模型覆盖串到主对话或其他请求。"""
    from dataclasses import replace

    if config.source == "user":
        return config
    model = resolve_model_assignment(db, "subagent", config.model)
    if agent_name:
        model = resolve_model_assignment(db, f"agent:{agent_name}", model)
    return replace(config, model=model)


def configure_subagent(db, agent, user_id=None):
    """给独立、请求级 BaseAgent 注入同一配置，不改变共享对象或角色设定。"""
    from app.utils.api_resolver import resolve_api_config

    config = resolve_subagent_config(db, resolve_api_config(db, user_id), agent_name=agent.name)
    agent._base_url = config.base_url
    agent._api_key = config.api_key
    agent._model = config.model
    agent._assigned_model = config.model
    for field, attribute in (
        ("timeout_seconds", "_timeout"), ("max_retries", "_max_retries"), ("temperature", "_temperature"),
    ):
        value = getattr(config, field)
        if value is not None:
            setattr(agent, attribute, value)
    return agent
