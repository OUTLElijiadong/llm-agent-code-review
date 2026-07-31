"""
FastAPI应用核心配置模块
使用pydantic-settings从环境变量/.env文件加载所有配置项
"""

from pathlib import Path
from typing import List

from pydantic import model_validator
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
    malware_scan_fail_closed: bool = False

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
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_timeout: int = 60
    deepseek_max_retries: int = 2
    deepseek_chunk_threshold: int = 6000
    # DeepSeek V4 上下文窗口与内部 Agent 投影预算。完整 transcript 仍持久化供审计。
    deepseek_context_window_tokens: int = 1_000_000
    deepseek_max_output_tokens: int = 32_768
    deepseek_compaction_threshold_tokens: int = 850_000
    deepseek_compaction_keep_recent_tokens: int = 200_000
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
    skill_scheduler_enabled: bool = True
    # Skill 事件触发总开关(订阅 event_bus 事件触发 Skill)
    skill_event_trigger_enabled: bool = True
    # Skill 触发全局并发上限(定时+事件共用,防雪崩)
    skill_trigger_max_concurrency: int = 3
    # Skill 事件触发去抖窗口(秒,同 key 不重复触发)
    skill_event_debounce_seconds: int = 300
    # Agent SSE 跨 worker 广播。留空时自动降级为当前进程内事件总线。
    redis_url: str = ""

    # MCP Server 由平台在本地发现并转换为 function tools。DeepSeek 当前会忽略
    # 原生 type=mcp，因此不能把配置直接透传给上游。
    mcp_servers_json: str = "[]"
    mcp_allow_private_urls: bool = False
    mcp_timeout: float = 30.0
    agent_event_stream_maxlen: int = 500

    # 宿主机运维白名单执行器；仅 Unix Socket，不开放 TCP。
    ops_executor_socket: str = "/run/prism-ops/agent.sock"
    ops_executor_token: str = ""
    ops_automation_enabled: bool = True

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
        if problems:
            raise ValueError(
                f"检测到不安全的生产配置(APP_ENV={self.app_env}): " + "; ".join(problems),
            )
        return self


settings = Settings()
