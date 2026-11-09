from datetime import datetime, timezone

import pytest

from app.api.v1 import billing
from app.api.v1.billing import enterprise_usage, personal_usage, pricing
from app.core.exceptions import ForbiddenError
from app.models.billing_ledger import BillingReservation, BillingReservationPart, BillingWallet
from app.models.enterprise_org import Enterprise, EnterpriseGroup, EnterpriseGroupMember, EnterpriseMember
from app.models.user import User
from app.services.billing_ledger_service import RATE_CATALOG_VERSION, public_rate_catalog


def test_pricing_catalog_matches_current_deepseek_model_routing(db):
    user = User(id=1, username="pricing", password="x", role="user", status=1)
    catalogue = pricing(db=db, user=user).data
    assert catalogue["pricing_version"] == RATE_CATALOG_VERSION
    assert catalogue["model_milli_per_token"] == public_rate_catalog(db)
    assert catalogue["model_milli_per_token"]["deepseek-flash"] == {"input": 1, "output": 4}
    assert catalogue["model_milli_per_token"]["deepseek-v4-flash"] == {"input": 1, "output": 4}
    assert catalogue["model_milli_per_token"]["deepseek-v4-flash-vision-exp"] == {
        "input": 1, "output": 4,
    }
    assert "deepseek-v4-pro" not in catalogue["model_milli_per_token"]
    assert catalogue["billable_models"] == (
        "deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp",
    )
    assert catalogue["channels"] == {"alipay": False, "wechat": False}
    assert catalogue["sales_enabled"] is False
    assert catalogue["credit_pack"] == {"credits": 1000, "price_fen": 400}
    assert catalogue["pricing_effective_at"] == datetime(1970, 1, 1)
    assert catalogue["billing_unit"] == {
        "unit": "milli_credit", "milli_per_credit": 1000,
        "rate_unit": "milli_credit_per_token",
        "rounding": "ceil_per_model_request",
    }


def test_pricing_contract_exposes_each_channel_while_preserving_aggregate(db, monkeypatch):
    states = {"alipay": False, "wechat": True}
    monkeypatch.setattr(billing, "_sales_ready", lambda channel: states[channel])
    user = User(id=1, username="pricing-channels", password="x", role="user", status=1)

    catalogue = pricing(db=db, user=user).data

    assert catalogue["channels"] == {"alipay": False, "wechat": True}
    assert catalogue["sales_enabled"] is True
    states.update(alipay=False, wechat=False)
    catalogue = pricing(db=db, user=user).data
    assert catalogue["channels"] == {"alipay": False, "wechat": False}
    assert catalogue["sales_enabled"] is False


def test_personal_trend_counts_own_personal_and_enterprise_calls_without_other_users(db):
    user = User(username="usage-user", password="x", role="user", status=1)
    db.add(user)
    db.flush()
    common = {"model_name": "deepseek-flash", "requested_milli": 100,
              "created_at": datetime.now(timezone.utc).replace(tzinfo=None)}
    db.add_all([
        BillingReservation(request_key="mine-ok", user_id=user.id, usage_kind="member", status="settled",
                           usage_state="reported", input_tokens=10, output_tokens=5, actual_milli=30, **common),
        BillingReservation(request_key="mine-unknown", user_id=user.id, usage_kind="member", status="unknown",
                           usage_state="unknown", server_tool_search_calls=2, server_tool_fetch_calls=1, **common),
        BillingReservation(request_key="other", user_id=user.id+1, usage_kind="member", status="settled",
                           usage_state="reported", input_tokens=100, output_tokens=100, actual_milli=500, **common),
        BillingReservation(request_key="company", user_id=user.id, enterprise_id=33, usage_kind="member",
                           status="settled", usage_state="reported", input_tokens=100, output_tokens=100,
                           actual_milli=500, server_tool_search_calls=3, server_tool_fetch_calls=2, **common),
    ])
    db.commit()
    result = personal_usage(days=1, db=db, user=user).data
    assert result["timezone"] == "UTC"
    assert result["items"][0]["input_tokens"] == 110
    assert result["items"][0]["output_tokens"] == 105
    assert result["items"][0]["unknown_calls"] == 1
    assert result["items"][0]["charged_milli"] == 530
    assert (result["items"][0]["server_tool_search_calls"],
            result["items"][0]["server_tool_fetch_calls"],
            result["items"][0]["server_tool_usage_reported_calls"]) == (5, 3, 2)


