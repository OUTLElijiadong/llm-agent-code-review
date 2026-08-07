"""沙箱完整部署核验 Agent。

在测试执行前调用:基于源码摘要与目标语言,判断应用能否"完整部署并稳定运行";
如发现入口缺失/依赖不完整,生成受控补全启动脚本 `_prism_launch.sh`(由镜像内置
runner 调用,不执行任意命令),并给出依赖补全说明。所有生成内容仅在隔离沙箱内运行。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.agents.base import AgentContext, BaseAgent

MAX_LAUNCH_BYTES = 20_000


class DeploymentCoordinatorAgent(BaseAgent):
    """判断并补全沙箱完整部署:生成启动补丁与依赖说明。"""

    name = "deployment_coordinator"
    description = "沙箱完整部署核验:入口检测、依赖补全、稳定运行补丁"
    icon = "deployment_coordinator"
    color = "#B26A00"
    category = "deploy"

    def __init__(self) -> None:
        super().__init__(
            system_prompt=(
                "你是沙箱部署协调 Agent。给定源码摘要与语言,判断应用能否在离线沙箱完整部署并稳定运行;"
                "如入口缺失或依赖不全,生成一个受控启动脚本补丁。只输出 JSON。"
            ),
            temperature=0.1,
            max_tokens=8_000,
        )

    def plan(
        self,
        *,
        language: str,
        test_mode: str,
        source_summary: dict[str, Any],
        db_type: str = "none",
        ctx: Optional[AgentContext] = None,
    ) -> dict[str, Any]:
        db_instruction = ""
        if db_type == "mysql":
            db_instruction = (
                "\n数据库: 沙箱已连接独立测试库 MySQL(只读环境变量连接,禁止硬编码凭据):\n"
                "  PRISM_DB_HOST / PRISM_DB_PORT / PRISM_DB_USER / PRISM_DB_PASSWORD / PRISM_DB_NAME\n"
                "  php: \\$pdo=new PDO('mysql:host='.getenv('PRISM_DB_HOST').';port='.getenv('PRISM_DB_PORT').\n"
                "    ';dbname='.getenv('PRISM_DB_NAME'), getenv('PRISM_DB_USER'), getenv('PRISM_DB_PASSWORD'));\n"
                "  若应用需要数据库配置,请在启动脚本中把 DB 主机/用户/密码指向上述环境变量,使应用能在沙箱内连库运行。\n"
            )
        elif db_type == "sqlite":
            db_instruction = "\n数据库: 沙箱内置 sqlite(路径 /workspace/.prism-db/app.db,可自行创建)。\n"
        user_message = (
            f"语言: {language}\n测试模式: {test_mode}\n数据库: {db_type}\n源码摘要(JSON):\n"
            f"{json.dumps(source_summary, ensure_ascii=False, default=str)[:12000]}\n\n"
            "要求:\n"
            "1. 判断是否有可启动入口(main/app/index/server 等);若没有,生成一个最小可启动补全脚本。\n"
            "2. 脚本为 POSIX sh,监听 127.0.0.1 的 ${PRISM_PREVIEW_PORT}(默认8080),禁止外联、禁止读环境密钥。\n"
            "3. 若依赖可能缺失,在 notes 中说明(离线沙箱只能用镜像内置或 vendor 依赖)。\n"
            "4. 输出 JSON: {\'launch_script\':\'...\',\'notes\':\'...\'};\n"
            "   入口已存在且完整时 launch_script 为空字符串。\n"
            + db_instruction
        )
        try:
            agent_result = self.call_json(user_message, ctx=ctx)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:300]}
        if not getattr(agent_result, "success", False):
            return {"error": str(getattr(agent_result, "error", "生成失败"))[:300]}
        data = getattr(agent_result, "data", None)
        if not isinstance(data, dict):
            return {"error": "部署计划结果格式错误"}
        launch_script = str(data.get("launch_script") or "").strip()
        notes = str(data.get("notes") or "").strip()
        if launch_script and len(launch_script.encode("utf-8", errors="ignore")) > MAX_LAUNCH_BYTES:
            launch_script = ""
        # 只允许普通文本 shell 脚本,禁止明显的外联/危险指令
        if re.search(r"(curl|wget|nc\s|/dev/tcp|ssh\s|scp\s)", launch_script, re.I):
            launch_script = ""
        return {"launch_script": launch_script, "notes": notes[:2000]}
