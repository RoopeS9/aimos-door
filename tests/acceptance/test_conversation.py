from __future__ import annotations

import asyncio

import httpx

from conftest import human_auth
from reference_bot.providers import next_monday


def inject_http(client, events=None):
    async def record(request):
        if events is not None:
            events.append((request.method, request.url.path))

    connection = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app),
        base_url="http://testserver",
        event_hooks={"request": [record]},
    )
    client.app.state.bot_http_client = connection
    return connection


def converse(client, message, *, customer_id="hotel", history=None, **extra):
    response = client.post("/bot/chat", json={
        "stage": "converse", "customer_id": customer_id, "message": message,
        "history": history or [], **extra,
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["stage"] == "conversation" and result["reply"]
    assert result["model"]["fixture"] is True
    return result


def order_count(client, customer_id="hotel"):
    with client.app.state.service.store.connect() as db:
        return db.execute("SELECT COUNT(*) FROM orders WHERE customer_id=?", (customer_id,)).fetchone()[0]


def document_count(client, kind):
    with client.app.state.service.store.connect() as db:
        return db.execute("SELECT COUNT(*) FROM documents WHERE kind=?", (kind,)).fetchone()[0]


def reset(client):
    response = client.post("/demo/reset", json={}, headers=human_auth(client))
    assert response.status_code == 200, response.text


def test_L01(client):
    before = order_count(client)
    calls = []
    connection = inject_http(client, calls)
    try:
        result = converse(client, "Hi, what can you help me with?")
        assert result["action"] == "reply"
        reply = result["reply"].lower()
        assert "what aimo sells" in reply and "price" in reply and "stock" in reply
        assert "orders" in reply and "signed limits" in reply
        assert order_count(client) == before == 0
        assert document_count(client, "option") == document_count(client, "offer") == 0
        assert not calls
    finally:
        asyncio.run(connection.aclose())


def test_L02(client):
    before = order_count(client)
    calls = []
    connection = inject_http(client, calls)
    try:
        milks = converse(client, "What lactose-free milks do you have?")
        assert milks["action"] == "products" and 2 <= len(milks["products"]) <= 6
        assert milks["delivery_date"]
        for product in milks["products"]:
            assert product["product_id"] and product["name"] and product["pack"]
            assert isinstance(product["price_eur"], (int, float)) and product["price_eur"] > 0
            assert product["unit_price"] and product["availability"] in {"in_stock", "limited", "unavailable", "unknown"}
            assert product["available_text"] and "lactose-free" in product["properties"]
        for question, expected in (("Do you sell oat drink?", "oat"), ("what cheese do you have?", "cheese")):
            result = converse(client, question)
            assert result["action"] == "products" and 1 <= len(result["products"]) <= 6
            assert any(expected in product["name"].lower() for product in result["products"])
            assert result["delivery_date"] and all("price_eur" in product and "availability" in product for product in result["products"])
        assert order_count(client) == before == 0
        assert document_count(client, "offer") == 0
        assert calls and all(path == "/catalog/search" for _, path in calls)
    finally:
        asyncio.run(connection.aclose())


def test_L03(client):
    connection = inject_http(client)
    try:
        for message, product in (("some cheese please", "cheese"), ("Can I get butter?", "butter")):
            before = order_count(client)
            reservations = client.app.state.service.reservations()
            result = converse(client, message)
            assert result["action"] == "clarification"
            assert "how much" in result["reply"].lower() and product in result["reply"].lower()
            assert order_count(client) == before == 0
            assert client.app.state.service.reservations() == reservations
            assert not result.get("order")
    finally:
        asyncio.run(connection.aclose())


def test_L04(client):
    connection = inject_http(client)
    try:
        result = converse(client, "I need 1,000 litres of lactose-free milk by Monday.")
        assert result["action"] == "order" and result["order"]["status"] == "committed"
        assert result["order"]["receipt"] and result["need"]["quantity"] == 1000
        assert order_count(client) == 1
        before_reorder = order_count(client, "restaurant")
        reordered = converse(client, "Same as last week, plus 5 litres more milk.", customer_id="restaurant")
        assert reordered["action"] == "order" and reordered["order"]["status"] == "committed"
        assert reordered["need"]["product_type"] == "usual basket"
        assert reordered["need"]["reorder_adjustments"] == [{"product_type": "drinking milk", "quantity": 5.0, "unit": "L"}]
        assert order_count(client, "restaurant") == before_reorder + 1
    finally:
        asyncio.run(connection.aclose())


def test_L05(client):
    connection = inject_http(client)
    message = "Order 200 litres of lactose-free milk"
    try:
        result = converse(client, message)
        assert result["action"] == "draft" and result["draft"]
        draft = result["draft"]
        assert draft["need"]["quantity"] == 200 and draft["need"]["unit"] == "L"
        assert draft["product_name"] and draft["pack"] and isinstance(draft["packs"], int) and draft["packs"] > 0
        assert draft["total_eur"] > 0 and draft["delivery_date"]
        assert order_count(client) == 0 and result["offer"] is result["order"] is None
        assert client.app.state.service.reservations() == {}

        executed = client.post("/bot/chat", json={
            "stage": "execute", "customer_id": "hotel", "message": message,
            "need": draft["need"], "restatement": draft["restatement"],
            "simulate_limited_stock": False, "idempotency_key": "L05-console-draft",
        })
        assert executed.status_code == 200, executed.text
        placed = executed.json()
        assert placed["order"]["status"] == "committed" and order_count(client) == 1
        assert placed["match"]["options"][0]["lines"][0]["packs"] == draft["packs"]
        assert placed["match"]["options"][0]["total"] == draft["total_eur"]

        reset(client)
        history = [
            {"role": "user", "text": message},
            {"role": "assistant", "text": result["reply"], "action": "draft", "products": [], "draft": draft},
        ]
        confirmed = converse(client, "yes, place it", history=history, idempotency_key="L05-confirmed-draft")
        assert confirmed["action"] == "order" and confirmed["order"]["status"] == "committed"
        assert confirmed["need"]["quantity"] == 200 and order_count(client) == 1

        reset(client)
        declined = converse(client, "no", history=history)
        assert declined["action"] in {"reply", "clarification"} and not declined.get("order")
        changed = converse(client, "Order 300 litres of lactose-free milk instead", history=history)
        assert changed["action"] != "order" and not changed.get("order")
        assert order_count(client) == 0
    finally:
        asyncio.run(connection.aclose())


def test_L06(client):
    class InventedQuantityProvider:
        def __init__(self):
            self.tool = "place_order"

        async def converse(self, message, history):
            need = {
                "product_type": "drinking milk", "properties": ["lactose-free"],
                "quantity": 999, "unit": "L", "deadline": next_monday().isoformat(), "preferences": {},
            }
            return {"name": self.tool, "arguments": {"need": need, "restatement": "Invented quantity."}, "model": {"provider": "fixture", "id": "adversarial-stub", "fixture": True}}

    provider = InventedQuantityProvider()
    client.app.state.bot_provider = provider
    calls = []
    connection = inject_http(client, calls)
    try:
        for tool in ("place_order", "draft_order"):
            provider.tool = tool
            result = converse(client, "Order 200 litres of lactose-free milk by Monday")
            assert result["action"] == "clarification" and "does not appear" in result["reply"]
            assert not result.get("order")
        assert order_count(client) == 0 and document_count(client, "option") == document_count(client, "offer") == 0
        assert not calls, "A model-invented quantity must be refused before any purchasing API call."
    finally:
        asyncio.run(connection.aclose())


def test_L07(client):
    connection = inject_http(client)
    try:
        listed = converse(client, "What lactose-free milks do you have?")
        selected = listed["products"][1]
        history = [
            {"role": "user", "text": "What lactose-free milks do you have?"},
            {"role": "assistant", "text": listed["reply"], "action": "products", "products": listed["products"], "draft": None},
        ]
        result = converse(client, "I'll take 20 litres of the second one by Monday", history=history)
        assert result["action"] in {"draft", "order"}
        assert result["need"]["product_id"] == selected["product_id"]
        assert result["need"]["quantity"] == 20
        lines = [line for option in result["match"]["options"] for line in option["lines"]]
        assert lines and all(line["id"] == selected["product_id"] for line in lines)
        assert all(line["properties"]["lactose-free"] == "manufacturer" for line in lines)
        selection = result["match"]["product_selection"]
        assert selection["satisfies_properties"] is True and "selected" in selection["message"].lower()
    finally:
        asyncio.run(connection.aclose())


def test_L08(client):
    connection = inject_http(client)
    try:
        result = converse(client, "Order 5 litres of oat drink by Monday")
        assert result["action"] in {"products", "clarification"}
        assert result["products"] and any("oat" in product["name"].lower() for product in result["products"])
        assert "?" in result["reply"]
        assert not result.get("order") and order_count(client) == 0
        assert document_count(client, "offer") == 0
    finally:
        asyncio.run(connection.aclose())


def test_complete_instruction_does_not_depend_on_the_model(client):
    """If the model answers a complete instruction with a question or a search, the bot still plans the order."""
    from reference_bot.providers import FixtureProvider

    class Hesitant(FixtureProvider):
        def __init__(self, tool):
            super().__init__()
            self.tool = tool

        async def converse(self, message, history):
            arguments = {"question": "Which milk would you like?", "text": "milk"} if self.tool == "ask" else {"text": "milk", "properties": []}
            return {"name": self.tool, "arguments": arguments, "model": {"fixture": True}}

    connection = inject_http(client)
    try:
        for tool in ("ask", "search_catalog"):
            client.app.state.bot_provider = Hesitant(tool)
            before = order_count(client)
            result = converse(client, "I need 1,000 litres of lactose-free milk by Monday.")
            assert result["action"] == "order" and result["need"]["quantity"] == 1000
            assert order_count(client) == before + 1
        # A question that merely mentions a quantity and a day is still a question.
        client.app.state.bot_provider = Hesitant("search_catalog")
        before = order_count(client)
        asked = converse(client, "What lactose-free milk do you have?")
        assert asked["action"] == "products" and order_count(client) == before
        before_reorder = order_count(client, "restaurant")
        reordered = converse(client, "Same as last week, plus 5 litres more milk.", customer_id="restaurant")
        assert reordered["action"] == "order" and order_count(client, "restaurant") == before_reorder + 1
    finally:
        asyncio.run(connection.aclose())
