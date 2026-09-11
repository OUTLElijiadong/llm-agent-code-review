"""小菱多模态:图片校验/留档/占位符-还原/视觉模型解析/运行时载荷。"""

import base64
import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import settings
from app.models.agent_multimodal import AgentMultimodalAsset
from app.services import multimodal_service as ms
from app.services.deepseek_responses_runtime import DeepSeekResponsesRuntime, InMemoryCheckpointStore

PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
PNG_URL = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()


def _junk_url() -> str:
    return "data:image/png;base64," + base64.b64encode(b"this is not an image at all").decode()


def test_validate_rejects_bad_format_and_caps_count():
    with pytest.raises(Exception):
        ms.validate_images([_junk_url()])  # 魔数不符
    with pytest.raises(Exception):
        ms.validate_images(["https://example.com/a.png"])  # 外链 URL 一律拒绝(仅支持本地上传)
    assert ms.validate_images([PNG_URL]) == [PNG_URL]
    assert len(ms.validate_images([PNG_URL] * 6)) == 4  # 截断到 4 张


def test_store_dedups_and_builds_parts(db):
    assets = ms.store_message_images(
        db, user_id=9, run_id="run-mm-1", surface="user", images=[PNG_URL, PNG_URL],
    )
    assert len(assets) == 1  # 同内容去重
    row = db.query(AgentMultimodalAsset).one()
    assert row.mime == "image/png" and row.role == "input"
    assert row.sha256 == hashlib.sha256(PNG_1PX).hexdigest()
    assert row.data == PNG_1PX  # 留档原始输入

    parts = ms.multimodal_content_parts("看看这张图", assets)
    assert parts[0] == {"type": "input_text", "text": "看看这张图"}
    assert parts[1]["type"] == "input_image"
    assert parts[1]["image_url"] == f"prism-asset://{row.sha256}"  # 占位符,非 base64


def test_restore_placeholder_roundtrip():
    sha = hashlib.sha256(PNG_1PX).hexdigest()
    items = [{"role": "user", "content": [
        {"type": "input_text", "text": "看图"},
        {"type": "input_image", "image_url": f"prism-asset://{sha}", "detail": "auto"},
    ]}]
    restored = ms.restore_image_placeholders(items, {sha: PNG_URL})
    assert restored[0]["content"][1]["image_url"] == PNG_URL
    # 资产缺失(重启恢复)时降级为文字,不带死链
    degraded = ms.restore_image_placeholders(items, {})
    assert degraded[0]["content"][1] == {"type": "input_text", "text": "[历史图片已归档,无法再次查看]"}


def test_resolve_vision_model_assignment_first(db):
    from app.services import system_config_service as scs

    assert ms.resolve_vision_model(db) == settings.deepseek_vision_model
    scs.replace_model_registry(db, [{"id": "my-vision", "vision": True}])
    scs.update_model_assignments(db, {"chat_vision": "my-vision"})
    assert ms.resolve_vision_model(db) == "my-vision"


@pytest.mark.asyncio
async def test_runtime_sends_real_url_but_checkpoint_keeps_placeholder():
    payloads: list[dict[str, Any]] = []

    async def transport(payload):
        payloads.append(payload)
        return {
            "id": "r1", "status": "completed", "model": "vision", "usage": {},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "一个红色像素"}]}],
        }

    sha = hashlib.sha256(PNG_1PX).hexdigest()
    checkpoint_store = InMemoryCheckpointStore()
    runtime = DeepSeekResponsesRuntime(
        transport=transport,
        tool_executor=SimpleNamespace(),
        checkpoint_store=checkpoint_store,
        model="deepseek-v4-flash-vision-exp",
        image_assets={sha: PNG_URL},
    )
    result = await runtime.start(
        [{"role": "user", "content": [
            {"type": "input_text", "text": "图里是什么"},
            {"type": "input_image", "image_url": f"prism-asset://{sha}", "detail": "auto"},
        ]}],
    )
    assert result.status == "completed"
    # 发往上游的载荷带真实 data URL
    sent = payloads[0]["input"][0]["content"][1]
    assert sent["image_url"] == PNG_URL
    # 检查点只留占位符(不写巨型 base64)
    saved_dict = checkpoint_store._items[result.run_id]
    stored_item = saved_dict["transcript"][0]
    assert stored_item["content"][1]["image_url"] == f"prism-asset://{sha}"
    assert "base64" not in str(saved_dict["transcript"])
