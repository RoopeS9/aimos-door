"""Adverse cases around human learning, revocation and reservation replacement."""
import copy
from datetime import timedelta
import uuid

from test_service import client, customer, human_auth, make_offer, signed_mandate
from door.service import utcnow


def submit(client, auth, offer):
    return client.post("/orders", json={**auth, "offer": offer["offer"]}, headers={"Idempotency-Key": str(uuid.uuid4())})


def held_preference(client):
    auth = signed_mandate(client, soft_preferences={"required": {"brand": "Buyer favorite"}})
    auth, offer = make_offer(client, auth)
    held = submit(client, auth, offer).json()
    assert held["status"] == "requires_escalation"
    return auth, offer, held


def confirm_standing(client, held, scope="always"):
    path = f"/orders/{held['id']}/signoff"
    prepared = client.post(path, json={"action": "prepare_answer", "answer": "approve", "scope": scope}).json()
    pending = prepared["pending_answer"]
    response = client.post(path, json={"action": "approve", **pending, "confirmed": True}, headers=human_auth(client))
    assert response.status_code == 200, response.text
    return response.json()


def test_removed_preference_revokes_old_authority(client):
    _, _, held = held_preference(client)
    confirm_standing(client, held)
    prior = customer(client)
    _, document = make_offer(client)
    entry = prior["mandate_payload"]["learned_preferences"][0]
    removed = client.post(f"/limits/{entry['id']}/reset", json={"customer_id": "hotel", "confirmed": True}, headers=human_auth(client))
    assert removed.status_code == 200
    old = {"mandate": prior["mandate"], "bot_id": prior["bot_id"]}
    assert submit(client, old, document).status_code == 403
    assert client.post("/needs/match", json={**old, "need": held["offer_payload"]["need"]}).status_code == 403
    assert client.post("/offers", json={**old, "option": document["payload"]}).status_code == 403


def test_missing_claim_is_exposed_even_when_a_preference_also_fails(client):
    auth = signed_mandate(client, soft_preferences={"required": {"brand": "Buyer favorite"}})
    need = {"product_type": "drinking milk", "product_ids": ["milk-unknown"], "properties": ["lactose-free"], "quantity": 10, "unit": "L", "deadline": (utcnow() + timedelta(days=2)).date().isoformat()}
    matched = client.post("/needs/match", json={**auth, "need": need}).json()
    document = client.post("/offers", json={**auth, "option": matched["options"][0]}).json()
    held = submit(client, auth, document).json()
    assert {check["id"] for check in held["checks"] if not check["passed"]} >= {4, 8}
    assert held["question_spec"]["kind"] == "property_confirmation"
    assert held["question_spec"]["missing_properties"][0]["product_id"] == "milk-unknown"


def test_old_hold_learning_preserves_newer_lower_ceiling(client):
    _, _, held = held_preference(client)
    path = f"/orders/{held['id']}/signoff"
    prepared = client.post(path, json={"action": "prepare_answer", "answer": "approve", "scope": "always"}).json()
    signed_mandate(client, per_order_cap=50)
    current = customer(client)["mandate_payload"]
    response = client.post(path, json={"action": "approve", **prepared["pending_answer"], "confirmed": True}, headers=human_auth(client))
    assert response.status_code == 200, response.text
    after = customer(client)["mandate_payload"]
    assert after["version"] > current["version"]
    assert after["limits"] == current["limits"]
    assert after["limits"]["per_order_cap"] == 50
    assert after["learned_preferences"]


def test_standing_summary_must_exist_and_match(client):
    _, _, held = held_preference(client)
    path = f"/orders/{held['id']}/signoff"
    before = customer(client)["mandate"]
    response = client.post(path, json={"action": "approve", "answer": "approve", "scope": "always", "confirmed": True}, headers=human_auth(client))
    assert response.status_code == 200 and response.json()["status"] == "requires_escalation"
    assert response.json()["pending_answer"]["confirmation_summary"]
    assert customer(client)["mandate"] == before
    wrong = client.post(path, json={"action": "approve", "answer": "approve", "scope": "always", "confirmed": True, "confirmation_summary": "Allow every product without restrictions"}, headers=human_auth(client))
    assert wrong.status_code == 409 and customer(client)["mandate"] == before


def test_always_ask_stays_held_and_rejection_releases_after_revocation(client):
    _, _, held = held_preference(client)
    confirm_standing(client, held, "always_ask")
    auth, document = make_offer(client)
    again = submit(client, auth, document).json()
    assert again["status"] == "requires_escalation" and not again["checks"][7]["passed"]
    signed_mandate(client, per_order_cap=25)
    assert client.post(f"/orders/{again['id']}/signoff", json={"action": "reject"}, headers=human_auth(client)).status_code == 200
    with client.app.state.service.store.connect() as db:
        assert db.execute("SELECT count(*) FROM reservations WHERE order_id=? AND active=1", (again["id"],)).fetchone()[0] == 0


def test_confirmation_cannot_override_explicit_contrary_manufacturer_data(client):
    auth, document = make_offer(client)
    service = client.app.state.service
    pid = document["payload"]["lines"][0]["id"]
    product = service.catalog.get(pid)
    product["contradicted_properties"] = {"lactose-free": "manufacturer"}
    service.catalog.upsert(product)
    held = submit(client, auth, document).json()
    assert not held["checks"][3]["passed"]
    response = client.post(f"/orders/{held['id']}/signoff", json={"action": "approve", "confirmed": True, "confirmations": [{"product_id": pid, "property": "lactose-free", "confirmed": True}]}, headers=human_auth(client))
    assert response.status_code == 409
    with service.store.connect() as db:
        assert service.store.list(db, "confirmation") == []


