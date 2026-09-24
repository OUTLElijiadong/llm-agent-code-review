"""角色手册修订仅重建明确选中的知识切片。"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "seed_agent_playbooks.py"
_SPEC = importlib.util.spec_from_file_location("seed_agent_playbooks", _SCRIPT_PATH)
assert _SPEC and _SPEC.loader
seed_agent_playbooks = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(seed_agent_playbooks)


def test_targeted_role_playbook_selection() -> None:
    chosen = seed_agent_playbooks.selected_playbooks({"role_permission_guide.md"})
    assert [(item["agent_code"], item["file"]) for item in chosen] == [
        ("chat_assistant", "role_permission_guide.md"),
        ("manager", "role_permission_guide.md"),
    ]
    with pytest.raises(ValueError, match="未知手册"):
        seed_agent_playbooks.selected_playbooks({"not-a-playbook.md"})


def test_targeted_seed_does_not_delete_unrelated_knowledge(monkeypatch, tmp_path) -> None:
    (tmp_path / "role_permission_guide.md").write_text("审查员可依 audit:view 查看操作审计", encoding="utf-8")
    session = SimpleNamespace(close=lambda: None)
    removed = []
    events = []
    added = []
    monkeypatch.setattr(seed_agent_playbooks, "SessionLocal", lambda: session)
    monkeypatch.setattr(seed_agent_playbooks, "CONTENT_DIR", tmp_path)
    def remove_existing(_db, agent_code, title, *, keep_doc_id):
        removed.append((agent_code, title, keep_doc_id))
        events.append(("remove", keep_doc_id))

    monkeypatch.setattr(seed_agent_playbooks, "_delete_existing", remove_existing)

    def add_document(_db, **kwargs):
        added.append(kwargs)
        events.append(("add", len(added)))
        return SimpleNamespace(id=len(added), title=kwargs["title"], chunk_count=1, status="active")

    monkeypatch.setattr(seed_agent_playbooks.agent_knowledge_service, "add_document", add_document)
    seed_agent_playbooks.seed({"role_permission_guide.md"})

    assert len(removed) == len(added) == 2
    assert events == [("add", 1), ("remove", 1), ("add", 2), ("remove", 2)]
    assert {item["agent_code"] for item in added} == {"chat_assistant", "manager"}
    assert {item["source_ref"] for item in added} == {"role_permission_guide.md"}
    assert all("审查员可依 audit:view" in item["content"] for item in added)


def test_embedding_failure_keeps_old_playbook(monkeypatch, tmp_path) -> None:
    (tmp_path / "role_permission_guide.md").write_text("新版角色说明", encoding="utf-8")
    monkeypatch.setattr(seed_agent_playbooks, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(seed_agent_playbooks, "CONTENT_DIR", tmp_path)
    deleted = []
    monkeypatch.setattr(seed_agent_playbooks, "_delete_existing", lambda *_args, **_kwargs: deleted.append(True))

    def fail_embed(*_args, **_kwargs):
        raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(seed_agent_playbooks.agent_knowledge_service, "add_document", fail_embed)
    with pytest.raises(RuntimeError, match="embedding unavailable"):
        seed_agent_playbooks.seed({"role_permission_guide.md"})
    assert not deleted
