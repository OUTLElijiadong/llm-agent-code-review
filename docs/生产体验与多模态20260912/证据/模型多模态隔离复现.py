"""仅使用内存数据库/模拟上游，核对生产提交 bc207b2 的模型缺口。"""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.models.system_config import SystemConfig
from app.services import system_config_service as scs
from app.services.deepseek_responses_runtime import DeepSeekResponsesRuntime, InMemoryCheckpointStore
from app.services.multimodal_service import multimodal_content_parts
from app.utils.image_utils import decode_data_image_url

async def main():
    evidence = {"commit": "bc207b273441e2f01f988122d28e2f2acb17f4c5", "environment": "in-memory SQLite and mock transport only", "findings": {}}
    engine = create_engine("sqlite:///:memory:")
    SystemConfig.__table__.create(engine)
    with Session(engine) as db:
        models, _ = scs.merge_pulled_models(db, ["deepseek-flash", "deepseek-v4-pro"])
        evidence["findings"]["latest_flash_incorrect_vision_flag"] = next(m for m in models if m["id"] == "deepseek-flash")["vision"] is False
        assignment = scs.update_model_assignments(db, {"chat_vision": "deepseek-v4-pro"})
        evidence["findings"]["nonvision_assignment_accepted"] = assignment["chat_vision"] == "deepseek-v4-pro"
        scs.replace_model_registry(db, [])
        evidence["findings"]["deleted_model_assignment_still_resolves"] = scs.resolve_model_assignment(db, "chat_vision", "default") == "deepseek-v4-pro"
    header_only = b"\x89PNG\r\n\x1a\n"
    image_url = "data:image/png;base64," + base64.b64encode(header_only).decode()
    evidence["findings"]["header_only_invalid_png_accepted"] = decode_data_image_url(image_url, max_bytes=1500000) is not None
    sha = hashlib.sha256(header_only).hexdigest()
    store = InMemoryCheckpointStore()
    payloads = []
    async def first(payload):
        payloads.append(payload)
        return {"id": "first", "status": "completed", "output": [{"type": "function_call", "call_id": "ask1", "name": "ask_user", "arguments": json.dumps({"question": "需要确认哪部分？"})}]}
    runtime = DeepSeekResponsesRuntime(transport=first, tool_executor=SimpleNamespace(), checkpoint_store=store, model="deepseek-flash", image_assets={sha: image_url})
    result = await runtime.start([{"role": "user", "content": multimodal_content_parts("请看图", [{"sha256": sha}])}], run_id="run-audit-vision")
    assert result.status == "waiting_input"
    async def resumed(payload):
        payloads.append(payload)
        return {"id": "second", "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "完成"}]}]}
    # 实际 Service.resume 同样调用 _runtime(run_id,event_sink)，没有 image_assets。
    fresh_runtime = DeepSeekResponsesRuntime(transport=resumed, tool_executor=SimpleNamespace(), checkpoint_store=store, model="deepseek-v4-pro")
    await fresh_runtime.answer("run-audit-vision", "查看标题", "ask1")
    evidence["findings"]["resume_sends_unresolved_prism_asset_url"] = "prism-asset://" in json.dumps(payloads[-1])
    evidence["first_request_image_url_scheme"] = payloads[0]["input"][0]["content"][1]["image_url"].split(":")[0]
    evidence["resumed_request_image_url_scheme"] = payloads[-1]["input"][0]["content"][1]["image_url"].split(":")[0]
    evidence["resumed_checkpoint_model"] = payloads[-1]["model"]
    assert all(evidence["findings"].values())
    out = Path(__file__).with_suffix(".json")
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))

asyncio.run(main())
