"""完整脱敏的小结果及有界结构摘要；原结果通过同账号引用分页读取。"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _encoded_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")).encode())


def project_team_result(value: Any, *, team_id: int, task_id: int | None = None,
                        result_scope: str = "task", result_path: str = "",
                        result_indices: list[int] | None = None, budget: int = 16000) -> Any:
    from app.services.agent_responses_service import redact_agent_model_context_value

    if budget < 1024:
        raise ValueError("结构摘要预算不能低于1024字节")
    if (result_scope not in {"task", "team_summary", "team_error"}
            or (result_scope == "task") != (task_id is not None)):
        raise ValueError("结果引用必须明确指定任务或团队摘要/错误作用域")
    # 脱敏器失败直接抛错，不以未经脱敏的值替代。
    safe = redact_agent_model_context_value(value)
    raw = json.dumps(safe, ensure_ascii=False, default=str, separators=(",", ":")).encode()
    if len(raw) <= budget:
        return safe
    reference = {"tool": "get_agent_team", "team_id": team_id, "result_scope": result_scope,
                 "result_path": result_path, "offset": 0, "limit": 20}
    if task_id is not None:
        reference["task_id"] = task_id
    if result_indices is not None:
        reference.pop("result_path")
        reference["result_indices"] = result_indices
    projection_summary = "结果超过单次上下文预算；此处为结构摘要，完整脱敏结果仍可按引用读取。"
    business_summary = safe.get("summary") if isinstance(safe, dict) else None
    if not isinstance(business_summary, str):
        business_summary = None
    preview = {}
    if isinstance(safe, dict):
        for key in ("status", "summary", "capability", "status_code", "id", "title", "total", "page", "page_size"):
            if key == "summary":
                continue
            child = safe.get(key)
            if isinstance(child, (int, float, bool)) or isinstance(child, str) and _encoded_size(child) <= 200:
                preview[key] = child
    output = {
        # Keep the business field consumed by terminal-team clients separate from
        # the metadata explaining that this payload is only a projection.
        "summary": business_summary if business_summary is not None else projection_summary,
        "projection_summary": projection_summary,
        "summary_truncated": False,
        "complete": False, "preview": preview,
        "type": type(safe).__name__, "size_bytes": len(raw),
        "item_count": len(safe) if isinstance(safe, (dict, list, str)) else None,
        "field_names": [key for key in safe if _encoded_size(key) <= 100][:20]
        if isinstance(safe, dict) else [],
        "sha256": hashlib.sha256(raw).hexdigest(), "result_reference": reference,
    }
    # 元数据也必须计入UTF-8预算，不截断原值或制造不可访问的长路径。
    while _encoded_size(output) > budget and output["field_names"]:
        output["field_names"].pop()
    while _encoded_size(output) > budget and preview:
        preview.pop(next(reversed(preview)))
    if business_summary is not None and _encoded_size(output) > budget:
        output["summary_truncated"] = True
        low, high = 0, len(business_summary)
        while low < high:
            middle = (low + high + 1) // 2
            output["summary"] = business_summary[:middle] + ("…" if middle < len(business_summary) else "")
            if _encoded_size(output) <= budget:
                low = middle
            else:
                high = middle - 1
        output["summary"] = business_summary[:low] + ("…" if low < len(business_summary) else "")
    if _encoded_size(output) > budget:
        raise ValueError("结构摘要引用超过预算，请使用索引定位引用")
    for key, child in preview.items():
        if key not in output and _encoded_size({**output, key: child}) <= budget:
            output[key] = child
    return output


def pointer_child(path: str, child: Any) -> str:
    return path + "/" + str(child).replace("~", "~0").replace("/", "~1")
