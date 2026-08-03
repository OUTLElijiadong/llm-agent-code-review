"""生产敏感配置门禁测试。"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_deepseek_context_budget_defaults_to_one_million_tokens():
    configured = Settings(_env_file=None)

    assert configured.deepseek_context_window_tokens == 1_000_000
    assert configured.deepseek_max_output_tokens == 32_768
    assert configured.security_semantic_max_output_tokens == 16_384
    assert configured.security_semantic_bounded_total_chars == 16_000
    assert configured.security_semantic_bounded_per_file_chars == 4_000
    assert configured.security_semantic_bounded_max_files == 4
    assert configured.deepseek_compaction_threshold_tokens == 850_000
    assert configured.deepseek_compaction_keep_recent_tokens == 200_000


@pytest.mark.parametrize(
    "overrides",
    [
        {"deepseek_context_window_tokens": 1_000_001},
        {
            "deepseek_context_window_tokens": 100_000,
            "deepseek_max_output_tokens": 99_000,
            "deepseek_compaction_threshold_tokens": 10_000,
        },
        {
            "deepseek_compaction_threshold_tokens": 100_000,
            "deepseek_compaction_keep_recent_tokens": 100_001,
        },
        {"security_semantic_max_output_tokens": 40_000},
        {"security_semantic_bounded_per_file_chars": 16_001},
        {"security_semantic_bounded_total_chars": 16_001},
        {"security_semantic_bounded_max_files": 3},
        {"security_semantic_max_split_depth": 7},
    ],
)
def test_rejects_invalid_deepseek_context_budgets(overrides):
    with pytest.raises(ValidationError, match="DeepSeek 上下文预算"):
        Settings(_env_file=None, **overrides)


def test_production_requires_independent_api_key_encryption_key():
    """生产环境不得继续只依赖 JWT_SECRET 加密持久化 API Key。"""
    with pytest.raises(ValidationError, match="API_KEY_ENCRYPTION_KEYS"):
        Settings(
            _env_file=None,
            app_env="prod",
            jwt_secret="production-jwt-secret-123456789012345",
            deepseek_api_key="sk-production-key",
            api_key_encryption_keys=[],
        )


def test_production_accepts_independent_api_key_encryption_key():
    """配置足够长的独立主密钥后生产设置应通过。"""
    configured = Settings(
        _env_file=None,
        app_env="prod",
        jwt_secret="production-jwt-secret-123456789012345",
        deepseek_api_key="sk-production-key",
        api_key_encryption_keys=["production-api-key-secret-123456789012345"],
        ops_executor_token="production-ops-token-12345678901234567890",
        malware_scan_fail_closed=True,
    )

    assert configured.api_key_encryption_keys == ["production-api-key-secret-123456789012345"]


def test_production_rejects_api_key_encryption_key_equal_to_jwt():
    """持久化 API Key 主密钥必须与 JWT 签名密钥相互独立。"""
    shared_secret = "shared-production-secret-123456789012345"

    with pytest.raises(ValidationError, match="必须与 JWT_SECRET 不同"):
        Settings(
            _env_file=None,
            app_env="prod",
            jwt_secret=shared_secret,
            deepseek_api_key="sk-production-key",
            api_key_encryption_keys=[shared_secret],
        )


def test_production_beta_registration_requires_independent_pepper():
    """生产启用内测注册时必须配置独立 Pepper。"""

    with pytest.raises(ValidationError, match="BETA_CODE_PEPPER"):
        Settings(
            _env_file=None,
            app_env="prod",
            jwt_secret="production-jwt-secret-123456789012345",
            deepseek_api_key="sk-production-key",
            api_key_encryption_keys=["production-api-key-secret-123456789012345"],
            beta_registration_enabled=True,
            beta_code_pepper="",
        )


def test_production_accepts_independent_beta_pepper():
    configured = Settings(
        _env_file=None,
        app_env="prod",
        jwt_secret="production-jwt-secret-123456789012345",
        deepseek_api_key="sk-production-key",
        api_key_encryption_keys=["production-api-key-secret-123456789012345"],
        beta_registration_enabled=True,
        beta_code_pepper="production-beta-pepper-123456789012345",
        ops_executor_token="production-ops-token-12345678901234567890",
        malware_scan_fail_closed=True,
    )

    assert configured.beta_registration_enabled is True


@pytest.mark.parametrize(
    "pepper",
    [
        "change_me_beta_code_pepper_at_least_32_chars",
        "please-change-me-to-independent-32-byte-random-string",
    ],
)
def test_production_rejects_beta_pepper_placeholders(pepper):
    with pytest.raises(ValidationError, match="BETA_CODE_PEPPER"):
        Settings(
            _env_file=None,
            app_env="prod",
            jwt_secret="production-jwt-secret-123456789012345",
            deepseek_api_key="sk-production-key",
            api_key_encryption_keys=["production-api-key-secret-123456789012345"],
            beta_registration_enabled=True,
            beta_code_pepper=pepper,
        )


def test_production_rejects_whitespace_wrapped_jwt_as_beta_pepper():
    jwt_secret = "production-jwt-secret-123456789012345"
    with pytest.raises(ValidationError, match="必须与 JWT_SECRET 不同"):
        Settings(
            _env_file=None,
            app_env="prod",
            jwt_secret=jwt_secret,
            deepseek_api_key="sk-production-key",
            api_key_encryption_keys=["production-api-key-secret-123456789012345"],
            beta_registration_enabled=True,
            beta_code_pepper=f"  {jwt_secret}  ",
        )
