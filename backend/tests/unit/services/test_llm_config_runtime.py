"""全局 LLM 运行参数持久化和解析回退。"""

import json

import pytest

from app.api.v1.llm_config import _resolve_draft
from app.core.config import settings
from app.core.exceptions import ValidationError
from app.models.system_config import SystemConfig
from app.schemas.llm_config import LlmTestIn
from app.services import system_config_service
from app.utils.api_resolver import resolve_api_config


def test_global_runtime_options_round_trip(db, monkeypatch):
    monkeypatch.setattr("app.utils.api_resolver._resolve_host", lambda _host: {"93.184.216.34"})
    saved = system_config_service.update_llm_config(
        db,
        provider="openai",
        base_url="https://api.example.com/v1",
        model="gpt-4o",
        api_key="sk-runtime",
        active=True,
        timeout_seconds=45,
        max_retries=4,
        temperature=1.1,
    )

    assert saved["timeout_seconds"] == 45
    assert saved["max_retries"] == 4
    assert saved["temperature"] == 1.1
    cfg = resolve_api_config(db)
    assert cfg.source == "global"
    assert cfg.timeout_seconds == 45
    assert cfg.max_retries == 4
    assert cfg.temperature == 1.1


@pytest.mark.parametrize(
    ("field", "value"),
    [("timeout_seconds", 4), ("timeout_seconds", 601), ("max_retries", -1), ("max_retries", 6),
     ("temperature", -0.1), ("temperature", 2.1)],
)
def test_update_global_runtime_options_reject_invalid_values(db, field, value):
    with pytest.raises(ValidationError):
        system_config_service.update_llm_config(
            db,
            base_url="https://api.example.com",
            **{field: value},
        )


def test_corrupt_runtime_values_are_safely_defaulted(db):
    row = SystemConfig(
        config_key=system_config_service.LLM_KEY,
        config_value=json.dumps({
            "provider": "custom",
            "base_url": "https://api.example.com",
            "model": "model",
            "api_key_enc": "bad",
            "active": False,
            "timeout_seconds": "not-a-number",
            "max_retries": 99,
            "temperature": -10,
        }),
    )
    db.add(row)
    db.commit()

    cfg = system_config_service.get_llm_config(db)
    assert cfg["timeout_seconds"] == settings.deepseek_timeout
    assert cfg["max_retries"] == settings.deepseek_max_retries
    assert cfg["temperature"] == settings.deepseek_temperature


def test_update_can_recover_from_legacy_non_object_json(db, monkeypatch):
    row = SystemConfig(
        config_key=system_config_service.LLM_KEY,
        config_value='["legacy", "invalid"]',
    )
    db.add(row)
    db.commit()
    monkeypatch.setattr("app.utils.api_resolver._resolve_host", lambda _host: {"93.184.216.34"})

    saved = system_config_service.update_llm_config(
        db,
        provider="custom",
        base_url="https://api.example.com/v1",
        model="recovered-model",
        api_key="sk-recovered",
        active=True,
    )

    assert saved["source"] == "global"
    assert saved["model"] == "recovered-model"


def test_draft_never_reuses_stored_key_for_a_different_endpoint(db, monkeypatch):
    monkeypatch.setattr(
        system_config_service,
        "get_llm_config",
        lambda _db: {
            "provider": "custom",
            "base_url": "https://old.example.com/v1",
            "model": "old-model",
            "api_key": "sk-old-secret",
        },
    )

    draft = _resolve_draft(
        LlmTestIn(base_url="https://new.example.com/v1", model="new-model"),
        db,
    )

    assert draft["api_key"] == ""


def test_draft_can_reuse_stored_key_for_the_same_normalized_endpoint(db, monkeypatch):
    monkeypatch.setattr(
        system_config_service,
        "get_llm_config",
        lambda _db: {
            "provider": "custom",
            "base_url": "https://api.example.com/v1/",
            "model": "saved-model",
            "api_key": "sk-saved-secret",
        },
    )

    draft = _resolve_draft(
        LlmTestIn(base_url="https://api.example.com/v1/chat/completions"),
        db,
    )

    assert draft["api_key"] == "sk-saved-secret"