def test_personal_usage_separates_billable_tools_from_model_tokens_and_legacy_tool_counts(db):
    user = User(username="tool-usage-user", password="x", role="user", status=1)
    db.add(user)
    db.flush()
    created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    provider = {
        "provider_identity_version": "deepseek-official-v1",
        "provider_name": "deepseek",
        "provider_endpoint_host": "api.deepseek.com",
        "credential_source": "system",
    }
    common = {
        "enterprise_id": 33,
        "group_id": 4,
        "user_id": user.id,
        "usage_kind": "member",
        "requested_milli": 1_800,
        "created_at": created_at,
        "tool_cost_cny_micros_per_success": 5_038,
        "tool_price_milli_per_success": 1_800,
        "pricing_version": "tool-catalog-v1",
        "provider_attempt_started_at": created_at,
        **provider,
    }
    db.add_all([
        BillingReservation(request_key="tool-usage-search-paid", model_name="deepseek-search",
                           billable_kind="search", tool_name="search", status="settled",
                           usage_state="reported", tool_successful_calls=1, actual_milli=1_800, **common),
        BillingReservation(request_key="tool-usage-search-reconciled-zero", model_name="deepseek-search",
                           billable_kind="search", tool_name="search", status="settled",
                           usage_state="reported", tool_successful_calls=0, actual_milli=0, **common),
        BillingReservation(request_key="tool-usage-fetch-unknown", model_name="deepseek-fetch",
                           billable_kind="fetch", tool_name="fetch", status="unknown",
                           usage_state="unknown", tool_successful_calls=None, **common),
        BillingReservation(request_key="tool-usage-search-pending", model_name="deepseek-search",
                           billable_kind="search", tool_name="search", status="reserved",
                           usage_state="pending", tool_successful_calls=None, **common),
        BillingReservation(request_key="tool-usage-fetch-nonbillable", model_name="deepseek-fetch",
                           billable_kind="fetch", tool_name="fetch", status="released",
                           usage_state="not_billable", tool_successful_calls=0, actual_milli=0, **common),
        BillingReservation(request_key="tool-usage-model", model_name="deepseek-flash", user_id=user.id,
                           enterprise_id=33, group_id=4, usage_kind="member", requested_milli=1_800,
                           status="settled", usage_state="reported", input_tokens=12, output_tokens=3,
                           actual_milli=24, server_tool_search_calls=2, server_tool_fetch_calls=1,
                           created_at=created_at),
    ])
    db.commit()

    day = personal_usage(days=1, db=db, user=user).data["items"][0]

    assert (day["input_tokens"], day["output_tokens"], day["reported_calls"], day["charged_milli"]) == (
        12, 3, 1, 24,
    )
    assert (day["server_tool_search_calls"], day["server_tool_fetch_calls"],
            day["server_tool_usage_reported_calls"]) == (2, 1, 1)
    assert (day["search_success_calls"], day["search_charged_milli"],
            day["search_unknown_calls"], day["search_pending_calls"],
            day["search_unknown_milli"], day["search_pending_milli"],
            day["search_not_billable_calls"]) == (1, 1_800, 0, 1, 0, 1_800, 1)
    assert (day["fetch_success_calls"], day["fetch_charged_milli"],
            day["fetch_unknown_calls"], day["fetch_unknown_milli"],
            day["fetch_pending_calls"], day["fetch_not_billable_calls"]) == (0, 0, 1, 1_800, 0, 1)


