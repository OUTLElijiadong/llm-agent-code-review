"""
RBAC 权限点常量定义

集中定义全部 56 个权限编码字符串常量,便于在路由、服务、依赖注入中
以符号方式引用权限点,避免硬编码字符串带来的拼写错误与维护成本。

权限点按模块分组,与 alembic 迁移 007_rbac_tables.py 中预置的权限点一一对应:
- project 模块(6): 项目管理
- file 模块(5): 代码文件管理
- review 模块(5): 代码审查任务
- issue 模块(4): 审查问题处理
- rule 模块(4): 审查规则管理
- report 模块(5): 审查报告与导出
- agent 模块(14): Agent 中心与 Agent 工坊
- security 模块(2): 安全扫描
- user 模块(6): 用户与角色菜单管理
- audit 模块(2): 审计日志
- server_ops 模块(3): 服务器运维
"""
from __future__ import annotations


class PermissionCode:
    """权限点编码常量集合

    所有常量值为字符串,与 permission 表 code 字段一致。
    用法示例:
        @app.get("/projects", dependencies=[Depends(require_permission(PermissionCode.PROJECT_VIEW))])
    """

    # === project 模块(6): 项目管理 ===
    PROJECT_CREATE = "project:create"  # 创建新项目
    PROJECT_VIEW = "project:view"  # 查看项目列表与详情
    PROJECT_UPDATE = "project:update"  # 更新项目信息
    PROJECT_DELETE = "project:delete"  # 删除项目
    PROJECT_IMPORT = "project:import"  # 导入外部项目
    PROJECT_MEMBER_MANAGE = "project:member:manage"  # 管理项目成员关系

    # === file 模块(5): 代码文件管理 ===
    FILE_UPLOAD = "file:upload"  # 上传代码文件
    FILE_VIEW = "file:view"  # 查看代码文件内容
    FILE_EDIT = "file:edit"  # 编辑代码文件
    FILE_DELETE = "file:delete"  # 删除代码文件
    FILE_DOWNLOAD = "file:download"  # 下载代码文件

    # === review 模块(5): 代码审查任务 ===
    REVIEW_START = "review:start"  # 启动代码审查任务
    REVIEW_VIEW = "review:view"  # 查看审查任务与结果
    REVIEW_APPROVE = "review:approve"  # 审批审查任务结果
    REVIEW_CANCEL = "review:cancel"  # 取消审查任务
    REVIEW_RERUN = "review:rerun"  # 重新执行审查任务

    # === issue 模块(4): 审查问题处理 ===
    ISSUE_VIEW = "issue:view"  # 查看审查问题
    ISSUE_HANDLE = "issue:handle"  # 处理/修复审查问题
    ISSUE_BATCH = "issue:batch"  # 批量处理审查问题
    ISSUE_EXPORT = "issue:export"  # 导出审查问题

    # === rule 模块(4): 审查规则管理 ===
    RULE_VIEW = "rule:view"  # 查看审查规则
    RULE_CREATE = "rule:create"  # 创建审查规则
    RULE_UPDATE = "rule:update"  # 更新审查规则
    RULE_DELETE = "rule:delete"  # 删除审查规则

    # === report 模块(5): 审查报告与导出 ===
    REPORT_VIEW = "report:view"  # 查看审查报告
    REPORT_EXPORT_PDF = "report:export:pdf"  # 导出 PDF 格式报告
    REPORT_EXPORT_WORD = "report:export:word"  # 导出 Word 格式报告
    REPORT_EXPORT_JSON = "report:export:json"  # 导出 JSON 格式报告
    REPORT_EXPORT_HTML = "report:export:html"  # 导出 HTML 格式报告
    REPORT_TEMPLATE_MANAGE = "report:template_manage"  # 报告模板增删改

    # === agent 模块(14): Agent 中心与 Agent 工坊 ===
    AGENT_VIEW = "agent:view"  # 查看 Agent 信息
    AGENT_CHAT = "agent:chat"  # 与 Agent 进行对话
    AGENT_CONFIGURE = "agent:configure"  # 配置 Agent 参数
    AGENT_ASSET_CREATE = "agent_asset:create"
    AGENT_ASSET_UPDATE_OWN = "agent_asset:update_own"
    AGENT_ASSET_TEST = "agent_asset:test"
    AGENT_ASSET_SUBMIT = "agent_asset:submit"
    SKILL_ASSET_CREATE = "skill_asset:create"
    SKILL_ASSET_UPDATE_OWN = "skill_asset:update_own"
    CUSTOM_AGENT_INVOKE = "custom_agent:invoke"
    AGENT_ASSET_APPROVE = "agent_asset:approve"
    AGENT_ASSET_PUBLISH = "agent_asset:publish"
    AGENT_ASSET_DISABLE = "agent_asset:disable"
    AGENT_ASSET_ROLLBACK = "agent_asset:rollback"

    # === security 模块(2): 安全扫描 ===
    SECURITY_SCAN = "security:scan"  # 执行安全扫描
    SECURITY_VIEW = "security:view"  # 查看安全扫描结果

    # === user 模块(6): 用户与角色菜单管理 ===
    USER_VIEW = "user:view"  # 查看用户列表
    USER_CREATE = "user:create"  # 创建新用户
    USER_UPDATE = "user:update"  # 更新用户信息
    USER_DELETE = "user:delete"  # 删除用户
    ROLE_MANAGE = "role:manage"  # 管理角色与权限分配
    MENU_MANAGE = "menu:manage"  # 管理菜单配置

    # === audit 模块(2): 审计日志 ===
    AUDIT_VIEW = "audit:view"  # 查看操作审计日志
    AI_LOG_VIEW = "ai_log:view"  # 查看 AI 调用日志

    # === server_ops 模块(3): 服务器运维 ===
    SERVER_OPS_VIEW = "server_ops:view"  # 查看服务器状态和日志
    SERVER_OPS_EXECUTE = "server_ops:execute"  # 执行需要批准的运维变更
    SERVER_OPS_CRITICAL = "server_ops:critical"  # 执行完全权限高危变更

    # === pentest 模块(3): 授权渗透测试 ===
    PENTEST_VIEW = "pentest:view"  # 查看渗透测试委托与发现
    PENTEST_START = "pentest:start"  # 发起并授权渗透测试
    PENTEST_MANAGE = "pentest:manage"  # 管控全部用户的渗透测试委托


