"""Provider-safe new IDs and replay of already persisted historical IDs."""

import re
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.models.user import User
from app.services import billing_checkout_service as checkout


@pytest.mark.parametrize("channel", ["alipay", "wechat"])
def test_new_order_identifier_is_compatible_with_native_wechat_contract(db, channel):
    buyer = User(username=f"buyer-{channel}", password="x", role="user", status=1)
    db.add(buyer)
    db.flush()
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel=channel)
    assert re.fullmatch(r"[A-Za-z0-9_|*\-]{6,32}", order.order_no)


@pytest.mark.parametrize("channel", ["alipay", "wechat"])
def test_old_long_order_keeps_identity_on_idempotent_replay_and_lookup(db, channel):
    buyer = User(username=f"old-buyer-{channel}", password="x", role="user", status=1)
    db.add(buyer)
    db.flush()
    args = dict(user_id=buyer.id, product="reviewer_month", quantity=1, channel=channel,
                client_request_id=f"old-request-{channel}")
    order = checkout.create_order(db, **args)
    old_no = "pr" + "a" * 32
    order.order_no = old_no
    db.commit()
    replay = checkout.create_order(db, **args)
    assert replay.id == order.id and replay.order_no == old_no
    assert checkout._order(db, old_no, channel).id == order.id


def test_parallel_new_order_identifiers_are_unique_and_provider_compatible():
    with ThreadPoolExecutor(max_workers=12) as pool:
        identifiers = list(pool.map(lambda _: checkout._new_order_number(), range(4096)))
    assert len(set(identifiers)) == len(identifiers)
    assert all(re.fullmatch(r"[A-Za-z0-9_|*\-]{6,32}", value) for value in identifiers)