def test_enterprise_usage_requires_company_admin_or_group_lead(db):
    owner = User(username="usage-owner", password="x", role="user", status=1)
    lead = User(username="usage-lead", password="x", role="user", status=1)
    employee = User(username="usage-employee", password="x", role="user", status=1)
    outsider = User(username="usage-outside", password="x", role="admin", status=1)
    db.add_all([owner, lead, employee, outsider])
    db.flush()
    enterprise = Enterprise(name="usage-co", owner_id=owner.id, seat_limit=2, paid_order_id="usage-order")
    db.add(enterprise)
    db.flush()
    group = EnterpriseGroup(enterprise_id=enterprise.id, name="usage-group")
    db.add(group)
    db.flush()
    other_group = EnterpriseGroup(enterprise_id=enterprise.id, name="other-usage-group")
    db.add(other_group)
    db.flush()
    db.add_all([EnterpriseMember(enterprise_id=enterprise.id, user_id=owner.id, role="owner"),
                EnterpriseMember(enterprise_id=enterprise.id, user_id=lead.id, role="member"),
                EnterpriseMember(enterprise_id=enterprise.id, user_id=employee.id, role="member"),
                EnterpriseGroupMember(enterprise_id=enterprise.id, group_id=group.id, user_id=lead.id, role="leader"),
                EnterpriseGroupMember(enterprise_id=enterprise.id, group_id=group.id, user_id=employee.id,
                                      role="member")])
    db.flush()
    db.add(BillingReservation(request_key="enterprise-usage", enterprise_id=enterprise.id, group_id=group.id,
                              user_id=lead.id, usage_kind="member", model_name="deepseek-flash",
                              requested_milli=100, status="settled", usage_state="reported",
                              input_tokens=20, output_tokens=10, actual_milli=60,
                              server_tool_search_calls=1, server_tool_fetch_calls=2))
    db.add_all([
        BillingReservation(request_key="other-group-usage", enterprise_id=enterprise.id, group_id=other_group.id,
                           user_id=lead.id, usage_kind="member", model_name="deepseek-flash", requested_milli=100,
                           status="settled", usage_state="reported", input_tokens=900, output_tokens=90,
                           actual_milli=2700, server_tool_search_calls=9, server_tool_fetch_calls=8),
        BillingReservation(request_key="other-member-usage", enterprise_id=enterprise.id, group_id=group.id,
                           user_id=employee.id, usage_kind="member", model_name="deepseek-flash", requested_milli=100,
                           status="settled", usage_state="reported", input_tokens=30, output_tokens=15,
                           actual_milli=90, server_tool_search_calls=4, server_tool_fetch_calls=5),
        BillingReservation(request_key="other-enterprise-usage", enterprise_id=enterprise.id + 100,
                           group_id=group.id, user_id=lead.id, usage_kind="member", model_name="deepseek-flash",
                           requested_milli=100, status="settled", usage_state="reported", input_tokens=700,
                           output_tokens=70, actual_milli=2100, server_tool_search_calls=7,
                           server_tool_fetch_calls=6),
    ])
    db.commit()
    company_item = enterprise_usage(
        enterprise.id, days=1, group_id=None, member_id=None, db=db, user=owner
    ).data["items"][0]
    group_item = enterprise_usage(
        enterprise.id, days=1, group_id=group.id, member_id=None, db=db, user=lead
    ).data["items"][0]
    member_item = enterprise_usage(
        enterprise.id, days=1, group_id=group.id, member_id=lead.id, db=db, user=lead
    ).data["items"][0]
    assert company_item["input_tokens"] == 950
    assert company_item["server_tool_search_calls"] == 14
    assert group_item["input_tokens"] == 50
    assert (group_item["server_tool_search_calls"], group_item["server_tool_fetch_calls"],
            group_item["server_tool_usage_reported_calls"]) == (5, 7, 2)
    assert member_item["input_tokens"] == 20
    assert (member_item["server_tool_search_calls"], member_item["server_tool_fetch_calls"],
            member_item["server_tool_usage_reported_calls"]) == (1, 2, 1)
    with pytest.raises(ForbiddenError):
        enterprise_usage(enterprise.id, days=1, group_id=None, member_id=None, db=db, user=lead)
    with pytest.raises(ForbiddenError):
        enterprise_usage(enterprise.id, days=1, group_id=group.id, member_id=None, db=db, user=outsider)


