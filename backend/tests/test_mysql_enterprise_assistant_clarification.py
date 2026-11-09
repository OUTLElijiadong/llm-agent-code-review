"""Disposable MySQL checks for clarification evidence; no tool execution is enabled."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import test_fresh_database_chain as chain
from test_mysql_enterprise_chat_authority import _seed

from app.models.enterprise_assistant_clarification import EnterpriseAssistantClarificationDecision
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage
from app.models.enterprise_org import Enterprise
from app.services import enterprise_assistant_clarification_service as service

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


def test_status_reads_committed_decisions_despite_existing_repeatable_read_snapshot(fresh_chain):
    factory, enterprise_id, _group_id, _owner_id, author_id, room_id, run_public_id = _seed(fresh_chain)
    with factory.begin() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(public_id=run_public_id).one()
        db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one().status = "running"
        run.status = "running"
        db.flush()
        request = service.create_request(
            db, enterprise_id=enterprise_id, conversation_id=room_id, run_id=run.id, phase="tool",
            call_id="call", action="create_registered_enterprise_agent_team", target={}, arguments={"title": "团队"},
            expires_at=service._now() + timedelta(hours=1),
        )
        request_id = request.id
        source_id = db.query(EnterpriseChatMessage.id).filter_by(
            enterprise_id=enterprise_id, conversation_id=room_id, sender_id=author_id,
        ).scalar()
    with factory() as old_snapshot:
        assert old_snapshot.query(EnterpriseAssistantClarificationDecision).filter_by(
            request_id=request_id,
        ).one().decision == "pending"
        with factory.begin() as db:
            service.decide(db, enterprise_id=enterprise_id, conversation_id=room_id, request_id=request_id,
                           source_message_id=source_id, user_id=author_id, decision="allow")
        assert service.status(old_snapshot, enterprise_id=enterprise_id, conversation_id=room_id,
                              request_id=request_id) == "recorded"


def test_new_higher_authority_message_committed_before_check_invalidates_evidence(fresh_chain):
    factory, enterprise_id, _group_id, _owner_id, author_id, room_id, run_public_id = _seed(fresh_chain)
    with factory.begin() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(public_id=run_public_id).one()
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
        state.status = "running"
        run.status = "running"
        db.flush()
        request = service.create_request(
            db, enterprise_id=enterprise_id, conversation_id=room_id, run_id=run.id, phase="tool",
            call_id="call", action="create_registered_enterprise_agent_team", target={}, arguments={"title": "团队"},
            expires_at=service._now() + timedelta(hours=1),
        )
        request_id = request.id
        source_id = db.query(EnterpriseChatMessage.id).filter_by(
            enterprise_id=enterprise_id, conversation_id=room_id, sender_id=author_id,
        ).scalar()
    with factory.begin() as db:
        service.decide(db, enterprise_id=enterprise_id, conversation_id=room_id, request_id=request_id,
                       source_message_id=source_id, user_id=author_id, decision="allow")
    with factory.begin() as db:
        assert service.status(db, enterprise_id=enterprise_id, conversation_id=room_id,
                              request_id=request_id) == "recorded"

    started = Event()

    def check():
        with factory.begin() as db:
            started.set()
            return service.status(db, enterprise_id=enterprise_id, conversation_id=room_id, request_id=request_id)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with factory.begin() as writer:
            writer.query(Enterprise).filter_by(id=enterprise_id).with_for_update().one()
            writer.add(EnterpriseChatMessage(enterprise_id=enterprise_id, conversation_id=room_id,
                                              sender_id=author_id, sender_type="human",
                                              client_message_id="new-authority", content="先暂停"))
            writer.flush()
            future = pool.submit(check)
            assert started.wait(5)
            assert not future.done()
        assert future.result(timeout=10) == "stale"


def test_authority_check_holding_company_lock_precedes_new_message(fresh_chain):
    factory, enterprise_id, _group_id, _owner_id, author_id, room_id, run_public_id = _seed(fresh_chain)
    with factory.begin() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(public_id=run_public_id).one()
        db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one().status = "running"
        run.status = "running"
        db.flush()
        request = service.create_request(
            db, enterprise_id=enterprise_id, conversation_id=room_id, run_id=run.id, phase="tool",
            call_id="call", action="create_registered_enterprise_agent_team", target={}, arguments={"title": "团队"},
            expires_at=service._now() + timedelta(hours=1),
        )
        request_id = request.id
        source_id = db.query(EnterpriseChatMessage.id).filter_by(
            enterprise_id=enterprise_id, conversation_id=room_id, sender_id=author_id,
        ).scalar()
    with factory.begin() as db:
        service.decide(db, enterprise_id=enterprise_id, conversation_id=room_id, request_id=request_id,
                       source_message_id=source_id, user_id=author_id, decision="allow")

    holding, release, writer_started = Event(), Event(), Event()

    def check_and_hold():
        with factory.begin() as db:
            result = service.status(db, enterprise_id=enterprise_id, conversation_id=room_id, request_id=request_id)
            holding.set()
            assert release.wait(10)
            return result

    def write_after_check():
        with factory.begin() as db:
            writer_started.set()
            db.query(Enterprise).filter_by(id=enterprise_id).with_for_update().one()
            db.add(EnterpriseChatMessage(enterprise_id=enterprise_id, conversation_id=room_id,
                                         sender_id=author_id, sender_type="human",
                                         client_message_id="after-check", content="先暂停"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        checked = pool.submit(check_and_hold)
        assert holding.wait(5)
        writer = pool.submit(write_after_check)
        assert writer_started.wait(5)
        assert not writer.done()
        release.set()
        assert checked.result(timeout=10) == "recorded"
        writer.result(timeout=10)
    with factory.begin() as db:
        assert service.status(db, enterprise_id=enterprise_id, conversation_id=room_id,
                              request_id=request_id) == "stale"
