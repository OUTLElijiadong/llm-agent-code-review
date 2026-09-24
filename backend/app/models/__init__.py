"""显式、惰性的 ORM 注册入口；导入包本身不启动应用或加载数据库配置。"""

from importlib import import_module

_MODEL_MODULES = (
    "admin_chat",
    "agent_capability",
    "agent_governance",
    "agent_mesh",
    "agent_multimodal",
    "agent_response_run",
    "agent_skill_record",
    "agent_team",
    "ai_call_log",
    "api_config",
    "audit_log",
    "beta_invite_code",
    "code_file",
    "code_version",
    "custom_agent",
    "eval_case",
    "evolution_proposal",
    "forum_post",
    "forum_reply",
    "knowledge_chunk",
    "knowledge_doc",
    "maintenance_ticket",
    "malware_scan_log",
    "pentest",
    "project",
    "project_import_task",
    "project_member",
    "project_source_archive",
    "project_source_revision",
    "rbac",
    "report_template",
    "review_experience",
    "review_issue",
    "review_report",
    "review_rule",
    "review_task",
    "review_task_file",
    "roundtable",
    "system_config",
    "user",
    "user_avatar",
    "user_feedback",
    "user_profile",
)


def load_all_models() -> None:
    """仅注册映射，可重复调用；不连接数据库、建表或导入服务及 Agent。"""
    for module_name in _MODEL_MODULES:
        import_module(f"{__name__}.{module_name}")
