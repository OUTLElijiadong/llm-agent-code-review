"""A team billing fence must see holds committed after an RR snapshot."""

from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy.orm import sessionmaker
from test_mysql_enterprise_group_shared_lock import _seed

from app.core.exceptions import ConflictError
from app.models.billing_ledger import BillingReservation
from app.models.enterprise_agent_team import EnterpriseAgentTeam
from app.services import billing_ledger_service as ledger
from app.services import enterprise_agent_team_runtime_service as runtime

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url
verified_model_catalog = chain.verified_model_catalog


def test_team_pending_fence_observes_hold_created_after_snapshot(fresh_chain, verified_model_catalog):
    engine = fresh_chain.execution_options(isolation_level="REPEATABLE READ")
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    company, group, _owner, users, tasks, teams = _seed(factory)
    key = f"ent-team:{teams[0]}:{tasks[0]}:1:1"
    identity = ledger.BillingProviderIdentity(
        identity_version="deepseek-official-v1", provider_name="deepseek",
        endpoint_host="api.deepseek.com", credential_source="system",
    )
    with factory() as old_snapshot:
        team = old_snapshot.get(EnterpriseAgentTeam, teams[0])
        assert old_snapshot.query(BillingReservation).filter_by(request_key=key).all() == []
        with factory.begin() as writer:
            wallet = ledger.get_or_create_wallet(writer, "enterprise", company)
            ledger.grant(writer, wallet_id=wallet.id, grant_key=f"snapshot:{uuid4().hex}",
                         kind="purchase", amount_milli=10_000)
            ledger.reserve(writer, request_key=key, model_name="deepseek-flash", user_id=users[0],
                           max_milli=1_000, enterprise_wallet_id=wallet.id, enterprise_id=company,
                           group_id=group, provider_identity=identity)
        # A plain consistent read stays on the old snapshot; the actual fence must
        # use a locking current read to observe and block the new reservation.
        assert old_snapshot.query(BillingReservation).filter_by(request_key=key).all() == []
        rows = runtime._team_reservations(old_snapshot, team)
        assert [(row.request_key, row.status) for row in rows] == [(key, "reserved")]
        with pytest.raises(ConflictError, match="未结算或待核对"):
            runtime._require_team_billing_clear_for_send(old_snapshot, {"team": team})
