import copy
import math
import uuid

from test_service import client, customer, human_auth


def restaurant(client):
    row = next(row for row in client.get("/demo/customers").json()["customers"] if row["id"] == "restaurant")
    return {"mandate": row["mandate"], "bot_id": row["bot_id"]}, row["mandate_payload"]


def basket_order(client, factor=1.2, omitted=None):
    usual = client.get("/usual-order?customer_id=restaurant", headers=human_auth(client, "restaurant")).json()
    authority, _ = restaurant(client)
    # Choose whole packs explicitly: rounding 1.2 packs up to two would be a
    # 100% departure for a one-pack staple rather than the intended 20%.
    requested = [{"product_id": row["id"], "quantity": math.floor(row["packs"] * factor), "unit": "PACK", "properties": row.get("required_properties", [])} for row in usual["lines"] if row["id"] != omitted]
    need = {"product_type": "usual basket", "anchor_id": usual["anchor_id"], "properties": [], "deadline": usual["deadline"], "lines": requested}
    match = client.post("/needs/match", json={**authority, "need": need})
    assert match.status_code == 200, match.text
    offer = client.post("/offers", json={**authority, "option": match.json()["options"][0]}).json()
    response = client.post("/orders", json={**authority, "offer": offer["offer"]}, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert response.status_code == 200, response.text
    return response.json()


def limits(client):
    return client.get("/limits?customer_id=restaurant", headers=human_auth(client, "restaurant")).json()


def departure(client):
    return next(row for row in limits(client)["rows"] if row["id"] == "departure")


def test_four_explicit_clean_approvals_propose_but_do_not_loosen(client):
    client.post("/limits/departure/reset", json={"customer_id": "restaurant"}, headers=human_auth(client, "restaurant"))
    assert not limits(client)["proposals"]
    for _ in range(4):
        held = basket_order(client)
        assert held["status"] == "requires_escalation"
        approved = client.post(f"/orders/{held['id']}/signoff", json={"action": "approve"}, headers=human_auth(client, "restaurant"))
        assert approved.status_code == 200, approved.text
    assert departure(client)["current_freedom"] == 0
    proposals = limits(client)["proposals"]
    assert len(proposals) == 1 and len(proposals[0]["evidence"]) == 4
    assert len({row["order_id"] for row in proposals[0]["evidence"]}) == 4


def test_proposal_requires_right_customer_explicit_tap_and_clamps_current_ceiling(client):
    proposal = limits(client)["proposals"][0]
    endpoint = f"/limits/proposals/{proposal['id']}/accept"
    assert client.post(endpoint, json={"customer_id": "restaurant"}, headers=human_auth(client, "restaurant")).status_code == 409
    assert client.post(endpoint, json={"customer_id": "hotel", "confirmed": True}, headers=human_auth(client, "restaurant")).status_code == 403
    authority, payload = restaurant(client)
    payload = copy.deepcopy(payload)
    payload.update(jti=str(uuid.uuid4()), version=payload["version"] + 1)
    payload["limits"]["departure_ceiling"] = .15
    token = client.app.state.service.keys.sign_buyer("restaurant", payload)
    assert client.post("/mandates", json={"mandate": token}, headers=human_auth(client, "restaurant")).status_code == 200
    assert client.post(endpoint, json={"customer_id": "restaurant", "confirmed": True, "proposed_freedom": 999}, headers=human_auth(client, "restaurant")).status_code == 200
    assert departure(client)["current_freedom"] == .15
    assert restaurant(client)[1] == payload


def test_edit_tightens_without_changing_anchor_or_signed_limits(client):
    before = client.get("/usual-order?customer_id=restaurant", headers=human_auth(client, "restaurant")).json()
    _, mandate = restaurant(client)
    held = basket_order(client, 1.4)
    response = client.post(f"/orders/{held['id']}/signoff", json={"action": "edit", "quantity": 1}, headers=human_auth(client, "restaurant"))
    assert response.status_code == 200, response.text
    assert departure(client)["current_freedom"] == 0
    assert "edit" in departure(client)["reason"].lower()
    assert client.get("/usual-order?customer_id=restaurant", headers=human_auth(client, "restaurant")).json()["lines"] == before["lines"]
    assert restaurant(client)[1]["limits"] == mandate["limits"]
    assert basket_order(client, 1)["status"] == "requires_escalation"


def test_missing_usual_line_is_a_full_departure(client):
    usual = client.get("/usual-order?customer_id=restaurant", headers=human_auth(client, "restaurant")).json()
    rice = next(line for line in usual["lines"] if line["product_type"] == "rice")
    result = basket_order(client, 1, omitted=rice["id"])
    assert result["status"] == "requires_escalation"
    assert not result["checks"][8]["passed"]
    assert "100.0%" in result["checks"][8]["reason"]


def test_basket_global_claim_stays_strict_and_stale_anchor_is_refused(client):
    usual = client.get("/usual-order?customer_id=restaurant", headers=human_auth(client, "restaurant")).json()
    authority, _ = restaurant(client)
    base = {"product_type": "usual basket", "deadline": usual["deadline"], "lines": [{"product_id": line["id"], "quantity": line["packs"], "unit": "PACK"} for line in usual["lines"]]}
    stale = client.post("/needs/match", json={**authority, "need": {**base, "anchor_id": "stale"}})
    assert stale.status_code == 409
    strict = client.post("/needs/match", json={**authority, "need": {**base, "properties": ["lactose-free"]}})
    assert strict.status_code == 200
    offer = client.post("/offers", json={**authority, "option": strict.json()["options"][0]}).json()
    result = client.post("/orders", json={**authority, "offer": offer["offer"]}, headers={"Idempotency-Key": "strict-basket"}).json()
    assert result["status"] == "requires_escalation" and not result["checks"][3]["passed"]


def test_acknowledgment_is_exactly_once_preserves_receipt_and_proposes_next_freedom(client):
    proposal = limits(client)["proposals"][0]
    assert client.post(f"/limits/proposals/{proposal['id']}/accept", json={"customer_id": "restaurant", "confirmed": True}, headers=human_auth(client, "restaurant")).status_code == 200
    before = departure(client)["current_freedom"]
    usual_before = client.get("/usual-order?customer_id=restaurant", headers=human_auth(client, "restaurant")).json()
    for i in range(4):
        result = basket_order(client)
        assert result["status"] == "committed"
        reservations = client.app.state.service.reservations()
        endpoint = f"/orders/{result['id']}/signoff"
        assert client.post(endpoint, json={"action": "acknowledge", "customer_id": "hotel", "confirmed": True}, headers=human_auth(client, "restaurant")).status_code == 403
        body = {"action": "acknowledge", "customer_id": "restaurant", "confirmed": True}
        reviewed = client.post(endpoint, json=body, headers=human_auth(client, "restaurant")).json()
        repeated = client.post(endpoint, json=body, headers=human_auth(client, "restaurant")).json()
        assert reviewed == repeated and reviewed["acknowledged"]
        assert reviewed["receipt"] == result["receipt"] and reviewed["record_id"] == result["record_id"]
        assert reviewed["acknowledgment"]["review_record_id"] != result["record_id"]
        assert client.app.state.service.reservations() == reservations
        assert departure(client)["current_freedom"] == before
        assert client.get("/usual-order?customer_id=restaurant", headers=human_auth(client, "restaurant")).json()["lines"] == usual_before["lines"]
        if i < 3:
            assert not limits(client)["proposals"]
    next_proposal = limits(client)["proposals"][0]
    assert len(next_proposal["evidence"]) == 4 and next_proposal["proposed_freedom"] <= .3
    assert client.post(f"/limits/proposals/{next_proposal['id']}/accept", json={"customer_id": "restaurant", "confirmed": True}, headers=human_auth(client, "restaurant")).status_code == 200
    one_more = basket_order(client)
    client.post(f"/orders/{one_more['id']}/signoff", json={"action": "acknowledge", "customer_id": "restaurant", "confirmed": True}, headers=human_auth(client, "restaurant"))
    assert not limits(client)["proposals"], "Old evidence must not immediately earn the next proposal."


def test_anchor_moves_only_after_four_exact_reviews_and_explicit_acceptance(client):
    service = client.app.state.service
    # Set the autonomy state at its signed ceiling to exercise the anchor-proposal branch.
    with service.store.transaction() as db:
        state = service.store.get(db, "freedom", "restaurant:usual basket")
        state["current_freedom"] = .3
        service.store.put(db, "freedom", "restaurant:usual basket", state)
        db.execute("DELETE FROM documents WHERE kind='proposal'")
    baseline = client.get("/usual-order?customer_id=restaurant", headers=human_auth(client, "restaurant")).json()
    _, original_mandate = restaurant(client)
    for _ in range(4):
        result = basket_order(client)
        response = client.post(f"/orders/{result['id']}/signoff", json={"action": "acknowledge", "customer_id": "restaurant", "confirmed": True}, headers=human_auth(client, "restaurant"))
        assert response.status_code == 200
    assert client.get("/usual-order?customer_id=restaurant", headers=human_auth(client, "restaurant")).json()["anchor_id"] == baseline["anchor_id"]
    proposal = limits(client)["proposals"][0]
    assert proposal["type"] == "anchor" and len(proposal["evidence"]) == 4
    response = client.post(f"/limits/proposals/{proposal['id']}/accept", json={"customer_id": "restaurant", "confirmed": True}, headers=human_auth(client, "restaurant"))
    assert response.status_code == 200
    after = client.get("/usual-order?customer_id=restaurant", headers=human_auth(client, "restaurant")).json()
    assert after["anchor_id"] != baseline["anchor_id"] and after["lines"] == proposal["proposed_lines"]
    assert after["history"] == baseline["history"]
    assert restaurant(client)[1]["limits"] == original_mandate["limits"]
    assert departure(client)["current_freedom"] == .3
    client.post("/demo/reset", headers=human_auth(client))
    assert client.get("/usual-order?customer_id=restaurant", headers=human_auth(client, "restaurant")).json()["lines"] == baseline["lines"]


def test_basket_quantity_edit_scales_each_unit_without_mixing_them(client):
    held = basket_order(client, 1.4)
    old = held["offer_payload"]["lines"]
    edited = client.post(f"/orders/{held['id']}/signoff", json={"action": "edit", "quantity": 2}, headers=human_auth(client, "restaurant")).json()
    assert edited["offer_payload"]["need"]["quantity"] == 2
    for before, after in zip(old, edited["offer_payload"]["lines"]):
        assert after["packs"] == 2 * before["packs"]
        assert after["unit"] == before["unit"]
    assert edited["offer_payload"]["overage"] == 0


def test_acknowledging_an_edited_order_does_not_count_as_clean_evidence(client):
    from test_service import signed_mandate, make_offer
    authority = signed_mandate(client, per_order_cap=100)
    authority, offer = make_offer(client, authority)
    held = client.post("/orders", json={**authority, "offer": offer["offer"]}, headers={"Idempotency-Key": "edited-review"}).json()
    edited = client.post(f"/orders/{held['id']}/signoff", json={"action": "edit", "quantity": 10}, headers=human_auth(client)).json()
    assert edited["status"] == "committed" and edited["edited_by_human"]
    reviewed = client.post(f"/orders/{held['id']}/signoff", json={"action": "acknowledge", "customer_id": "hotel", "confirmed": True}, headers=human_auth(client))
    assert reviewed.status_code == 200 and reviewed.json()["acknowledged"]
    with client.app.state.service.store.connect() as db:
        state = client.app.state.service.store.get(db, "freedom", "hotel:drinking milk")
    assert state["clean_approvals"] == [] and state["pattern_approvals"] == {}
