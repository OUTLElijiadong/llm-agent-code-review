"""小菱多模态(图片消息)服务

职责:
1) 校验并留档消息中的图片(agent_multimodal_asset 表,审计/回放用);
2) 生成多模态 input parts(text + input_image 占位符);
3) 解析视觉模型(管理员角色分配 chat_vision > 系统默认视觉模型);
4) 运行期把占位符还原为 data URL 发往上游,检查点永不落 base64。

图片约束对齐 DeepSeek 视觉接口:JPEG/PNG/GIF/WebP,单图 ≤1.5MB(应用侧
收紧,远低于上游 32MB),单条消息 ≤4 张。
"""
from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.agent_multimodal import AgentMultimodalAsset
from app.utils.image_utils import decode_data_image_url

MAX_IMAGE_BYTES = 1_500_000
MAX_IMAGES_PER_MESSAGE = 4
ASSET_URL_PREFIX = "prism-asset://"


def validate_images(images: Sequence[str]) -> list[str]:
    """逐张校验 data URL 图片,返回合法列表;超量直接截断到上限。"""
    result: list[str] = []
    for url in list(images)[:MAX_IMAGES_PER_MESSAGE]:
        decoded = decode_data_image_url(url, max_bytes=MAX_IMAGE_BYTES)
        if decoded is None:
            from app.core.exceptions import BadRequestError

            raise BadRequestError(
                "图片格式不支持:仅支持 PNG/JPEG/WebP/GIF,单张不超过 1.5MB", code=40001,
            )
        result.append(url)
    return result


def store_message_images(
    db: Session,
    *,
    user_id: int,
    run_id: str,
    surface: str,
    images: Sequence[str],
) -> list[dict[str, Any]]:
    """校验并留档图片,返回 [{sha256, mime, data_url, asset_id}](同 run 同内容去重)。"""
    stored: list[dict[str, Any]] = []
    seen: set[str] = set()
    for url in validate_images(images):
        import base64

        payload = url.partition(",")[2]
        binary = base64.b64decode(payload)
        digest = hashlib.sha256(binary).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        row = (
            db.query(AgentMultimodalAsset)
            .filter(
                AgentMultimodalAsset.run_id == run_id,
                AgentMultimodalAsset.sha256 == digest,
            )
            .first()
        )
        if row is None:
            mime = decode_data_image_url(url, max_bytes=MAX_IMAGE_BYTES)[0]
            row = AgentMultimodalAsset(
                run_id=run_id, user_id=user_id, surface=surface,
                role="input", mime=mime, sha256=digest, data=binary,
            )
            db.add(row)
            db.flush()
        stored.append({
            "sha256": digest,
            "mime": row.mime,
            "data_url": url,
            "asset_id": int(row.id),
        })
    db.commit()
    return stored


def multimodal_content_parts(text: str, assets: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """构造 Responses input 的 content parts;图片用占位符 URL,运行期再还原。"""
    parts: list[dict[str, Any]] = [{"type": "input_text", "text": text or "(图片)"}]
    for asset in assets:
        parts.append({
            "type": "input_image",
            "image_url": f"{ASSET_URL_PREFIX}{asset['sha256']}",
            "detail": "auto",
        })
    return parts


def image_asset_map(assets: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """sha256 -> data_url,供运行期还原占位符。"""
    return {str(asset["sha256"]): str(asset["data_url"]) for asset in assets}


def resolve_vision_model(db: Session) -> str:
    """视觉模型:管理员角色分配(chat_vision)优先,否则系统默认视觉模型。"""
    from app.services.system_config_service import resolve_model_assignment

    return resolve_model_assignment(db, "chat_vision", settings.deepseek_vision_model)


def restore_image_placeholders(
    items: Sequence[Mapping[str, Any]],
    assets: Mapping[str, str],
) -> list[dict[str, Any]]:
    """把 transcript 中的 prism-asset:// 占位符还原为 data URL(仅用于发往上游的 payload)。

    运行中断后恢复(assets 缺项)时,占位符降级为文字说明,不让请求带死链。
    """
    restored: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        if not isinstance(content, Sequence) or isinstance(content, (str, bytes, bytearray)):
            restored.append(dict(item))
            continue
        new_parts: list[dict[str, Any]] = []
        changed = False
        for part in content:
            if not isinstance(part, Mapping):
                new_parts.append(part)
                continue
            url = str(part.get("image_url") or "")
            if str(part.get("type")) == "input_image" and url.startswith(ASSET_URL_PREFIX):
                sha = url[len(ASSET_URL_PREFIX):]
                data_url = assets.get(sha)
                if data_url:
                    new_parts.append({**dict(part), "image_url": data_url})
                else:
                    new_parts.append({"type": "input_text", "text": "[历史图片已归档,无法再次查看]"})
                changed = True
            else:
                new_parts.append(part)
        restored.append({**dict(item), "content": new_parts} if changed else dict(item))
    return restored
