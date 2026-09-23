"""刷新会话时恢复同账号的跨运行文本历史。"""

import json
from types import SimpleNamespace
from uuid import uuid4

from app.api.v1 import agent_responses as api
from app.models.agent_multimodal import AgentMultimodalAsset
from app.models.agent_response_run import AgentResponseRun


def _run(db, *, user_id=107, surface="user", session="session-history", messages,
         status="completed", mesh_message_id=None):
    row = AgentResponseRun(
        run_id=f"run-{uuid4().hex}", user_id=user_id, surface=surface,
        session_key=session, mesh_message_id=mesh_message_id, status=status,
        checkpoint_json=json.dumps({"transcript": messages, "rounds": 1}), version=1,
    )
    db.add(row)
    db.commit()
    return row


def _session(db, monkeypatch, *, session="session-history"):
    monkeypatch.setattr(api.agent_mesh_service, "list_session_messages", lambda *_args, **_kwargs: [])
    return api.get_agent_response_session(
        surface="user", session_id=session, db=db, user=SimpleNamespace(id=107),
    ).data


def test_session_restores_original_prompt_and_ordered_background_results(db, monkeypatch):
    prompt = "请组织团队审查这个文件。" * 12
    root = _run(db, messages=[
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": "团队已启动"},
    ])
    _run(db, messages=[{"role": "assistant", "content": "现成 Agent 结果"}],
         mesh_message_id="team-69-task-213-result")
    _run(db, messages=[{"role": "assistant", "content": "临时 Agent 结果"}],
         mesh_message_id="team-69-task-214-result")
    final = _run(db, messages=[{"role": "assistant", "content": "汇总报告"}],
                 mesh_message_id="team-69-task-215-result")
    _run(db, user_id=108, messages=[{"role": "user", "content": "其他账号私有消息"}])
    _run(db, surface="admin", messages=[{"role": "user", "content": "管理员消息"}])
    _run(db, session="other-session", messages=[{"role": "user", "content": "其他会话消息"}])

    data = _session(db, monkeypatch)

    assert data["run"]["run_id"] == final.run_id
    assert data["messages"] == [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": "团队已启动"},
        {"role": "assistant", "content": "现成 Agent 结果"},
        {"role": "assistant", "content": "临时 Agent 结果"},
        {"role": "assistant", "content": "汇总报告"},
    ]
    assert root.user_id == 107


def test_full_history_prefix_is_not_duplicated_and_same_new_prompt_is_preserved(db, monkeypatch):
    first = [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
    ]
    _run(db, messages=first)
    _run(db, messages=first + [
        {"role": "user", "content": "第二问"},
        {"role": "assistant", "content": "第二答"},
    ])
    _run(db, messages=[{"role": "user", "content": "第二问"}])

    assert _session(db, monkeypatch)["messages"] == first + [
        {"role": "user", "content": "第二问"},
        {"role": "assistant", "content": "第二答"},
        {"role": "user", "content": "第二问"},
    ]


def test_active_run_state_stays_selected_while_history_includes_newer_run(db, monkeypatch):
    active = _run(db, status="running", messages=[{"role": "user", "content": "进行中"}])
    _run(db, messages=[{"role": "assistant", "content": "后台完成的消息"}],
         mesh_message_id="background-result")

    data = _session(db, monkeypatch)

    assert data["run"]["run_id"] == active.run_id
    assert data["run"]["status"] == "running"
    assert data["messages"] == [
        {"role": "user", "content": "进行中"},
        {"role": "assistant", "content": "后台完成的消息"},
    ]


def test_later_full_transcript_does_not_repeat_background_reply(db, monkeypatch):
    first = [
        {"role": "user", "content": "审查文件"},
        {"role": "assistant", "content": "团队已启动"},
    ]
    background = {"role": "assistant", "content": "成员已完成"}
    _run(db, messages=first)
    _run(db, messages=[{"role": "user", "content": "内部投递信息"}, background],
         mesh_message_id="task-result")
    _run(db, messages=first + [background, {"role": "user", "content": "继续"}])

    assert _session(db, monkeypatch)["messages"] == first + [
        background, {"role": "user", "content": "继续"},
    ]


def test_truncated_full_transcript_merges_overlapping_window_once(db, monkeypatch):
    first = [{"role": "user", "content": f"消息 {index}"} for index in range(100)]
    _run(db, messages=first)
    _run(db, messages=first + [{"role": "user", "content": "第 101 条"}])

    assert _session(db, monkeypatch)["messages"] == first[1:] + [
        {"role": "user", "content": "第 101 条"},
    ]


def test_partial_history_window_does_not_repeat_existing_messages(db, monkeypatch):
    first = [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "第二问"},
        {"role": "assistant", "content": "第二答"},
    ]
    _run(db, messages=first)
    _run(db, messages=first[-2:] + [{"role": "user", "content": "第三问"}])

    assert _session(db, monkeypatch)["messages"] == first + [
        {"role": "user", "content": "第三问"},
    ]


