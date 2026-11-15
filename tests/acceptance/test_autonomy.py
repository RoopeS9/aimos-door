from __future__ import annotations

import copy
from datetime import date, datetime
import json

import pytest

from conftest import ROOT, assert_live_model, auth, fixture_offer, human_auth, need, order, verify


def usual(client):
    response = client.get("/usual-order", params={"customer_id": "restaurant"}, headers=human_auth(client, "restaurant"))
    assert response.status_code == 200
    result = response.json()
    assert len(result.get("history", [])) == 12, "Seed twelve weekly restaurant orders before exercising autonomy."
    assert result.get("lines"), "The restaurant requires a human-confirmed usual basket."
    return result


def departure_order(client, factor):
    baseline = usual(client)
    lines = copy.deepcopy(baseline["lines"])
    for line in lines:
        line["packs"] = max(1, round(line["packs"] * factor, 8))
        line["quantity"] = line["packs"] * line["pack_size"]
        line["total"] = round(line["packs"] * line["unit_price"], 2)
    authority = auth(client, "restaurant")
    # Basket constraints belong to each line; the helper's milk-only default
    # must not require the rice staple to carry a lactose-free dairy claim.
    document = fixture_offer(client, authority, lines=lines, total=sum(line["total"] for line in lines), need=need(product_type="usual basket", properties=[], lines=lines), on_time=True)
    return order(client, authority, document)


def departure_row(client):
    limits = client.get("/limits", params={"customer_id": "restaurant"}, headers=human_auth(client, "restaurant")).json()
    return next(row for row in limits["rows"] if row["id"] == "departure")


def test_F01(client):
    first = usual(client)
    assert first["synthetic"] is True
    assert "synthetic" in json.dumps(first).lower()
    dates = [date.fromisoformat((entry.get("date") or entry.get("delivery_date") or entry.get("created_at"))[:10]) for entry in first["history"]]
    assert sorted((right - left).days for left, right in zip(sorted(dates), sorted(dates)[1:])) == [7] * 11
    assert any(line["product_type"] == "drinking milk" and line["properties"].get("lactose-free") == "manufacturer" for line in first["lines"])
    assert len(first["lines"]) >= 2
    assert client.post("/demo/reset", headers=human_auth(client)).status_code == 200
    second = usual(client)
    assert first["history"] == second["history"] and first["lines"] == second["lines"]
    source = (ROOT / "frontend/src/App.tsx").read_text()
    assert "Synthetic order history" in source and "weekly orders" in source


@pytest.mark.live
def test_F02(live):
    baseline = usual(live)
    response = live.post("/bot/chat", json={"customer_id": "restaurant", "message": "Same as last week, plus 5 litres more milk."})
    assert response.status_code == 200, response.text
    result = response.json()
    assert_live_model(result)
    actual = {line["id"]: line for line in result["order"]["offer_payload"]["lines"]}
    expected = {line["id"]: line for line in baseline["lines"]}
    assert actual.keys() == expected.keys()
    for product_id, old in expected.items():
        line = actual[product_id]
        assert float(line["packs"]).is_integer()
        if old["product_type"] == "drinking milk":
            assert line["quantity"] >= old["quantity"] + 5 and line["unit"] == "L"
        else:
            assert line["packs"] == old["packs"]


def test_F03(client):
    row = departure_row(client)
    assert 0 < row["current_freedom"] < row["signed_ceiling"]
    silent = departure_order(client, 1 + row["current_freedom"] / 2)
    notice = departure_order(client, 1 + (row["current_freedom"] + row["signed_ceiling"]) / 2)
    held = departure_order(client, 1 + row["signed_ceiling"] + .05)
    assert silent["status"] == "committed" and silent["tier"] == "Commit" and not silent["notice"]
    assert notice["status"] == "committed" and notice["tier"] == "Notify" and notice["notice"]
    assert "undo" in notice["notice"].lower() and "approval by silence" in notice["notice"].lower()
    assert held["status"] == "requires_escalation" and not held["checks"][8]["passed"]
    assert held["receipt"] is None