def test_same_inventory_pool_cannot_be_double_reserved_across_lines(client):
    auth, document = make_offer(client)
    service = client.app.state.service
    payload = copy.deepcopy(document["payload"])
    line = payload["lines"][0]
    line.update(packs=50, quantity=500, raw_available=60, available=60, reserved_at_match=0, total=600)
    payload.update(lines=[line, copy.deepcopy(line)], total=1200, jti=str(uuid.uuid4()))
    token = service.keys.sign_door(payload)
    with service.store.transaction() as db:
        service.store.put(db, "offer", payload["jti"], {"offer": token, "payload": payload})
    result = submit(client, auth, {"offer": token}).json()
    assert result["status"] == "requires_escalation"
    assert service.reservations()[line["id"]] == 60
    approved = client.post(f"/orders/{result['id']}/signoff", json={"action": "approve"}, headers=human_auth(client))
    assert approved.status_code == 409
    assert client.get(f"/orders/{result['id']}", headers=human_auth(client)).json()["status"] == "requires_escalation"


def test_quantity_edit_preserves_order_identity_and_replaces_reservation(client):
    auth = signed_mandate(client, per_order_cap=100)
    auth, document = make_offer(client, auth)
    original = client.post("/orders", json={**auth, "offer": document["offer"]}, headers={"Idempotency-Key": "edit-retry"}).json()
    edited = client.post(f"/orders/{original['id']}/signoff", json={"action": "edit", "quantity": 10}, headers=human_auth(client)).json()
    assert edited["id"] == original["id"] and edited["status"] == "committed"
    again = client.post("/orders", json={**auth, "offer": document["offer"]}, headers={"Idempotency-Key": "edit-retry"}).json()
    assert again == edited
    assert sum(client.app.state.service.reservations().values()) == sum(line["packs"] for line in edited["offer_payload"]["lines"])


def test_fresh_held_price_requires_exact_offer_acknowledgment(client):
    auth = signed_mandate(client, per_order_cap=1)
    auth, document = make_offer(client, auth)
    held = submit(client, auth, document).json()
    service = client.app.state.service
    expired_payload = copy.deepcopy(held["offer_payload"])
    expired_payload["exp"] = int((utcnow() - timedelta(seconds=1)).timestamp())
    expired = service.keys.sign_door(expired_payload)
    with service.store.transaction() as db:
        service.store.put(db, "offer", expired_payload["jti"], {"offer": expired, "payload": expired_payload})
        held.update(offer=expired, offer_payload=expired_payload)
        service.store.save_order(db, held)
    refreshed = client.get(f"/orders/{held['id']}", headers=human_auth(client)).json()
    assert refreshed["requires_price_confirmation"] and refreshed["offer"] != expired
    endpoint = f"/orders/{held['id']}/signoff"
    assert client.post(endpoint, json={"action": "approve"}, headers=human_auth(client)).status_code == 409
    assert client.post(endpoint, json={"action": "approve", "offer": expired}, headers=human_auth(client)).status_code == 409
    assert client.post(endpoint, json={"action": "approve", "offer": refreshed["offer"]}, headers=human_auth(client)).status_code == 200


def test_absent_upstream_cutoff_is_frozen_in_signed_offer(client):
    service = client.app.state.service
    for product_id in ("milk-10", "milk-12", "milk-175"):
        service.valio.fixture_availability[product_id] = {"available": 10000, "delivery_date": (utcnow() + timedelta(days=2)).date().isoformat(), "cutoff": None, "sales_block": False, "logistic_model": "Stock", "deliverable": True}
    auth, document = make_offer(client)
    frozen = document["payload"]["lines"][0]["cutoff"]
    assert frozen and "Simulated" in document["payload"]["lines"][0]["cutoff_source"]
    order = submit(client, auth, document).json()
    assert order["offer_payload"]["lines"][0]["cutoff"] == frozen
    assert order["reserved"][0]["cutoff"] == frozen


def test_edit_preserves_labelled_simulated_inventory_pools(client):
    buyer = customer(client)
    auth = {"mandate": buyer["mandate"], "bot_id": buyer["bot_id"]}
    need = {"product_type": "drinking milk", "properties": ["lactose-free"], "quantity": 1000, "unit": "L", "deadline": (utcnow() + timedelta(days=2)).date().isoformat(), "simulate_limited_stock": True}
    matched = client.post("/needs/match", json={**auth, "need": need}).json()
    document = client.post("/offers", json={**auth, "option": matched["options"][0]}).json()
    held = submit(client, auth, document).json()
    assert held["status"] == "requires_escalation"
    before = [(line["id"], line["delivery_date"], line["raw_available"]) for line in held["offer_payload"]["lines"] if line.get("simulated_stock")]
    assert before
    edited = client.post(f"/orders/{held['id']}/signoff", json={"action": "edit", "quantity": 1000}, headers=human_auth(client)).json()
    after = [(line["id"], line["delivery_date"], line["raw_available"]) for line in edited["offer_payload"]["lines"] if line.get("simulated_stock")]
    assert after == before
    assert edited["offer_payload"]["simulation"]
    approved = client.post(f"/orders/{held['id']}/signoff", json={"action": "approve"}, headers=human_auth(client))
    assert approved.status_code == 200 and approved.json()["status"] == "committed"
