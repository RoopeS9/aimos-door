from __future__ import annotations

from datetime import datetime, timedelta, timezone
import httpx
import pytest

from conftest import auth, matching, need
from door.decisions import DecisionLabeler
from door.matching import rank_options, whole_packs


def test_B01(client):
    response = client.get("/catalog/search", params={"text": "milk"})
    assert response.status_code == 200 and response.json()["products"]
    forbidden = {"price", "stock", "availability", "available", "unit_price", "catalogPrice", "comparisonPrice", "scheduleLines"}
    def walk(value):
        if isinstance(value, dict):
            assert not forbidden.intersection(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    for product in response.json()["products"]:
        assert product["id"] and product["gtin"] and product["names"]
        walk(product)


def test_B02(client):
    for path, body in (("/needs/match", {"need": need()}), ("/offers", {"option": {}})):
        for authority in ({}, {"mandate": "broken", "bot_id": "reference-bot"}):
            assert client.post(path, json={**body, **authority}).status_code in (401, 403)
    for path, operations in client.get("/openapi.json").json()["paths"].items():
        if "price" in path or "availability" in path or "stock" in path:
            for method in operations:
                if method in {"get", "post"}:
                    response = getattr(client, method)(path, **({"json": {}} if method == "post" else {}))
                    assert response.status_code in (401, 403), path


@pytest.mark.live
def test_B03(live):
    result = matching(live)
    assert result["data_source"] == "Valio mock API"
    assert 1 <= len(result["options"]) <= 3
    assert [row["label"] for row in result["options"]] == ["best", "better", "good"][:len(result["options"])]
    for option in result["options"]:
        filters = " ".join(option["filters_applied"]).lower()
        assert "lactose-free" in filters and "drinking milk" in filters
        for line in option["lines"]:
            assert line["product_type"] == "drinking milk"
            name = " ".join(line["names"].values()).lower()
            assert not any(word in name for word in ("sour milk", "piimä", "coffee", "kahvi", "milk powder", "maitojauhe", "cream", "kerma", "barista"))
            assert line["price_source"] == "Valio mock API" and line["stock_source"] == "Valio mock API"


def test_B04(client):
    result = matching(client)
    assert result["options"]
    for option in result["options"]:
        for line in option["lines"]:
            assert line["properties"]["lactose-free"] == "manufacturer"
            assert client.app.state.service.catalog.get(line["id"])["properties"]["lactose-free"] == "manufacturer"
    assert "milk-unknown" not in {line["id"] for option in result["options"] for line in option["lines"]}


def test_B05(client):
    for product_id, pack_size, expected_packs, expected_overage in (("milk-175", 1.75, 572, 1), ("milk-10", 10, 100, 0), ("milk-12", 12, 84, 8)):
        client.app.state.service.valio.fixture_availability[product_id] = {"available": 10000, "delivery_date": need()["deadline"], "cutoff": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), "sales_block": False, "deliverable": True}
        result = matching(client, request_need=need(product_ids=[product_id]))
        line = result["options"][0]["lines"][0]
        assert line["packs"] == expected_packs and line["pack_size"] == pack_size
        assert result["options"][0]["overage"] == expected_overage
        assert isinstance(line["packs"], int) and line["quantity"] >= 1000
        assert whole_packs(1000, pack_size) == (expected_packs, expected_overage)


def test_B06():
    def option(id, on_time, inside, overage, price):
        return {"option_id": id, "on_time": on_time, "within_preferences": inside, "overage": overage, "total": price * (1000 + overage), "lines": [{"quantity": 1000 + overage}]}
    choices = [option("late-cheap", False, True, 0, .1), option("outside-cheap", True, False, 0, .2), option("overage-cheap", True, True, 1, .3), option("exact-expensive", True, True, 0, 2), option("exact-cheap", True, True, 0, 1)]
    assert [row["option_id"] for row in rank_options(choices)] == ["exact-cheap", "exact-expensive", "overage-cheap", "outside-cheap", "late-cheap"]


def test_B07(client, monkeypatch):
    calls = []
    async def unreachable(self, url, **kwargs):
        calls.append(url)
        raise httpx.ConnectError("Unreachable decision fixture")
    monkeypatch.setattr(httpx.AsyncClient, "post", unreachable)
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-fixture-key")
    service = client.app.state.service
    service.matcher.decision_labeler = DecisionLabeler(service.data_dir, enabled=True)
    result = matching(client)
    assert result["options"] and calls
    for option in result["options"]:
        for line in option["lines"]:
            assert line["product_type"] == "drinking milk" and line["product_type_source"] == "inferred"
            assert line["properties"]["lactose-free"] == "manufacturer"


def test_B08(client):
    service = client.app.state.service
    service.valio.fixture_availability["milk-10"] = {"available": 10000, "delivery_date": need()["deadline"], "cutoff": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), "sales_block": False, "deliverable": True}
    result = matching(client, request_need=need(product_ids=["milk-10"]))
    assert all(not option["on_time"] for option in result["options"])
    assert all(not line["on_time"] for option in result["options"] for line in option["lines"])