def test_F04(client):
    before = departure_row(client)
    result = departure_order(client, 1 + (before["current_freedom"] + before["signed_ceiling"]) / 2)
    reserved = client.app.state.service.reservations()
    assert result["tier"] == "Notify" and reserved
    response = client.post(f"/orders/{result['id']}/undo", headers=human_auth(client, "restaurant"))
    assert response.status_code == 200 and response.json()["status"] == "withdrawn"
    assert sum(client.app.state.service.reservations().values()) < sum(reserved.values())
    after = departure_row(client)
    assert after["current_freedom"] < before["current_freedom"] and after["signed_ceiling"] == before["signed_ceiling"]
    assert "undo" in after["reason"].lower() or "withdraw" in after["reason"].lower()
    next_order = departure_order(client, 1.01)
    assert next_order["status"] == "requires_escalation", "After a correction the same kind of order must ask next time."


def test_F05(client):
    client.post("/demo/reset", headers=human_auth(client))
    limits = client.get("/limits", params={"customer_id": "restaurant"}, headers=human_auth(client, "restaurant")).json()
    assert len(limits["proposals"]) == 1
    proposal = limits["proposals"][0]
    assert proposal["evidence"]
    before = departure_row(client)
    response = client.post(f"/limits/proposals/{proposal['id']}/accept", json={"customer_id": "restaurant", "confirmed": True}, headers=human_auth(client, "restaurant"))
    assert response.status_code == 200
    after = departure_row(client)
    assert before["current_freedom"] < after["current_freedom"] <= after["signed_ceiling"] == before["signed_ceiling"]
    assert not client.get("/limits", params={"customer_id": "restaurant"}, headers=human_auth(client, "restaurant")).json()["proposals"]


def test_F06(client):
    seeded = client.get("/limits", params={"customer_id": "restaurant"}, headers=human_auth(client, "restaurant")).json()["proposals"][0]
    assert client.post(f"/limits/proposals/{seeded['id']}/accept", json={"customer_id": "restaurant", "confirmed": True}, headers=human_auth(client, "restaurant")).status_code == 200
    assert not client.get("/limits", params={"customer_id": "restaurant"}, headers=human_auth(client, "restaurant")).json()["proposals"]
    before = departure_row(client)
    approved_ids = []
    for index in range(4):
        result = departure_order(client, 1.2)
        assert result["status"] == "committed"
        reviewed = client.post(f"/orders/{result['id']}/signoff", json={"action": "acknowledge", "customer_id": "restaurant", "confirmed": True}, headers=human_auth(client, "restaurant"))
        assert reviewed.status_code == 200 and reviewed.json()["acknowledged"]
        approved_ids.append(result["id"])
        assert departure_row(client)["current_freedom"] == before["current_freedom"]
        if index < 3:
            assert not client.get("/limits", params={"customer_id": "restaurant"}, headers=human_auth(client, "restaurant")).json()["proposals"]
    after = departure_row(client)
    assert after["current_freedom"] == before["current_freedom"]
    limits = client.get("/limits", params={"customer_id": "restaurant"}, headers=human_auth(client, "restaurant")).json()
    assert len(limits["proposals"]) == 1
    assert {entry["order_id"] for entry in limits["proposals"][0]["evidence"]} == set(approved_ids)


def test_F07(client):
    initial = usual(client)
    outcomes = [departure_order(client, 1.11 ** week) for week in (1, 2, 3)]
    assert outcomes[0]["checks"][8]["passed"] and outcomes[1]["checks"][8]["passed"]
    assert not outcomes[2]["checks"][8]["passed"] and outcomes[2]["status"] == "requires_escalation"
    assert usual(client)["lines"] == initial["lines"], "Clean orders must not move the human-confirmed usual anchor."
