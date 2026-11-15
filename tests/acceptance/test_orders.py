from __future__ import annotations

import base64
import copy
from datetime import datetime, timedelta, timezone
import json
import uuid

import jwt

from conftest import auth, buyer, fixture_offer, human_auth, matching, need, new_mandate, offer, order, verify


def test_C01(client):
    service = client.app.state.service
    before = service.reservations()
    document = offer(client)
    payload = verify(document["offer"], client.get("/.well-known/jwks.json").json())
    assert payload["kind"] == "offer" and payload["total"] > 0 and payload["exp"] > datetime.now(timezone.utc).timestamp()
    assert payload["delivery_date"] and payload["lines"]
    for line in payload["lines"]:
        assert line["unit_price"] > 0 and line["packs"] > 0 and line["quantity"] > 0
        assert line["available"] >= 0 and line["delivery_date"]
    assert service.reservations() == before and payload["reserves_stock"] is False


def test_C02(client):
    result = order(client)
    assert result["status"] == "committed" and result["tier"] == "Commit"
    assert len(result["checks"]) == 10 and all(check["passed"] for check in result["checks"])
    receipt = verify(result["receipt"], client.get("/.well-known/jwks.json").json())
    record = client.get(f"/records/{result['record_id']}").json()
    assert receipt["kind"] == "receipt" and receipt["order_id"] == result["id"]
    assert receipt["record_hash"] == record["hash"] == result["record_hash"]


def test_C03(client):
    authority = new_mandate(client, allowed_categories=["out-of-scope"], per_order_cap=1)
    result = order(client, authority)
    record = client.get(f"/records/{result['record_id']}").json()
    assert [check["id"] for check in record["checks"]] == list(range(1, 11))
    assert all(isinstance(check["passed"], bool) and check["reason"] for check in record["checks"])
    assert {check["id"] for check in record["checks"] if not check["passed"]} >= {3, 6}
    document = offer(client)
    response = client.post("/orders", json={"mandate": "broken", "bot_id": "reference-bot", "offer": document["offer"]}, headers={"Idempotency-Key": "early-failure"})
    assert response.status_code in (401, 403)
    invalid_record = client.get(f"/records/{response.json()['detail']['record_id']}").json()
    assert [check["id"] for check in invalid_record["checks"]] == list(range(1, 11))
    assert not invalid_record["checks"][0]["passed"]


def test_C04(client):
    result = order(client, new_mandate(client, per_order_cap=1))
    assert result["status"] == "requires_escalation" and result["tier"] == "Hold"
    assert result["continue_url"] and "signoff" in result["continue_url"]
    assert not result["checks"][5]["passed"] and result["receipt"] is None
    assert client.get(f"/orders/{result['id']}", headers=human_auth(client)).json()["status"] != "committed"


def test_C05(client):
    authority = new_mandate(client, monthly_budget=1500)
    first = order(client, authority)
    second = order(client, authority)
    assert first["status"] == "committed" and first["total"] < 1500
    assert second["status"] == "requires_escalation" and not second["checks"][6]["passed"]
    assert second["receipt"] is None and first["total"] + second["total"] > 1500


def test_C06(client):
    non_food = need(
        product_type="other", product_id="cleaning-spray", product_ids=["cleaning-spray"],
        properties=[], quantity=1, unit="PACK",
    )
    result = order(client, request_need=non_food)
    assert result["status"] == "requires_escalation" and not result["checks"][2]["passed"]
    assert "outside" in result["checks"][2]["reason"].lower() and result["receipt"] is None
    assert result["checks"][3]["passed"], "Non-food must not inherit the dairy-only property requirement."


def test_C07(client):
    authority, document = auth(client), offer(client)
    first = order(client, authority, document, key="retry-exactly-once")
    reservations = client.app.state.service.reservations()
    second = order(client, authority, document, key="retry-exactly-once")
    assert first["receipt"] and first == second
    assert client.app.state.service.reservations() == reservations
    with client.app.state.service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM reservations WHERE active=1").fetchone()[0] == len(first["offer_payload"]["lines"])
    for line in first["offer_payload"]["lines"]:
        assert reservations[line["id"]] == line["packs"]


def test_C08(client):
    service, customer, document = client.app.state.service, buyer(client), offer(client)
    expired = copy.deepcopy(customer["mandate_payload"])
    expired["exp"] = int((datetime.now(timezone.utc) - timedelta(seconds=5)).timestamp())
    tokens = [(service.keys.sign_buyer("hotel", expired), "reference-bot"), (customer["mandate"], "another-bot"), (customer["mandate"][:-12] + "AAAAAAAAAAAA", "reference-bot")]
    for index, (token, bot_id) in enumerate(tokens):
        before = service.reservations()
        response = client.post("/orders", json={"mandate": token, "bot_id": bot_id, "offer": document["offer"]}, headers={"Idempotency-Key": f"bad-{index}"})
        assert response.status_code in (401, 403)
        assert service.reservations() == before == {}
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_C09(client):
    document = offer(client)
    header, encoded, signature = document["offer"].split(".")
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    original = payload["total"]
    payload["total"] = .01
    payload["lines"][0]["unit_price"] = .0001
    forged = ".".join((header, base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode(), signature))
    result = order(client, document={"offer": forged})
    assert not result["checks"][1]["passed"] and result["status"] != "committed"
    assert result["total"] == original and result["receipt"] is None
    assert client.app.state.service.reservations() == {}