def test_identical_full_checkpoint_does_not_repeat_old_messages(db, monkeypatch):
    history = [
        {"role": "user", "content": "已提交的请求"},
        {"role": "assistant", "content": "正在处理"},
    ]
    _run(db, messages=history)
    _run(db, messages=history)

    assert _session(db, monkeypatch)["messages"] == history


def test_later_run_absorbs_only_some_background_replies_without_repeating_them(db, monkeypatch):
    first = [
        {"role": "user", "content": "开始审查"},
        {"role": "assistant", "content": "团队已启动"},
    ]
    one = {"role": "assistant", "content": "第一个成员完成"}
    two = {"role": "assistant", "content": "第二个成员完成"}
    _run(db, messages=first)
    _run(db, messages=[one], mesh_message_id="member-one")
    _run(db, messages=[two], mesh_message_id="member-two")
    _run(db, messages=first + [one, {"role": "user", "content": "继续"}])

    assert _session(db, monkeypatch)["messages"] == first + [
        one, two, {"role": "user", "content": "继续"},
    ]


def test_history_reads_older_page_when_recent_runs_have_no_visible_text(db, monkeypatch):
    _run(db, messages=[{"role": "user", "content": "最早的可见提问"}])
    for _ in range(100):
        _run(db, messages=[])
    _run(db, messages=[{"role": "assistant", "content": "最新的答复"}],
         mesh_message_id="newer-background-reply")

    assert _session(db, monkeypatch)["messages"] == [
        {"role": "user", "content": "最早的可见提问"},
        {"role": "assistant", "content": "最新的答复"},
    ]


def test_merged_history_keeps_exact_image_prefix_without_copying_payload(db, monkeypatch):
    sha = "b" * 64
    root = _run(db, messages=[
        {"role": "user", "content": [
            {"type": "input_text", "text": "看这张图片"},
            {"type": "input_image", "image_url": "prism-asset://" + sha},
        ]},
        {"role": "assistant", "content": "图片已收到"},
    ])
    asset = AgentMultimodalAsset(
        user_id=107, run_id=root.run_id, surface="user", role="input",
        mime="image/png", sha256=sha, data=b"fixture",
    )
    db.add(asset)
    db.commit()
    _run(db, messages=[
        {"role": "user", "content": "看这张图片"},
        {"role": "assistant", "content": "图片已收到"},
        {"role": "user", "content": "继续说明"},
    ])

    messages = _session(db, monkeypatch)["messages"]

    assert messages == [
        {"role": "user", "content": "看这张图片", "image_assets": [
            {"id": asset.id, "mime": "image/png", "sha256": sha},
        ]},
        {"role": "assistant", "content": "图片已收到"},
        {"role": "user", "content": "继续说明"},
    ]
    assert "fixture" not in json.dumps(messages)


def test_one_message_prefix_is_not_repeated_when_later_run_adds_a_user(db, monkeypatch):
    first = {"role": "user", "content": "第一问"}
    second = {"role": "user", "content": "第二问"}
    _run(db, messages=[first])
    _run(db, messages=[first, second])

    assert _session(db, monkeypatch)["messages"] == [first, second]


def test_one_message_prefix_with_new_assistant_and_user_is_not_repeated(db, monkeypatch):
    first = {"role": "user", "content": "第一问"}
    reply = {"role": "assistant", "content": "不同的答复"}
    followup = {"role": "user", "content": "再问一次"}
    _run(db, messages=[first])
    _run(db, messages=[first, reply, followup])

    assert _session(db, monkeypatch)["messages"] == [first, reply, followup]


def test_two_independent_runs_with_same_single_user_prompt_are_both_kept(db, monkeypatch):
    prompt = {"role": "user", "content": "相同的独立问题"}
    _run(db, messages=[prompt])
    _run(db, messages=[prompt])

    assert _session(db, monkeypatch)["messages"] == [prompt, prompt]


def test_interleaved_background_replies_are_not_repeated_by_later_user_run(db, monkeypatch):
    history = [
        {"role": "user", "content": "发起"},
        {"role": "assistant", "content": "已启动"},
    ]
    first_reply = {"role": "assistant", "content": "后台结果一"}
    question = {"role": "user", "content": "追问"}
    second_reply = {"role": "assistant", "content": "后台结果二"}
    final = {"role": "user", "content": "下一问"}
    _run(db, messages=history)
    _run(db, messages=[first_reply], mesh_message_id="background-one")
    _run(db, messages=history + [question])
    _run(db, messages=[second_reply], mesh_message_id="background-two")
    _run(db, messages=history + [question, second_reply, final])

    assert _session(db, monkeypatch)["messages"] == history + [
        first_reply, question, second_reply, final,
    ]