# 模块 → 权限编码列表的映射,便于按模块批量校验或前端渲染
PERMISSIONS_BY_MODULE: dict[str, list[str]] = {
    "project": [
        PermissionCode.PROJECT_CREATE,
        PermissionCode.PROJECT_VIEW,
        PermissionCode.PROJECT_UPDATE,
        PermissionCode.PROJECT_DELETE,
        PermissionCode.PROJECT_IMPORT,
        PermissionCode.PROJECT_MEMBER_MANAGE,
    ],
    "file": [
        PermissionCode.FILE_UPLOAD,
        PermissionCode.FILE_VIEW,
        PermissionCode.FILE_EDIT,
        PermissionCode.FILE_DELETE,
        PermissionCode.FILE_DOWNLOAD,
    ],
    "review": [
        PermissionCode.REVIEW_START,
        PermissionCode.REVIEW_VIEW,
        PermissionCode.REVIEW_APPROVE,
        PermissionCode.REVIEW_CANCEL,
        PermissionCode.REVIEW_RERUN,
    ],
    "issue": [
        PermissionCode.ISSUE_VIEW,
        PermissionCode.ISSUE_HANDLE,
        PermissionCode.ISSUE_BATCH,
        PermissionCode.ISSUE_EXPORT,
    ],
    "rule": [
        PermissionCode.RULE_VIEW,
        PermissionCode.RULE_CREATE,
        PermissionCode.RULE_UPDATE,
        PermissionCode.RULE_DELETE,
    ],
    "report": [
        PermissionCode.REPORT_VIEW,
        PermissionCode.REPORT_EXPORT_PDF,
        PermissionCode.REPORT_EXPORT_WORD,
        PermissionCode.REPORT_EXPORT_JSON,
        PermissionCode.REPORT_EXPORT_HTML,
    ],
    "agent": [
        PermissionCode.AGENT_VIEW,
        PermissionCode.AGENT_CHAT,
        PermissionCode.AGENT_CONFIGURE,
        PermissionCode.AGENT_ASSET_CREATE,
        PermissionCode.AGENT_ASSET_UPDATE_OWN,
        PermissionCode.AGENT_ASSET_TEST,
        PermissionCode.AGENT_ASSET_SUBMIT,
        PermissionCode.SKILL_ASSET_CREATE,
        PermissionCode.SKILL_ASSET_UPDATE_OWN,
        PermissionCode.CUSTOM_AGENT_INVOKE,
        PermissionCode.AGENT_ASSET_APPROVE,
        PermissionCode.AGENT_ASSET_PUBLISH,
        PermissionCode.AGENT_ASSET_DISABLE,
        PermissionCode.AGENT_ASSET_ROLLBACK,
    ],
    "security": [
        PermissionCode.SECURITY_SCAN,
        PermissionCode.SECURITY_VIEW,
    ],
    "user": [
        PermissionCode.USER_VIEW,
        PermissionCode.USER_CREATE,
        PermissionCode.USER_UPDATE,
        PermissionCode.USER_DELETE,
        PermissionCode.ROLE_MANAGE,
        PermissionCode.MENU_MANAGE,
    ],
    "audit": [
        PermissionCode.AUDIT_VIEW,
        PermissionCode.AI_LOG_VIEW,
    ],
    "server_ops": [
        PermissionCode.SERVER_OPS_VIEW,
        PermissionCode.SERVER_OPS_EXECUTE,
        PermissionCode.SERVER_OPS_CRITICAL,
    ],
    "pentest": [
        PermissionCode.PENTEST_VIEW,
        PermissionCode.PENTEST_START,
        PermissionCode.PENTEST_MANAGE,
    ],
}


# 全部权限编码集合(56 个),用于快速校验或 admin 全量授权
ALL_PERMISSION_CODES: list[str] = [
    code for codes in PERMISSIONS_BY_MODULE.values() for code in codes
]
