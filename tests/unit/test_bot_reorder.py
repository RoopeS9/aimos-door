import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from reference_bot.client import DoorAPIError, ReferenceBot
from reference_bot.providers import FixtureProvider, ProviderError, ToolProvider, next_monday


MESSAGE = "Same as last week, plus 5 litres more milk."
USUAL = {"customer_id": "restaurant", "anchor_confirmed": True, "anchor_id": "human-confirmed-anchor", "synthetic": True, "lines": [
    {"id": "milk", "name": "Milk", "product_type": "drinking milk", "packs": 25, "pack_size": 1, "quantity": 25, "unit": "L", "required_properties": ["lactose-free"]},
    {"id": "rice", "name": "Rice", "product_type": "rice", "packs": 5, "pack_size": .5, "quantity": 2.5, "unit": "KG", "required_properties": []},
    {"id": "pasta", "name": "Pasta", "product_type": "pasta", "packs": 5, "pack_size": .25, "quantity": 1.25, "unit": "KG", "required_properties": []},
]}


@pytest.mark.asyncio
async def test_preview_fetches_confirmed_usual_and_preserves_other_sales_packs():
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        assert request.url.params["customer_id"] == "restaurant"
        return httpx.Response(200, json=USUAL)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://testserver") as client:
        bot = ReferenceBot(client, FixtureProvider())
        preview = await bot.preview(MESSAGE, "restaurant")
        need = preview["need"]
        assert need["anchor_id"] == USUAL["anchor_id"] and need["properties"] == []
        lines = {line["product_id"]: line for line in need["lines"]}
        assert lines["milk"]["quantity"] == 30 and lines["milk"]["unit"] == "L"
        assert lines["milk"]["properties"] == ["lactose-free"]
        assert lines["rice"]["quantity"] == 5 and lines["rice"]["unit"] == "PACK"
        assert lines["pasta"]["quantity"] == 5 and lines["pasta"]["unit"] == "PACK"
        assert "5 litres more drinking milk" in preview["restatement"]
        assert all(name in preview["restatement"] for name in ("Milk", "Rice", "Pasta", "lactose-free", need["deadline"]))
        assert calls == [("GET", "/usual-order")], "Preview must not match, offer or order."


class StubModelHTTP:
    def __init__(self, arguments):
        self.arguments = arguments
        self.calls = []
        self.transport = httpx.MockTransport(self.handle)
    def handle(self, request):
        self.calls.append(request)
        body = json.loads(request.content)
        assert body["tools"][0]["name"] == "match_need"
        assert "reorder" in body["tools"][0]["input_schema"]["properties"]
        return httpx.Response(200, json={"model": "fixture-real-adapter", "content": [{"type": "tool_use", "name": "match_need", "input": self.arguments}]})
    async def post(self, url, *, headers, json):
        return await self.transport.handle_async_request(httpx.Request("POST", url, headers=headers, json=json))


@pytest.mark.asyncio
async def test_real_provider_uses_structured_reorder_tool_and_rejects_invalid_delta(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-placeholder")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://test")
    arguments = {"intent": "reorder", "reorder": {"deadline": next_monday().isoformat(), "adjustments": [{"product_type": "drinking milk", "quantity": 5, "unit": "L"}]}, "restatement": "I will look up the usual basket and add five litres of milk."}
    upstream = StubModelHTTP(arguments)
    provider = ToolProvider(provider="anthropic", client=upstream)
    result = await provider.plan(MESSAGE)
    assert result["intent"] == "reorder" and result["reorder"]["adjustments"][0]["quantity"] == 5
    assert result["model"]["fixture"] is False and len(upstream.calls) == 1
    upstream.arguments = {**arguments, "reorder": {"deadline": next_monday().isoformat(), "adjustments": [{"product_type": "drinking milk", "quantity": None, "unit": "L"}]}}
    with pytest.raises(ProviderError, match="incomplete usual-order adjustment"):
        await provider.plan(MESSAGE)


