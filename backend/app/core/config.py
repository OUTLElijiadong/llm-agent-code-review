"""
FastAPI应用核心配置模块
使用pydantic-settings从环境变量/.env文件加载所有配置项
"""

from pathlib import Path
from typing import List

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parent.parent.parent.parent / ".env"

# 不安全的占位默认值 —— 生产环境若仍为这些值,直接拒绝启动
_INSECURE_JWT_SECRETS = {"", "code-review-platform-dev-secret-key"}
_INSECURE_API_KEYS = {"", "sk-xxxxx"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "dev"
    log_level: str = "INFO"
    openapi_enabled: bool = True
    app_release: str = "dev"
    app_version: str = "3.6.0"
    # 注册验证码开关:生产默认开(dev/test 可关以便自动化测试)
    register_captcha_enabled: bool = True
    # 内测注册: 开启后注册必须携带管理员生成的一次性内测码。
    beta_registration_enabled: bool = False
    beta_code_pepper: str = ""

    # ── 恶意文件扫描 ──
    clamav_host: str = "clamav"
    clamav_port: int = 3310
    clamav_timeout: float = 5.0
    clamav_recheck_seconds: int = 30
    yara_rules_dir: str = "deploy/yara/rules"
    yara_match_timeout: int = Field(default=5, ge=1, le=60)
    malware_scan_fail_closed: bool = False
    source_archive_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1, le=20 * 1024 * 1024)
    source_archive_clamav_timeout: float = Field(default=120.0, gt=0, le=300)
    source_archive_yara_total_timeout: float = Field(default=300.0, gt=0, le=600)

    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "code_review"

    jwt_secret: str = "code-review-platform-dev-secret-key"
    jwt_algorithm: str = "HS256"
    jwt_expire_seconds: int = 7 * 24 * 3600
    # 第一项用于新写入，后续项仅用于解密旧密文；生产必须配置独立于 JWT 的随机密钥。
    api_key_encryption_keys: List[str] = []

    deepseek_api_key: str = "sk-xxxxx"
    deepseek_base_url: str = "https://api.deepseek.com"
    # 子 Agent 统一使用的默认模型;总调度者(Orchestrator)与小菱对话单独使用 pro。
    deepseek_model: str = "deepseek-v4-flash"
    # 总调度者与小菱(user/admin 两个 surface)使用的高能力模型。
    deepseek_orchestrator_model: str = "deepseek-v4-pro"
    # pro 模型返回“模型不可用”时,允许在同一轮自动回退到 flash;关闭则直接失败。
    deepseek_orchestrator_fallback_to_flash: bool = True
    deepseek_timeout: int = 60
    deepseek_max_retries: int = 2
    deepseek_chunk_threshold: int = 32000
    # 完整沙箱需要跨越排队、部署、测试和报告阶段；工具轮数过低会在沙箱
    # 已成功时提前把 Responses 会话标记为 max_rounds_exceeded。
    agent_responses_max_rounds: int = Field(default=128, ge=20, le=128)
    # 上传后的固定全量验证在一个工具调用内等待唯一沙箱终态，避免模型提前
    # 结束或反复轮询。上限覆盖最长语言 profile 与报告后处理。
    agent_full_validation_wait_seconds: int = Field(default=600, ge=60, le=900)
    # DeepSeek V4 上下文窗口与内部 Agent 投影预算。完整 transcript 仍持久化供审计。
    deepseek_context_window_tokens: int = 1_000_000
    deepseek_max_output_tokens: int = 65_536
    # 项目级白盒审计需要同时承载漏洞、入口和危险汇，不能沿用通用 Agent 的 4096 token 默认值。
    security_semantic_max_output_tokens: int = Field(default=65_536, ge=4_096, le=65_536)
    deepseek_compaction_threshold_tokens: int = 850_000
    deepseek_compaction_keep_recent_tokens: int = 200_000
    # 项目级语义审计先使用保守批次，输出截断时再递归拆分。
    security_semantic_batch_chars: int = Field(default=60_000, ge=8_000, le=120_000)
    security_semantic_min_split_chars: int = Field(default=2_000, ge=512, le=24_000)
    security_semantic_max_split_depth: int = Field(default=12, ge=1, le=12)
    security_semantic_max_requests: int = Field(default=256, ge=1, le=512)
    security_semantic_timeout_seconds: int = Field(default=3_600, ge=60, le=7_200)
    security_semantic_max_findings_per_batch: int = Field(default=12, ge=1, le=50)
    security_semantic_max_graph_items_per_batch: int = Field(default=20, ge=1, le=100)
    # static_full / triage 只调度可在共享请求预算内闭合的多文件语义窗口。
    security_semantic_bounded_total_chars: int = Field(default=32_000, ge=512, le=2_400_000)
    security_semantic_bounded_per_file_chars: int = Field(default=8_000, ge=512, le=240_000)
    security_semantic_bounded_max_files: int = Field(default=12, ge=1, le=32)
    # 全局审查并发上限:同时进行的后台审查任务数上限(2C2G 生产机默认 2)
    review_max_concurrency: int = 2
    allow_private_ai_base_url: bool = False
    enforce_ai_base_url_dns_check: bool = False

    # ── RAG 嵌入(embedding)配置 ──
    # 留空则降级为本地确定性哈希向量,保证无 Key 也能跑通(语义弱)。
    # 运行期可被 system_config 表(管理员在前端配置)覆盖。
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dim: int = 256  # 本地降级向量维度
    embedding_timeout: int = 30

    agent_governance_scheduler_enabled: bool = True
    agent_mesh_dispatcher_enabled: bool = True
    agent_mesh_dispatch_interval_seconds: int = 2
    agent_mesh_dispatch_lease_seconds: int = 300
    # Agent Mesh 监管者每轮调度允许的最大轮数;1-5 之间,防止监管链无限循环。
    agent_mesh_supervision_max_rounds: int = Field(default=3, ge=1, le=5)
    # 空 Mesh 会话归档阈值:活跃但无消息、无 Responses 运行且超过此时长的会话将被归档。
    agent_mesh_empty_session_archive_hours: int = Field(default=24, ge=1, le=720)
    # 管理小菱 JARVIS 巡逻:发现异常后采集只读证据。
    agent_jarvis_patrol_enabled: bool = True
    # 成本保护:默认不把定时巡逻简报自动交给模型处理;管理员明确发起时仍可核验。
    agent_jarvis_auto_dispatch_enabled: bool = False
    agent_jarvis_patrol_interval_seconds: int = Field(default=300, ge=60, le=86400)
    agent_jarvis_online_window_minutes: int = Field(default=10, ge=1, le=1440)
    # 动态子 Agent 团队的持久化队列和租约边界；小菱不计入子 Agent 槽位。
    agent_team_enabled: bool = True
    agent_team_max_active_children: int = Field(default=3, ge=1, le=32)
    agent_team_max_queue_length: int = Field(default=100, ge=1, le=10000)
    agent_team_task_lease_seconds: int = Field(default=300, ge=1, le=3600)
    agent_team_dispatch_interval_seconds: int = Field(default=2, ge=1, le=60)
    agent_team_default_max_attempts: int = Field(default=3, ge=1, le=10)
    agent_knowledge_fetch_timeout: int = 15
    agent_knowledge_fetch_max_bytes: int = 1024 * 1024
    agent_knowledge_allow_private_urls: bool = False
    agent_knowledge_enforce_dns_check: bool = False
    agent_knowledge_github_token: str = ""

    # ── AgentSkill 自进化与总调度升级 ──
    # 双层调度总开关: True=意图分类+LLM 规划调用链; False=回退单层 handler
    # 出问题时可设 False 快速降级,不影响主流程
    chat_double_layer_enabled: bool = True
    # Skill 定时进化总开关(每日 03:00 跑 evolution,每小时跑 proactive_check)
    # 关闭时不创建、注册或执行周期 Skill；不影响用户交互与独立事件触发开关。
    skill_scheduler_enabled: bool = True
    # Skill 事件触发总开关(订阅 event_bus 事件触发 Skill)
    skill_event_trigger_enabled: bool = True
    # Skill 触发全局并发上限(定时+事件共用,防雪崩)
    skill_trigger_max_concurrency: int = 3
    # Skill 事件触发去抖窗口(秒,同 key 不重复触发)
    skill_event_debounce_seconds: int = 300
    # Agent SSE 跨 worker 广播。留空时自动降级为当前进程内事件总线。
    redis_url: str = ""

    # 反向代理与登录失败限流。生产 Nginx 会覆盖写入 X-Real-IP；应用只在
    # 直连对端属于这些网段时信任该请求头，避免客户端伪造 X-Forwarded-For。
    trusted_proxy_cidrs: List[str] = [
        "127.0.0.0/8",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    ]
    login_failure_limit: int = Field(default=5, ge=1, le=100)
    login_failure_window_seconds: int = Field(default=60, ge=10, le=3600)

    # 远程项目异步导入队列。任务与租约落库，进程或容器重启后可重新领取。
    project_import_dispatcher_enabled: bool = True
    project_import_dispatch_interval_seconds: int = Field(default=2, ge=1, le=60)
    project_import_max_workers: int = Field(default=1, ge=1, le=4)
    project_import_max_attempts: int = Field(default=3, ge=1, le=10)
    project_import_lease_seconds: int = Field(default=900, ge=60, le=3600)

    # MCP Server 由平台在本地发现并转换为 function tools。DeepSeek 当前会忽略
    # 原生 type=mcp，因此不能把配置直接透传给上游。
    mcp_servers_json: str = "[]"
    mcp_allow_private_urls: bool = False
    mcp_timeout: float = 30.0
    agent_event_stream_maxlen: int = 500

    # 不可信代码只通过独立沙箱执行器运行，后端不挂载 Docker Socket。
    sandbox_enabled: bool = False
    sandbox_executor_socket: str = "/var/lib/prism-sandbox/agent.sock"
    sandbox_maintenance_file: str = "/var/lib/prism-sandbox/maintenance.lock"
    sandbox_executor_token: str = ""
    sandbox_default_ttl_hours: int = 72
    sandbox_max_ttl_hours: int = 168
    sandbox_max_concurrency: int = 1
    sandbox_required_runtime: str = "runsc"
    sandbox_mode: str = "strict"
    sandbox_allow_runc: bool = False
    sandbox_remote_targets_enabled: bool = True
    sandbox_max_repair_rounds: int = 2
    sandbox_repair_max_files: int = 3
    # 测试用例 LLM 生成总预算：超时后跳过动态白盒用例，只保留静态/黑盒链路。
    sandbox_agent_test_generation_seconds: int = Field(default=300, ge=60, le=600)
    # 进程内 TTL 缓存：相同源码/语言/测试模式的 agent 测试用例在窗口内直接复用。
    sandbox_agent_test_cache_seconds: int = Field(default=3600, ge=60, le=86400)
    # 长任务心跳与卡死回收。
    sandbox_heartbeat_seconds: int = Field(default=30, ge=10, le=300)
    sandbox_stuck_after_seconds: int = Field(default=900, ge=120, le=3600)
    sandbox_remote_timeout: int = 30

    # 宿主机运维白名单执行器；仅 Unix Socket，不开放 TCP。
    ops_executor_socket: str = "/run/prism-ops/agent.sock"
    ops_executor_token: str = ""
    ops_automation_enabled: bool = True
    # 成本保护:定时健康检查只采集状态和生成告警,默认不调用 LLM 诊断。
    ops_health_diagnosis_enabled: bool = False

    # ── 安全监控（security_monitor_service 规则阈值） ──
    security_monitor_enabled: bool = True
    security_monitor_interval_minutes: int = 5
    # SSH 登录来源白名单 CIDR；命中白名单的成功登录只入库不弹窗。
    security_ssh_allowlist_cidrs: List[str] = []
    security_failed_login_threshold: int = 20
    security_failed_login_window_hours: int = 1
    security_flytrap_threshold: int = 10
    security_flytrap_window_hours: int = 1
    security_backup_max_age_hours: int = 30
    security_backup_dir_max_gb: int = 10
    # 弹窗最低严重度：info<warning<high<critical
    security_popup_min_severity: str = "warning"
    # 被动溯源情报服务固定前缀（执行器侧再做 URL 白名单校验）
    threat_intel_base_url: str = "http://ip-api.com/json"
    # 生产数据库内部威胁信号巡检（只读 mysql.general_log 采样；须先由运维开启
    # general_log 且 log_output=TABLE，未开启时该动作返回 ok=False 不影响整体巡检）。
    security_db_monitor_enabled: bool = True
    security_db_window_hours: int = 1
    security_db_sample_limit: int = 4000
    security_db_destructive_threshold: int = 1
    security_db_dump_threshold: int = 1
    security_db_error_threshold: int = 3
    # 生产数据库自动备份（调度）。开关默认关闭，须由最高管理员在 .env 显式开启；
    # 备份目录自动轮换清理由 backup.sh 按 BACKUP_RETENTION_DAYS 完成。
    backup_schedule_enabled: bool = False
    backup_schedule_retention_days: int = 14

    max_upload_size: int = 20 * 1024 * 1024  # 20MB
    allowed_extensions: List[str] = ["*"]

    cors_origins: List[str] = ["http://localhost:5173"]

    @property
    def db_url(self) -> str:
        """构建数据库连接URL"""
        if self.db_host.lower() == "sqlite" or "sqlite" in self.db_name:
            return f"sqlite:///./{self.db_name}.db"
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}@"
            f"{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )

    @model_validator(mode="after")
    def _guard_deepseek_context_budget(self) -> "Settings":
        problems: List[str] = []
        if not 100_000 <= self.deepseek_context_window_tokens <= 1_000_000:
            problems.append("DEEPSEEK_CONTEXT_WINDOW_TOKENS 必须在 100000 到 1000000 之间")
        if not 128 <= self.deepseek_max_output_tokens <= 384_000:
            problems.append("DEEPSEEK_MAX_OUTPUT_TOKENS 必须在 128 到 384000 之间")
        if self.security_semantic_max_output_tokens > self.deepseek_max_output_tokens:
            problems.append("SECURITY_SEMANTIC_MAX_OUTPUT_TOKENS 不能超过 DEEPSEEK_MAX_OUTPUT_TOKENS")
        input_budget = self.deepseek_context_window_tokens - self.deepseek_max_output_tokens
        if input_budget <= 0:
            problems.append("DEEPSEEK_MAX_OUTPUT_TOKENS 必须小于上下文窗口")
        elif not 1_000 <= self.deepseek_compaction_threshold_tokens <= input_budget:
            problems.append("DEEPSEEK_COMPACTION_THRESHOLD_TOKENS 必须在 1000 到可用输入预算之间")
        if not 1_000 <= self.deepseek_compaction_keep_recent_tokens <= max(
            self.deepseek_compaction_threshold_tokens,
            0,
        ):
            problems.append("DEEPSEEK_COMPACTION_KEEP_RECENT_TOKENS 必须在 1000 到压缩阈值之间")
        if self.security_semantic_min_split_chars >= self.security_semantic_batch_chars:
            problems.append("SECURITY_SEMANTIC_MIN_SPLIT_CHARS 必须小于初始语义批次字符数")
        if self.security_semantic_bounded_per_file_chars > self.security_semantic_bounded_total_chars:
            problems.append(
                "SECURITY_SEMANTIC_BOUNDED_PER_FILE_CHARS 不能超过有界语义总字符数"
            )
        if (
            self.security_semantic_bounded_total_chars
            > self.security_semantic_bounded_per_file_chars
            * self.security_semantic_bounded_max_files
        ):
            problems.append(
                "SECURITY_SEMANTIC_BOUNDED_TOTAL_CHARS 不能超过单文件上限与文件数上限的乘积"
            )
        # split 最坏可形成约 4 * chars / min_split 个叶片；invalid_item
        # 每个节点最多消耗原调用和契约修复各一次，并预留一次数据流请求。
        max_terminal_leaves = max(1, (self.security_semantic_max_requests + 1) // 4)
        safe_bounded_chars = (
            max_terminal_leaves * self.security_semantic_min_split_chars // 4
        )
        if self.security_semantic_bounded_total_chars > safe_bounded_chars:
            problems.append(
                "SECURITY_SEMANTIC_BOUNDED_TOTAL_CHARS 超过共享请求预算的保守闭合上限 "
                f"{safe_bounded_chars}"
            )
        # 分割点会尽量贴近换行边界，最坏时较大子叶约为父节点的 3/4。
        # 配置深度必须足以把有界窗口降至终端叶片上限。
        required_split_depth = 0
        worst_leaf_chars = self.security_semantic_bounded_total_chars
        while worst_leaf_chars > self.security_semantic_min_split_chars:
            worst_leaf_chars = (worst_leaf_chars * 3 + 3) // 4
            required_split_depth += 1
        if self.security_semantic_max_split_depth < required_split_depth:
            problems.append(
                "SECURITY_SEMANTIC_MAX_SPLIT_DEPTH 不足以在最坏分割下闭合有界语义窗口,"
                f"至少需要 {required_split_depth}"
            )
        # 使用高于数据库当前 500 字符路径上限的保守开销，防止
        # 路径、语言和分隔标记使有界窗口意外拆成多个初始批次。
        bounded_batch_estimate = (
            self.security_semantic_bounded_total_chars
            + self.security_semantic_bounded_max_files * 2_168
        )
        if bounded_batch_estimate > self.security_semantic_batch_chars:
            problems.append(
                "有界语义窗口连同文件路径开销必须能够装入一个初始批次"
            )
        if problems:
            raise ValueError("检测到无效的 DeepSeek 上下文预算: " + "; ".join(problems))
        return self

    @model_validator(mode="after")
    def _guard_production_secrets(self) -> "Settings":
        """非 dev 环境下,拒绝使用不安全的默认密钥/API Key 启动。

        生产忘配 .env 时会静默沿用公开的默认 JWT 密钥,导致任何人都能伪造令牌;
        此处在配置加载阶段直接 fail-fast,把隐患暴露在启动而非线上。
        """
        if self.app_env.lower() == "dev":
            return self
        problems: List[str] = []
        if self.jwt_secret in _INSECURE_JWT_SECRETS:
            problems.append("JWT_SECRET 仍为默认/空值,请设置为足够随机的字符串")
        if self.deepseek_api_key in _INSECURE_API_KEYS:
            problems.append("DEEPSEEK_API_KEY 未正确配置")
        if self.beta_registration_enabled:
            beta_pepper = self.beta_code_pepper.strip()
            if len(beta_pepper) < 32 or beta_pepper.lower().startswith(("change_me", "please-change-me")):
                problems.append("BETA_CODE_PEPPER 必须配置至少 32 字符的独立随机密钥")
            elif beta_pepper == self.jwt_secret.strip():
                problems.append("BETA_CODE_PEPPER 必须与 JWT_SECRET 不同")
        encryption_keys = [key.strip() for key in self.api_key_encryption_keys if key.strip()]
        if not encryption_keys:
            problems.append("API_KEY_ENCRYPTION_KEYS 未配置独立密钥")
        elif len(encryption_keys[0]) < 32 or encryption_keys[0].lower().startswith("change_me"):
            problems.append("API_KEY_ENCRYPTION_KEYS 第一项必须是至少 32 字符的随机密钥")
        elif encryption_keys[0] == self.jwt_secret:
            problems.append("API_KEY_ENCRYPTION_KEYS 第一项必须与 JWT_SECRET 不同")
        if self.ops_automation_enabled and len(self.ops_executor_token.strip()) < 32:
            problems.append("OPS_EXECUTOR_TOKEN 必须配置至少 32 字符的独立随机令牌")
        if not self.malware_scan_fail_closed:
            problems.append("MALWARE_SCAN_FAIL_CLOSED 在非 dev 环境必须为 true")
        if self.sandbox_enabled and len(self.sandbox_executor_token.strip()) < 32:
            problems.append("SANDBOX_EXECUTOR_TOKEN 启用沙箱时必须至少 32 字符")
        if not 1 <= self.sandbox_default_ttl_hours <= self.sandbox_max_ttl_hours <= 720:
            problems.append("沙箱保留时间配置不合法")
        if not 1 <= self.sandbox_max_concurrency <= 8:
            problems.append("SANDBOX_MAX_CONCURRENCY 必须在 1-8")
        if self.sandbox_mode not in {"strict", "local_development"}:
            problems.append("SANDBOX_MODE 只能是 strict 或 local_development")
        if self.sandbox_allow_runc and self.sandbox_mode != "local_development":
            problems.append("SANDBOX_ALLOW_RUNC 只能在 local_development 模式启用")
        if problems:
            raise ValueError(
                f"检测到不安全的生产配置(APP_ENV={self.app_env}): " + "; ".join(problems),
            )
        return self


settings = Settings()