def test_C10(client):
    request_need = need(quantity=17.5, product_ids=["milk-175"])
    before = matching(client, request_need=request_need)["options"][0]["lines"][0]["available"]
    committed = order(client, request_need=request_need)
    packs = committed["offer_payload"]["lines"][0]["packs"]
    assert committed["status"] == "committed"
    after = matching(client, request_need=request_need)["options"][0]["lines"][0]["available"]
    assert after == before - packs
    assert client.post(f"/orders/{committed['id']}/undo", headers=human_auth(client)).status_code == 200
    assert matching(client, request_need=request_need)["options"][0]["lines"][0]["available"] == before
    held = order(client, new_mandate(client, per_order_cap=1), request_need=request_need)
    assert held["status"] == "requires_escalation"
    assert matching(client, request_need=request_need)["options"][0]["lines"][0]["available"] == before - packs
    rejected = client.post(f"/orders/{held['id']}/signoff", json={"action": "reject"}, headers=human_auth(client))
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert matching(client, request_need=request_need)["options"][0]["lines"][0]["available"] == before


def test_C11(client):
    authority = new_mandate(client, per_order_cap=100)
    held = order(client, authority)
    approved = client.post(f"/orders/{held['id']}/signoff", json={"action": "approve"}, headers=human_auth(client))
    assert approved.status_code == 200 and approved.json()["status"] == "committed"
    verify(approved.json()["receipt"], client.get("/.well-known/jwks.json").json())
    rejected_hold = order(client, authority)
    reserved_before = client.app.state.service.reservations()
    rejected = client.post(f"/orders/{rejected_hold['id']}/signoff", json={"action": "reject"}, headers=human_auth(client))
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert sum(client.app.state.service.reservations().values()) < sum(reserved_before.values())
    editable = order(client, authority)
    edited = client.post(f"/orders/{editable['id']}/signoff", json={"action": "edit", "quantity": 10}, headers=human_auth(client))
    assert edited.status_code == 200, "A buyer must be able to edit a held quantity."
    result = edited.json()
    assert len(result["checks"]) == 10 and result["checks"][5]["passed"]
    assert result["total"] < editable["total"] and result["offer_payload"]["need"]["quantity"] == 10


def test_C12(client):
    authority = new_mandate(client, per_order_cap=1)
    held = order(client, authority)
    service = client.app.state.service
    payload = copy.deepcopy(held["offer_payload"])
    payload["exp"] = int((datetime.now(timezone.utc) - timedelta(seconds=1)).timestamp())
    expired = service.keys.sign_door(payload)
    with service.store.transaction() as db:
        service.store.put(db, "offer", payload["jti"], {"offer": expired, "payload": payload})
        stored = service.store.order(db, held["id"])
        stored["offer"], stored["offer_payload"] = expired, payload
        service.store.save_order(db, stored)
    pid = payload["lines"][0]["id"]
    service.valio.fixture_prices[pid] = {"unit_price": payload["lines"][0]["unit_price"] * 1.1, "comparison_price": 1.32, "comparison_unit": "L", "error": False, "source": "synthetic fixture"}
    first = client.post(f"/orders/{held['id']}/signoff", json={"action": "approve"}, headers=human_auth(client))
    assert first.status_code == 409, "Expired prices must not be approved invisibly."
    refreshed = client.get(f"/orders/{held['id']}", headers=human_auth(client)).json()
    assert refreshed["offer"] != expired, "The held order must expose its fresh offer before approval."
    fresh_payload = verify(refreshed["offer"], client.get("/.well-known/jwks.json").json())
    assert fresh_payload["total"] != payload["total"]
    assert refreshed["status"] == "requires_escalation" and refreshed["receipt"] is None
    approved = client.post(f"/orders/{held['id']}/signoff", json={"action": "approve", "offer": refreshed["offer"]}, headers=human_auth(client))
    assert approved.status_code == 200 and approved.json()["status"] == "committed"
    assert approved.json()["total"] == fresh_payload["total"]


def test_C13(client):
    customer = buyer(client)
    original = copy.deepcopy(customer["mandate_payload"])
    proposed = copy.deepcopy(original)
    proposed["jti"] = str(uuid.uuid4())
    proposed["version"] += 1
    proposed["limits"]["per_order_cap"] += 500
    door_signed = client.app.state.service.keys.sign_door(proposed)
    assert client.post("/mandates", json={"mandate": door_signed}, headers=human_auth(client)).status_code == 403
    unsigned = client.post("/mandates", json={"payload": proposed}, headers=human_auth(client))
    assert unsigned.status_code in (401, 403, 422)
    assert client.get(f"/mandates/{original['jti']}").json()["payload"] == original
    same_version = copy.deepcopy(proposed)
    same_version["jti"] = original["jti"]
    same_token = client.app.state.service.keys.sign_buyer("hotel", same_version)
    assert client.post("/mandates", json={"mandate": same_token}, headers=human_auth(client)).status_code == 409
    buyer_signed = client.app.state.service.keys.sign_buyer("hotel", proposed)
    response = client.post("/mandates", json={"mandate": buyer_signed}, headers=human_auth(client))
    assert response.status_code == 200
    verified = verify(response.json()["mandate"], client.get("/demo/buyer-jwks").json())
    assert verified == proposed
    assert client.get(f"/mandates/{original['jti']}").json()["payload"] == original
    reset = client.post("/limits/per_order_cap/reset", json={"customer_id": "hotel", "signed_ceiling": 100000}, headers=human_auth(client))
    assert reset.status_code in (200, 400, 403, 409, 422)
    assert client.get(f"/mandates/{original['jti']}").json()["payload"] == original
