"""Seed persistent Chinese aliases for source, testing and sandbox workflows.

Revision ID: 025
Revises: 024
"""

from __future__ import annotations

import re
import unicodedata
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "025"
down_revision: Union[str, None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SEPARATORS = re.compile(r"[^0-9a-z\u3400-\u9fff]+")
SEEDED_ALIASES: tuple[tuple[str, str], ...] = (
    ("agent:security_sentinel", "安全审计"),
    ("agent:security_sentinel", "漏洞扫描"),
    ("agent:security_sentinel", "源码审计"),
    ("agent:security_sentinel", "白盒扫描"),
    ("agent:security_sentinel", "代码安全"),
    ("agent:test_verifier", "白盒测试"),
    ("agent:test_verifier", "黑盒测试"),
    ("agent:test_verifier", "动态测试"),
    ("agent:test_verifier", "运行测试"),
    ("agent:test_verifier", "沙箱测试"),
    ("agent:test_verifier", "验证代码"),
    ("agent:sandbox_deployer", "部署沙箱"),
    ("agent:sandbox_deployer", "预览环境"),
    ("agent:sandbox_deployer", "临时部署"),
    ("agent:sandbox_deployer", "运行项目"),
    ("agent:sandbox_deployer", "测试环境"),
    ("sandbox:create_test", "跑测试"),
    ("sandbox:create_test", "测试源码"),
    ("sandbox:create_test", "执行测试"),
    ("sandbox:create_test", "动态验证"),
    ("sandbox:create_deploy", "部署项目"),
    ("sandbox:create_deploy", "启动项目"),
    ("sandbox:create_deploy", "创建预览"),
    ("sandbox:create_deploy", "沙箱运行"),
    ("sandbox:close", "关闭沙箱"),
    ("sandbox:close", "停止环境"),
    ("sandbox:close", "销毁预览"),
    ("sandbox:close", "结束测试环境"),
    ("sandbox:extend", "延长沙箱"),
    ("sandbox:extend", "沙箱续期"),
    ("sandbox:extend", "保留环境"),
    ("sandbox:extend", "延长部署时间"),
    ("mcp:prism-code:download_project_source", "源码下载"),
    ("mcp:prism-code:download_project_source", "下载源码"),
    ("mcp:prism-code:download_project_source", "下载代码"),
    ("mcp:prism-code:download_project_source", "导出源码"),
    ("mcp:prism-code:download_project_source", "远程下载代码"),
    ("mcp:prism-code:download_project_source", "获取完整源码"),
    ("mcp:prism-code:download_project_source", "打包源码下载"),
    ("mcp:prism-code:download_project_source", "拉取项目代码"),
    ("mcp:prism-code:download_project_source", "下载项目压缩包"),
    ("agent:test_verifier", "帮我跑白盒测试"),
    ("agent:test_verifier", "做源码级白盒验证"),
    ("agent:test_verifier", "帮我跑黑盒测试"),
    ("agent:test_verifier", "执行接口黑盒验证"),
    ("sandbox:create_test", "创建白盒测试沙箱"),
    ("sandbox:create_test", "创建黑盒测试沙箱"),
    ("sandbox:create_test", "在隔离环境运行测试"),
    ("sandbox:create_test", "访问目标服务器测试"),
    ("sandbox:create_deploy", "部署代码到沙箱"),
    ("sandbox:create_deploy", "创建临时部署环境"),
    ("sandbox:create_deploy", "启动项目预览环境"),
    ("sandbox:create_deploy", "在本机沙箱部署"),
    ("sandbox:close", "关闭测试沙箱"),
    ("sandbox:close", "停止临时部署环境"),
    ("sandbox:close", "释放运行环境"),
    ("sandbox:extend", "延长沙箱保留时间"),
    ("sandbox:extend", "测试环境续期"),
    ("sandbox:extend", "继续保留部署环境"),
    ("sandbox:extend", "延长临时环境"),
)


def _normalize(value: str) -> str:
    return _SEPARATORS.sub("", unicodedata.normalize("NFKC", value).casefold())


def _table() -> sa.TableClause:
    return sa.table(
        "agent_capability_alias",
        sa.column("capability_code", sa.String(255)),
        sa.column("alias", sa.String(160)),
        sa.column("normalized_alias", sa.String(160)),
        sa.column("locale", sa.String(20)),
        sa.column("weight", sa.Float()),
        sa.column("enabled", sa.SmallInteger()),
    )


def upgrade() -> None:
    bind = op.get_bind()
    aliases = _table()
    for capability_code, alias in SEEDED_ALIASES:
        normalized_alias = _normalize(alias)
        exists = bind.execute(
            sa.select(sa.literal(1)).select_from(aliases).where(
                aliases.c.capability_code == capability_code,
                aliases.c.locale == "zh-CN",
                aliases.c.normalized_alias == normalized_alias,
            ).limit(1)
        ).first()
        if exists is None:
            bind.execute(
                aliases.insert().values(
                    capability_code=capability_code,
                    alias=alias,
                    normalized_alias=normalized_alias,
                    locale="zh-CN",
                    weight=1.0,
                    enabled=1,
                )
            )


def downgrade() -> None:
    """数据保护型降级：别名可能已被管理员编辑，不自动删除业务数据。"""
