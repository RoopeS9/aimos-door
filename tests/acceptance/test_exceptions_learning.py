from __future__ import annotations

import copy
from datetime import date, timedelta
import json

from conftest import auth, buyer, fixture_offer, held_for_preference, human_auth, matching, need, new_mandate, offer, order, verify


def test_D01(client):
    result = matching(client, request_need=need(preferred_product_id="milk-175"))
    options = result["options"]
    assert 1 <= len(options) <= 3
    assert [option["label"] for option in options] == ["best", "better", "good"][:len(options)]
    assert any(len({line["id"] for line in option["lines"]}) > 1 and option["within_preferences"] and all(line["delivery_date"] <= need()["deadline"] for line in option["lines"]) for option in options), "Short stock needs an in-preference fill using another product."
    assert any(any(line["delivery_date"] <= need()["deadline"] for line in option["lines"]) and any(line["delivery_date"] > need()["deadline"] for line in option["lines"]) for option in options), "Short stock needs a part-now/rest-later option."
    assert any(all(line["delivery_date"] > need()["deadline"] for line in option["lines"]) for option in options), "Short stock needs an all-later option."
    for option in options:
        assert sum(line["quantity"] for line in option["lines"]) >= 1000
        assert all(line["properties"]["lactose-free"] == "manufacturer" and line["product_type"] == "drinking milk" for line in option["lines"])


def test_D02(client):
    authority = new_mandate(client, soft_preferences={"soft": {"brand": "Preferred brand"}})
    result = order(client, authority)
    assert result["status"] == "committed" and result["tier"] == "Notify"
    assert result["notice"] and "approval by silence" in result["notice"].lower()
    partial = fixture_offer(client, partial=True)
    partial_result = order(client, document=partial)
    assert partial_result["status"] == "committed" and partial_result["tier"] == "Notify" and partial_result["notice"]
    assert client.app.state.service.reservations()


def test_D03(client):
    _, held = held_for_preference(client)
    assert held["status"] == "requires_escalation" and held["receipt"] is None
    reason = held["checks"][7]["reason"]
    assert not held["checks"][7]["passed"] and len(reason.split()) >= 5 and reason.endswith(".")


def missing_property_offer(client, product_id="milk-unknown"):
    service = client.app.state.service
    original = offer(client)
    product = service.catalog.get(product_id)
    if product is None:
        product = {**service.catalog.get("milk-unknown"), "id": product_id, "name": "Another milk with no manufacturer claim", "names": {"en": "Another milk with no manufacturer claim"}}
        service.catalog.upsert(product)
    line = {**original["payload"]["lines"][0], **product, "product_id": product_id, "pack_size": 1, "packs": 100, "quantity": 100, "unit_price": 1.2, "total": 120, "available": 10000}
    return fixture_offer(client, lines=[line], total=120, need=need(quantity=100))


def test_D04(client):
    document = missing_property_offer(client)
    held = order(client, document=document)
    assert held["status"] == "requires_escalation" and not held["checks"][3]["passed"]
    approved = client.post(f"/orders/{held['id']}/signoff", json={"action": "approve", "confirmations": [{"product_id": "milk-unknown", "property": "lactose-free", "confirmed": True}], "confirmed": True}, headers=human_auth(client))
    assert approved.status_code == 200 and approved.json()["status"] == "committed"
    replay = order(client, document=document)
    assert replay["checks"][3]["passed"] and replay["status"] == "committed"
    other = order(client, document=missing_property_offer(client, "milk-unknown-2"))
    assert not other["checks"][3]["passed"] and other["status"] == "requires_escalation"
    unrelated = copy.deepcopy(document["payload"]["need"])
    unrelated["properties"] = ["gluten-free"]
    different_property = order(client, document=fixture_offer(client, lines=document["payload"]["lines"], need=unrelated))
    assert not different_property["checks"][3]["passed"]


def test_D05(client):
    original = offer(client)
    lines = copy.deepcopy(original["payload"]["lines"])
    late = (date.fromisoformat(need()["deadline"]) + timedelta(days=2)).isoformat()
    for line in lines:
        line["delivery_date"], line["on_time"] = late, False
    result = order(client, document=fixture_offer(client, lines=lines, delivery_date=late, on_time=False))
    assert result["status"] == "requires_escalation" and not result["checks"][4]["passed"]
    assert result["receipt"] is None and result["continue_url"]


