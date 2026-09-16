"""模型注册表与角色分配:拉取合并/手工编辑/分配校验/运行时解析。"""

import pytest

from app.core.exceptions import ValidationError
from app.services import system_config_service as scs


@pytest.fixture
def clean_registry(db):
    scs.replace_model_registry(db, [])
    db.query(scs.SystemConfig).filter(
        scs.SystemConfig.config_key == scs.MODEL_ASSIGNMENTS_KEY
    ).delete()
    db.commit()
    return db


def test_merge_pulled_marks_vision_and_keeps_manual(clean_registry):
    models, added = scs.merge_pulled_models(clean_registry, [
        "deepseek-v4-flash", "deepseek-v4-flash-vision-exp",
    ])
    assert added == ["deepseek-v4-flash", "deepseek-v4-flash-vision-exp"]
    by_id = {m["id"]: m for m in models}
    assert by_id["deepseek-v4-flash"]["vision"] is True  # 2026-09-10 起兼容路由至 V4.1 Flash
    assert by_id["deepseek-v4-flash-vision-exp"]["vision"] is True
    assert by_id["deepseek-v4-flash-vision-exp"]["source"] == "pulled"
    assert by_id["deepseek-v4-flash-vision-exp"]["added_at"]

    # 手工条目再拉取不丢失、不重复
    scs.replace_model_registry(clean_registry, models + [{"id": "my-custom-model", "label": "自用", "vision": False}])
    models2, added2 = scs.merge_pulled_models(clean_registry, ["deepseek-v4-flash"])
    assert added2 == []
    ids = {m["id"] for m in models2}
    assert "my-custom-model" in ids and len(models2) == 3


def test_assignments_require_registry_membership(clean_registry):
    scs.replace_model_registry(clean_registry, [{"id": "deepseek-v4-flash-vision-exp", "vision": True}])
    result = scs.update_model_assignments(clean_registry, {
        "chat_vision": "deepseek-v4-flash-vision-exp", "chat": "",
    })
    assert result == {"chat_vision": "deepseek-v4-flash-vision-exp"}

    with pytest.raises(ValidationError):
        scs.update_model_assignments(clean_registry, {"chat": "not-in-registry"})
    with pytest.raises(ValidationError):
        scs.update_model_assignments(clean_registry, {"unknown-role": "deepseek-v4-flash-vision-exp"})


def test_resolve_model_assignment_falls_back_to_default(clean_registry):
    scs.replace_model_registry(clean_registry, [{"id": "m1", "vision": False}])
    scs.update_model_assignments(clean_registry, {"chat": "m1"})
    assert scs.resolve_model_assignment(clean_registry, "chat", "default-x") == "m1"
    assert scs.resolve_model_assignment(clean_registry, "orchestrator", "default-x") == "default-x"
    # 清除后回退默认
    scs.update_model_assignments(clean_registry, {"chat": ""})
    assert scs.resolve_model_assignment(clean_registry, "chat", "default-x") == "default-x"


def test_registry_survives_corrupt_payload(db):
    scs._set_raw(db, scs.MODEL_REGISTRY_KEY, "{not-json")
    assert scs.get_model_registry(db) == []
    scs._set_raw(db, scs.MODEL_ASSIGNMENTS_KEY, "[1,2]")
    assert scs.get_model_assignments(db) == {}