@pytest.mark.asyncio
async def test_reorder_rejects_unconfirmed_anchor_wrong_customer_and_ambiguous_line():
    states = [
        {**USUAL, "anchor_confirmed": False},
        {**USUAL, "customer_id": "hotel"},
        {**USUAL, "lines": [*USUAL["lines"], {**USUAL["lines"][0], "id": "second-milk"}]},
    ]
    def handler(request):
        assert request.method == "GET" and request.url.path == "/usual-order"
        return httpx.Response(200, json=states.pop(0))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://testserver") as client:
        bot = ReferenceBot(client, FixtureProvider())
        for _ in range(3):
            with pytest.raises(DoorAPIError):
                await bot.preview(MESSAGE, "restaurant")


@pytest.mark.asyncio
async def test_reorder_execute_retains_anchor_and_exact_line_quantities():
    sent = []
    def handler(request):
        if request.url.path == "/usual-order":
            return httpx.Response(200, json=USUAL)
        if request.url.path == "/demo/customers":
            return httpx.Response(200, json={"customers": [{"id": "restaurant", "mandate": "fixture", "bot_id": "reference-bot"}]})
        body = json.loads(request.content)
        sent.append((request.url.path, body))
        if request.url.path == "/needs/match":
            return httpx.Response(200, json={"options": [{"label": "best", "lines": [], "option_id": "fixture-option"}]})
        if request.url.path == "/offers":
            return httpx.Response(200, json={"offer": "fixture-offer"})
        assert request.url.path == "/orders"
        return httpx.Response(200, json={"id": "fixture-order", "status": "committed", "tier": "Notify", "receipt": "fixture-receipt", "notice": "Approval by silence; undo before cutoff."})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://testserver") as client:
        bot = ReferenceBot(client, FixtureProvider())
        preview = await bot.preview(MESSAGE, "restaurant")
        result = await bot.execute("restaurant", copy.deepcopy(preview["need"]), message=MESSAGE)
        matched = sent[0][1]["need"]
        assert matched["anchor_id"] == USUAL["anchor_id"]
        assert matched["lines"] == preview["need"]["lines"]
        assert result["need"]["reorder_adjustments"] == preview["need"]["reorder_adjustments"]
        assert result["order"]["tier"] == "Notify"


@pytest.mark.asyncio
async def test_restaurant_reorder_through_actual_fixture_api_is_notify_and_keeps_basket(tmp_path):
    from door.main import create_app
    app = create_app(tmp_path, fixture=True)
    with TestClient(app) as client:
        session = client.post("/demo/buyer-session", json={"customer_id": "restaurant"}).json()
        human = {"X-Buyer-Session": session["token"]}
        baseline = client.get("/usual-order", params={"customer_id": "restaurant"}, headers=human).json()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as connection:
            app.state.bot_http_client = connection
            preview_response = client.post("/bot/chat", json={"customer_id": "restaurant", "stage": "preview", "message": MESSAGE})
            assert preview_response.status_code == 200, preview_response.text
            preview = preview_response.json()
            assert preview["need"]["anchor_id"] == baseline["anchor_id"]
            response = client.post("/bot/chat", json={"customer_id": "restaurant", "stage": "execute", "message": MESSAGE, "need": preview["need"], "restatement": preview["restatement"]})
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["order"]["status"] == "committed" and result["order"]["tier"] == "Notify"
            assert result["order"]["receipt"] and "undo" in result["order"]["notice"].lower()
            actual = {line["id"]: line for line in result["order"]["offer_payload"]["lines"]}
            assert actual.keys() == {line["id"] for line in baseline["lines"]}
            for old in baseline["lines"]:
                line = actual[old["id"]]
                assert float(line["packs"]).is_integer()
                if old["product_type"] == "drinking milk":
                    assert line["quantity"] >= old["quantity"] + 5
                else:
                    assert line["packs"] == old["packs"]
            assert client.get("/usual-order", params={"customer_id": "restaurant"}, headers=human).json()["anchor_id"] == baseline["anchor_id"]
