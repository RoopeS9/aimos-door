import base64
import copy
import json
from datetime import timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from door.main import create_app
from door.security import digest
from door.service import utcnow


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, fixture=True)) as client:
        yield client


def customer(client):
    return next(c for c in client.get("/demo/customers").json()["customers"] if c["id"] == "hotel")


def human_auth(client, customer_id="hotel"):
    response = client.post("/demo/buyer-session", json={"customer_id": customer_id})
    assert response.status_code == 200, response.text
    return {"X-Buyer-Session": response.json()["token"]}


def read_auth(client, customer_id="hotel"):
    row = next(row for row in client.get("/demo/customers").json()["customers"] if row["id"] == customer_id)
    return {"Authorization": f"Bearer {row['mandate']}", "X-Bot-Id": row["bot_id"]}


def signed_mandate(client, **limits):
    service = client.app.state.service
    original = customer(client)
    payload = copy.deepcopy(original["mandate_payload"])
    payload["jti"] += "-changed"
    payload["version"] += 1
    payload["limits"].update(limits)
    token = service.keys.sign_buyer("hotel", payload)
    assert client.post("/mandates", json={"mandate": token}, headers=human_auth(client)).status_code == 200
    return {"mandate": token, "bot_id": "reference-bot"}


def make_offer(client, auth=None):
    buyer = customer(client)
    auth = auth or {"mandate": buyer["mandate"], "bot_id": buyer["bot_id"]}
    need = {"product_type": "drinking milk", "properties": ["lactose-free"], "quantity": 1000, "unit": "L", "deadline": (utcnow() + timedelta(days=2)).date().isoformat()}
    match = client.post("/needs/match", json={**auth, "need": need})
    assert match.status_code == 200, match.text
    offer = client.post("/offers", json={**auth, "option": match.json()["options"][0]})
    assert offer.status_code == 200, offer.text
    return auth, offer.json()


def test_commit_retry_and_chain(client):
    service = client.app.state.service
    auth, offer = make_offer(client)
    assert service.reservations() == {}
    body = {**auth, "offer": offer["offer"]}
    first = client.post("/orders", json=body, headers={"Idempotency-Key": "one"}).json()
    assert first["status"] == "committed"
    assert len(first["checks"]) == 10 and all(c["passed"] for c in first["checks"])
    reserved = service.reservations()
    again = client.post("/orders", json=body, headers={"Idempotency-Key": "one"}).json()
    assert again == first and service.reservations() == reserved
    receipt = service.keys.verify_door(first["receipt"], "receipt")
    record = client.get(f"/records/{first['record_id']}").json()
    saved_hash = record.pop("hash")
    assert digest(record) == saved_hash == receipt["record_hash"]
    second = client.post("/orders", json=body, headers={"Idempotency-Key": "two"}).json()
    assert client.get(f"/records/{second['record_id']}").json()["previous_hash"] == saved_hash


def test_all_checks_and_invalid_authority_no_reservation(client):
    auth = signed_mandate(client, per_order_cap=1, allowed_categories=["not-milk"])
    auth, offer = make_offer(client, auth)
    held = client.post("/orders", json={**auth, "offer": offer["offer"]}, headers={"Idempotency-Key": "held"}).json()
    assert held["status"] == "requires_escalation"
    assert {c["id"] for c in held["checks"] if not c["passed"]} >= {3, 6}
    assert len(held["checks"]) == 10
    reserved = client.app.state.service.reservations()
    bad = client.post("/orders", json={**auth, "bot_id": "wrong-bot", "offer": offer["offer"]}, headers={"Idempotency-Key": "bad"})
    assert bad.status_code == 403
    assert len(bad.json()["detail"]["checks"]) == 10
    assert client.app.state.service.reservations() == reserved


def test_offer_tampering_never_uses_altered_price(client):
    auth, offer = make_offer(client)
    header, encoded, signature = offer["offer"].split(".")
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    original = payload["total"]
    payload["total"] = .01
    altered = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    token = ".".join((header, altered, signature))
    order = client.post("/orders", json={**auth, "offer": token}, headers={"Idempotency-Key": "tampered"}).json()
    assert order["status"] == "requires_escalation" and not order["checks"][1]["passed"]
    assert order["total"] == original
    assert client.app.state.service.reservations() == {}


def test_buyer_preferences_and_server_option_prices_cannot_be_overridden(client):
    auth = signed_mandate(client, soft_preferences={"required": {"brand": "Only This Brand"}})
    need = {"product_type": "drinking milk", "properties": [], "quantity": 10, "unit": "L", "deadline": (utcnow() + timedelta(days=2)).date().isoformat(), "preferences": {"required": {"brand": "Other"}}}
    result = client.post("/needs/match", json={**auth, "need": need}).json()
    assert result["need"]["preferences"]["required"]["brand"] == "Only This Brand"
    option = result["options"][0]
    original = option["total"]
    option["total"] = .01
    option["within_preferences"] = True
    offer = client.post("/offers", json={**auth, "option": option}).json()
    assert offer["payload"]["total"] == original
    order = client.post("/orders", json={**auth, "offer": offer["offer"]}, headers={"Idempotency-Key": "preferences"}).json()
    assert not order["checks"][7]["passed"]


def test_invalid_mandate_cannot_disclose_prices(client):
    assert client.post("/needs/match", json={}).status_code == 401
    assert client.post("/offers", json={}).status_code == 401
    assert client.post("/mandates", json={"mandate": "bad"}).status_code == 403


def test_signoff_cannot_approve_another_buyers_offer(client):
    _, offer = make_offer(client)
    other = next(c for c in client.get("/demo/customers").json()["customers"] if c["id"] == "restaurant")
    response = client.post("/orders", json={"mandate": other["mandate"], "bot_id": other["bot_id"], "offer": offer["offer"]}, headers={"Idempotency-Key": "cross-buyer"})
    held = response.json()
    assert held["status"] == "requires_escalation" and not held["checks"][1]["passed"]
    assert client.app.state.service.reservations() == {}
    assert client.post(f"/orders/{held['id']}/signoff", json={"action": "approve"}, headers=human_auth(client, "restaurant")).status_code == 409
    assert client.get(f"/orders/{held['id']}", headers=human_auth(client, "restaurant")).json()["status"] == "requires_escalation"


def test_later_delivery_is_not_mislabelled_as_short_stock(client):
    service = client.app.state.service
    payload = {
        "kind": "single_product", "stock_shortfall": 0,
        "need": {"deadline": "2026-11-16"},
        "lines": [{"id": "milk", "name": "Fully stocked milk", "packs": 2, "available": 10,
                   "delivery_date": "2026-11-17", "on_time": False, "stock_shortfall": 0}],
    }
    passed, reason = service._delivery_check(payload)
    assert not passed
    assert "2026-11-17" in reason and "stock is short" not in reason.lower()
    payload["kind"] = "split_delivery"
    passed, reason = service._delivery_check(payload)
    assert not passed
    assert reason.lower().index("stock is short") < reason.index("2026-11-17")