def test_E01(client):
    _, held = held_for_preference(client)
    spec = held["question_spec"]
    assert isinstance(spec, dict), "A tradeable hold must supply a question specification."
    assert spec.get("needed") or spec.get("what_is_needed")
    answers = spec.get("answers") or spec.get("accepted_answers")
    assert answers and all(answer.get("value") or answer.get("id") for answer in answers)
    assert all(isinstance(answer.get("cost"), dict) and ("price" in answer["cost"] or "date" in answer["cost"]) for answer in answers)
    scopes = spec.get("scope_options") or spec.get("scopes")
    assert set(scopes) == {"just_this_once", "always", "always_ask"}


def standing_answer(client):
    authority, held = held_for_preference(client)
    before = buyer(client)
    unconfirmed = client.post(f"/orders/{held['id']}/signoff", json={"action": "approve", "answer": "approve", "scope": "always", "confirmed": False}, headers=human_auth(client))
    assert unconfirmed.status_code in (200, 409, 422)
    assert buyer(client)["mandate"] == before["mandate"], "No standing answer may take effect before confirmation."
    detail = unconfirmed.json().get("detail", unconfirmed.json())
    assert isinstance(detail, dict) and (detail.get("confirmation_summary") or detail.get("summary")), "The human must be shown a one-line summary to confirm."
    confirmed = client.post(f"/orders/{held['id']}/signoff", json={"action": "approve", "answer": "approve", "scope": "always", "confirmed": True}, headers=human_auth(client))
    assert confirmed.status_code == 200, confirmed.text
    after = buyer(client)
    assert after["mandate"] != before["mandate"]
    return authority, held, before, after, confirmed.json()


def test_E02(client):
    _, held, before, after, result = standing_answer(client)
    payload = verify(after["mandate"], client.get("/demo/buyer-jwks").json())
    assert payload["version"] > before["mandate_payload"]["version"]
    assert payload["learned_preferences"] and result["status"] == "committed"
    replay = order(client, auth(client), request_need=held["offer_payload"]["need"])
    assert replay["status"] == "committed" and replay["question_spec"] is None


def test_E03(client):
    _, held = held_for_preference(client)
    before = buyer(client)["mandate"]
    response = client.post(f"/orders/{held['id']}/signoff", json={"action": "approve", "answer": "approve", "scope": "just_this_once", "confirmed": True}, headers=human_auth(client))
    assert response.status_code == 200 and response.json()["status"] == "committed"
    assert buyer(client)["mandate"] == before
    record = client.get(f"/records/{response.json()['record_id']}").json()
    text = json.dumps(record).lower()
    assert "just_this_once" in text or "just this once" in text
    assert "approve" in text


def test_E04(client):
    _, held, before, after, _ = standing_answer(client)
    payload = after["mandate_payload"]
    learned = payload["learned_preferences"]
    assert all(entry.get("product_type") == "drinking milk" or entry.get("scope", {}).get("product_type") == "drinking milk" for entry in learned)
    authority = auth(client)
    source = offer(client, authority)
    different = copy.deepcopy(source["payload"]["lines"])
    for line in different:
        line["product_type"] = "rice"
    other = order(client, authority, fixture_offer(client, authority, lines=different, need=need(product_type="rice"), within_preferences=False))
    assert not other["checks"][7]["passed"] and other["status"] == "requires_escalation"
    assert other["question_spec"]


def test_E05(client):
    standing_answer(client)
    limits = client.get("/limits", params={"customer_id": "hotel"}, headers=human_auth(client)).json()
    learned = limits["learned_preferences"]
    assert learned and all(row["question"] and row["answer"] for row in learned)
    selected = learned[0]
    removed = client.post(f"/limits/{selected['id']}/reset", json={"customer_id": "hotel", "confirmed": True}, headers=human_auth(client))
    assert removed.status_code == 200
    remaining = client.get("/limits", params={"customer_id": "hotel"}, headers=human_auth(client)).json()["learned_preferences"]
    assert selected["id"] not in {row["id"] for row in remaining}
    replay = order(client, auth(client))
    assert replay["status"] == "requires_escalation" and replay["question_spec"]