def test_usage_splits_mixed_wallet_payments_without_counting_tokens_twice(db):
    owner = User(username="split-owner", password="x", role="user", status=1)
    member = User(username="split-member", password="x", role="user", status=1)
    db.add_all([owner, member])
    db.flush()
    enterprise = Enterprise(name="split-co", owner_id=owner.id, seat_limit=2, paid_order_id="split-order")
    db.add(enterprise)
    db.flush()
    db.add_all([EnterpriseMember(enterprise_id=enterprise.id, user_id=owner.id, role="owner"),
                EnterpriseMember(enterprise_id=enterprise.id, user_id=member.id, role="member")])
    company_wallet = BillingWallet(owner_type="enterprise", owner_id=enterprise.id)
    personal_wallet = BillingWallet(owner_type="user", owner_id=member.id)
    db.add_all([company_wallet, personal_wallet])
    db.flush()
    company_call = BillingReservation(request_key="split-company", model_name="deepseek-flash",
                                      enterprise_id=enterprise.id, user_id=member.id, usage_kind="member",
                                      requested_milli=100, actual_milli=70, status="settled", usage_state="reported",
                                      input_tokens=50, output_tokens=5,
                                      server_tool_search_calls=1, server_tool_fetch_calls=2)
    own_call = BillingReservation(request_key="split-own", model_name="deepseek-flash", user_id=member.id,
                                  usage_kind="member", requested_milli=40, actual_milli=30, status="settled",
                                  usage_state="reported", input_tokens=10, output_tokens=5)
    pending_call = BillingReservation(request_key="split-pending", model_name="deepseek-flash",
                                      enterprise_id=enterprise.id, user_id=member.id, usage_kind="member",
                                      requested_milli=10, status="unknown", usage_state="unknown",
                                      server_tool_search_calls=2, server_tool_fetch_calls=0)
    db.add_all([company_call, own_call, pending_call])
    db.flush()
    db.add_all([
        BillingReservationPart(reservation_id=company_call.id, wallet_id=company_wallet.id, batch_id=1,
                               reserved_milli=40, charged_milli=40),
        BillingReservationPart(reservation_id=company_call.id, wallet_id=personal_wallet.id, batch_id=2,
                               reserved_milli=30, charged_milli=30),
        BillingReservationPart(reservation_id=own_call.id, wallet_id=personal_wallet.id, batch_id=3,
                               reserved_milli=40, charged_milli=30),
        BillingReservationPart(reservation_id=pending_call.id, wallet_id=personal_wallet.id, batch_id=4,
                               reserved_milli=10, charged_milli=None),
    ])
    db.commit()
    company = enterprise_usage(enterprise.id, days=1, group_id=None, member_id=None, db=db, user=owner).data["items"][0]
    assert (company["input_tokens"], company["output_tokens"], company["reported_calls"]) == (50, 5, 1)
    assert (company["charged_milli"], company["enterprise_wallet_charged_milli"],
            company["personal_wallet_charged_milli"], company["unknown_calls"]) == (70, 40, 30, 1)
    assert (company["server_tool_search_calls"], company["server_tool_fetch_calls"],
            company["server_tool_usage_reported_calls"]) == (3, 2, 2)
    own = personal_usage(days=1, db=db, user=member).data["items"][0]
    assert (own["input_tokens"], own["output_tokens"], own["reported_calls"]) == (60, 10, 2)
    assert (own["charged_milli"], own["personal_wallet_charged_milli"],
            own["enterprise_wallet_charged_milli"], own["enterprise_personal_fallback_milli"],
            own["personal_wallet_total_milli"]) == (100, 60, 40, 30, 60)
    assert (own["server_tool_search_calls"], own["server_tool_fetch_calls"],
            own["server_tool_usage_reported_calls"]) == (3, 2, 2)
