"""会话图片回放仅返回本人同会话资产元数据，时间必须带UTC时区。"""
import json
from datetime import datetime, timezone

from app.api.v1 import agent_responses as api
from app.models.agent_multimodal import AgentMultimodalAsset
from app.models.agent_response_run import AgentResponseRun

SHA = "a" * 64


def seed(db, *, user_id=107, session="history-test", surface="user", text="描述合成圆形", image=True, tail=False):
    transcript = [{"role": "user", "content": [
        {"type": "input_text", "text": text},
        *([{"type": "input_image", "image_url": "prism-asset://" + SHA}] if image else []),
    ]}, {"role": "assistant", "content": "蓝色圆形"}]
    if tail:
        transcript.append({"role": "user", "content": "只回复验收完成"})
    row = AgentResponseRun(run_id=f"run-{user_id}-{session}-{surface}-{image}-{tail}", user_id=user_id,
                           surface=surface, session_key=session, status="completed",
                           checkpoint_json=json.dumps({"transcript": transcript}), version=1)
    db.add(row)
    db.flush()
    asset = None
    if image:
        asset = AgentMultimodalAsset(user_id=user_id, run_id=row.run_id, surface=surface,
                                    role="input", mime="image/png", sha256=SHA, data=b"fixture")
        db.add(asset)
    db.commit()
    return row, asset


def test_current_run_replays_asset_metadata_without_base64(db):
    row, asset = seed(db)
    original = row.checkpoint_json
    messages = api._public_session_messages(db, row, json.loads(row.checkpoint_json))
    assert messages[0] == {"role": "user", "content": "描述合成圆形", "image_assets": [
        {"id": asset.id, "mime": "image/png", "sha256": SHA}]}
    assert "data:image" not in json.dumps(messages)
    assert row.checkpoint_json == original


def test_next_text_run_restores_original_image_from_exact_history_prefix(db):
    _, asset = seed(db)
    row, _ = seed(db, image=False, tail=True)
    messages = api._public_session_messages(db, row, json.loads(row.checkpoint_json))
    assert messages[0]["image_assets"][0]["id"] == asset.id
    assert "image_assets" not in messages[-1]


def test_other_owner_session_surface_and_changed_prefix_do_not_supply_images(db):
    seed(db, user_id=108)
    seed(db, session="other-session")
    seed(db, surface="admin")
    seed(db, text="不同问题")
    row, _ = seed(db, image=False, tail=True)
    messages = api._public_session_messages(db, row, json.loads(row.checkpoint_json))
    assert all("image_assets" not in item for item in messages)


def test_naive_and_aware_database_times_both_serialize_as_explicit_utc():
    assert api._public_utc_time(datetime(2026, 9, 12, 16, 33)) == "2026-09-12T16:33:00+00:00"
    assert api._public_utc_time(datetime(2026, 9, 12, 16, 33, tzinfo=timezone.utc)) == "2026-09-12T16:33:00+00:00"


def test_long_public_history_keeps_exact_prefix_and_original_image_position(db):
    _, asset = seed(db)
    row, _ = seed(db, image=False, tail=True)
    checkpoint = json.loads(row.checkpoint_json)
    checkpoint["transcript"].extend({"role": "user", "content": "重复提问"} for _ in range(100))
    messages = api._public_session_messages(db, row, checkpoint)
    assert len(messages) == 103
    assert messages[0]["image_assets"][0]["id"] == asset.id
    assert all("image_assets" not in item for item in messages[1:])


def test_same_question_with_different_later_answer_does_not_attach_branch_image(db):
    seed(db)
    row, _ = seed(db, image=False, tail=True)
    checkpoint = json.loads(row.checkpoint_json)
    checkpoint["transcript"][1]["content"] = "另一分支的答复"
    messages = api._public_session_messages(db, row, checkpoint)
    assert all("image_assets" not in item for item in messages)
