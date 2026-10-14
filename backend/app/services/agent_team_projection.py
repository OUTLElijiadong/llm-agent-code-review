"""完整脱敏的小结果及有界结构摘要；原结果通过同账号引用分页读取。"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def project_team_result(value: Any, *, team_id: int, task_id: int, result_path: str = "",
                        budget: int = 16000) -> Any:
    from app.services.agent_responses_service import redact_agent_model_context_value

    # 脱敏器失败直接抛错，不以未经脱敏的值替代。
    safe = redact_agent_model_context_value(value)
    raw = json.dumps(safe, ensure_ascii=False, default=str, separators=(",", ":")).encode()
    if len(raw) <= budget:
        return safe
    reference = {"tool": "get_agent_team", "team_id": team_id,
                 "task_id": task_id, "result_path": result_path, "offset": 0, "limit": 20}
    preview = {}
    if isinstance(safe, dict):
        for key in ("status", "summary", "capability", "status_code", "id", "title", "total", "page", "page_size"):
            child = safe.get(key)
            if isinstance(child, (int, float, bool)) or isinstance(child, str) and len(child) <= 200:
                preview[key] = child
    return {
        **preview,
        "summary": "结果超过单次上下文预算；此处为结构摘要，完整脱敏结果仍可按引用读取。",
        "complete": False, "preview": preview,
        "type": type(safe).__name__, "size_bytes": len(raw),
        "item_count": len(safe) if isinstance(safe, (dict, list, str)) else None,
        "field_names": list(safe)[:20] if isinstance(safe, dict) else [],
        "sha256": hashlib.sha256(raw).hexdigest(), "result_reference": reference,
    }


def pointer_child(path: str, child: Any) -> str:
    return path + "/" + str(child).replace("~", "~0").replace("/", "~1")
