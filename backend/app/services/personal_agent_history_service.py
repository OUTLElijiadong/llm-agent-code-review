"""Read-only, account-scoped summaries for recovering personal Agent runs."""

from app.models.billing_ledger import BillingReservation
from app.models.enterprise_asset import EnterpriseAssetPublication
from app.models.personal_agent import PersonalAgentPublication, PersonalAgentRun, PersonalAgentVersion


def list_runs(db, *, owner_id: int, source: str, before_id: int | None, limit: int):
    run = PersonalAgentRun
    query = (
        db.query(
            run.id,
            run.agent_id,
            run.publication_id,
            run.enterprise_publication_id,
            run.version_number,
            run.status,
            run.result_reason,
            run.create_time,
            PersonalAgentVersion.title.label("private_title"),
            PersonalAgentPublication.title.label("public_title"),
            EnterpriseAssetPublication.title.label("enterprise_public_title"),
            BillingReservation.status.label("billing_status"),
            BillingReservation.actual_milli,
        )
        .outerjoin(PersonalAgentVersion, PersonalAgentVersion.id == run.version_id)
        .outerjoin(PersonalAgentPublication, PersonalAgentPublication.id == run.publication_id)
        .outerjoin(EnterpriseAssetPublication, EnterpriseAssetPublication.id == run.enterprise_publication_id)
        .outerjoin(BillingReservation, BillingReservation.request_key == run.reservation_key)
        .filter(run.owner_id == owner_id)
    )
    if source == "private":
        query = query.filter(run.publication_id.is_(None), run.enterprise_publication_id.is_(None))
    elif source == "public":
        query = query.filter(run.publication_id.is_not(None))
    elif source == "enterprise_public":
        query = query.filter(run.enterprise_publication_id.is_not(None))
    else:
        raise ValueError("unknown history source")
    if before_id is not None:
        query = query.filter(run.id < before_id)
    rows = query.order_by(run.id.desc()).limit(limit).all()
    result = []
    for row in rows:
        status = (
            "result_unavailable"
            if row.status == "awaiting_reconciliation" and row.billing_status == "settled"
            else row.status
        )
        result.append(
            {
                "id": row.id,
                "agent_id": row.agent_id if row.publication_id is None else None,
                "publication_id": row.publication_id,
                "enterprise_publication_id": row.enterprise_publication_id,
                "version_number": row.version_number,
                "title": (row.enterprise_public_title if row.enterprise_publication_id is not None else
                          row.private_title if row.publication_id is None else row.public_title),
                "status": status,
                "actual_milli": row.actual_milli if row.billing_status == "settled" else None,
                "created_at": row.create_time,
            }
        )
    return result
