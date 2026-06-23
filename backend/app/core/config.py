"""
FastAPI应用核心配置模块
使用pydantic-settings从环境变量/.env文件加载所有配置项
"""
from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parent.parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "dev"
    log_level: str = "INFO"
    openapi_enabled: bool = True

    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "code_review"

    jwt_secret: str = "code-review-platform-dev-secret-key"
    jwt_algorithm: str = "HS256"
    jwt_expire_seconds: int = 7 * 24 * 3600

    deepseek_api_key: str = "sk-xxxxx"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_timeout: int = 60
    deepseek_max_retries: int = 2
    deepseek_chunk_threshold: int = 6000

    # ── RAG 嵌入(embedding)配置 ──
    # 留空则降级为本地确定性哈希向量,保证无 Key 也能跑通(语义弱)。
    # 运行期可被 system_config 表(管理员在前端配置)覆盖。
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dim: int = 256          # 本地降级向量维度
    embedding_timeout: int = 30

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


settings = Settings()
