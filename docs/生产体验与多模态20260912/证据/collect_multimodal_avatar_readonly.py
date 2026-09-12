"""只读采集合成验收运行，禁止输出用户上下文与图片原文。"""
import argparse
import hashlib
import json
import re

from sqlalchemy import text

from app.core.database import SessionLocal


def digest(value):
    return hashlib.sha256(value).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--run-id", action="append", default=[])
    args = parser.parse_args()
    if args.user_id < 1 or len(args.run_id) > 10 or any(
        not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item) for item in args.run_id
    ):
        parser.error("必须提供本人正整数编号与最多十个精确运行编号")
    db = SessionLocal()
    result = {"user_id": args.user_id, "runs": [], "missing_owned_run_ids": []}
    try:
        if db.get_bind().dialect.name == "mysql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        avatar = db.execute(text("SELECT avatar FROM user WHERE id=:uid"), {"uid": args.user_id}).first()
        result["user_exists"] = avatar is not None
        result["avatar"] = avatar[0] if avatar else None
        rows = db.execute(text("SELECT id,mime,data,create_time,update_time FROM user_avatar WHERE user_id=:uid"), {"uid": args.user_id}).mappings().all()
        result["avatar_assets"] = [
            {"id": row["id"], "mime": row["mime"], "bytes": len(row["data"]),
             "sha256": digest(bytes(row["data"])), "create_time": row["create_time"], "update_time": row["update_time"]}
            for row in rows
        ]
        for run_id in dict.fromkeys(args.run_id):
            row = db.execute(text("SELECT id,run_id,user_id,surface,session_key,status,root_agent_run_id,agent_run_id,checkpoint_json,create_time,update_time FROM agent_response_run WHERE run_id=:run AND user_id=:uid"), {"run": run_id, "uid": args.user_id}).mappings().first()
            if row is None:
                result["missing_owned_run_ids"].append(run_id)
                continue
            run = dict(row)
            raw = run.pop("checkpoint_json") or "{}"
            checkpoint = json.loads(raw)
            output = str(checkpoint.get("output_text") or "")
            transcript = json.dumps(checkpoint.get("transcript") or [], ensure_ascii=False)
            run["checkpoint"] = {
                "model": checkpoint.get("model"), "status": checkpoint.get("status"),
                "rounds": checkpoint.get("rounds"), "output_chars": len(output),
                "output_sha256": digest(output.encode()),
                "last_response_model": (checkpoint.get("last_response") or {}).get("model"),
                "has_error": bool(checkpoint.get("error")),
                "has_cancel_reason": bool(checkpoint.get("cancel_reason")),
                "pending_kind": (checkpoint.get("pending") or {}).get("kind"),
                "image_sha256_refs": sorted(set(re.findall(r"prism-asset://([a-f0-9]{64})", transcript))),
                "has_image_data_url": "data:image/" in raw,
            }
            assets = db.execute(text("SELECT id,user_id,surface,role,mime,sha256,data,create_time FROM agent_multimodal_asset WHERE run_id=:run AND user_id=:uid"), {"run": run_id, "uid": args.user_id}).mappings().all()
            run["assets"] = []
            for asset in assets:
                item = dict(asset)
                binary = bytes(item.pop("data"))
                item.update(bytes=len(binary), actual_sha256=digest(binary))
                item["sha256_matches"] = item["sha256"] == item["actual_sha256"]
                item["surface_matches"] = item["surface"] == row["surface"]
                run["assets"].append(item)
            logs = db.execute(text("SELECT l.id,l.user_id,l.model_name,l.agent_label,l.status,l.prompt_tokens,l.completion_tokens,l.total_tokens,l.root_agent_run_id,l.agent_run_id,l.tool_execution_id,l.create_time,LENGTH(l.prompt) AS prompt_bytes,LENGTH(l.response) AS response_bytes,r.user_id AS root_owner,a.user_id AS agent_owner FROM ai_call_log l LEFT JOIN agent_response_run r ON r.id=l.root_agent_run_id LEFT JOIN agent_response_run a ON a.id=l.agent_run_id WHERE l.user_id=:uid AND (l.root_agent_run_id=:rid OR l.agent_run_id=:rid) ORDER BY l.id"), {"uid": args.user_id, "rid": row["id"]}).mappings().all()
            run["call_logs"] = [dict(item) for item in logs]
            result["runs"].append(run)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
