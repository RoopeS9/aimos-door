import json

import httpx
import pytest

from door.catalog import Catalog
from door.matching import Matcher
from door.valio import ValioClient
from reference_bot.conversation import deadline_is_stated, quantity_is_stated, selected_product_id
from reference_bot.providers import ToolProvider, next_monday


class StubConversationHTTP:
    def __init__(self):
        self.calls = []
        self.transport = httpx.MockTransport(self.respond)

    def respond(self, request):
        self.calls.append(request)
        return httpx.Response(200, json={
            "model": "fixture-conversation-model",
            "choices": [{"message": {"tool_calls": [{"function": {"name": "search_catalog", "arguments": json.dumps({"text": "milk", "properties": ["lactose-free"]})}}]}}],
        })

    async def post(self, url, *, headers, json):
        return await self.transport.handle_async_request(httpx.Request("POST", url, headers=headers, json=json))


@pytest.mark.asyncio
async def test_conversation_adapter_uses_five_tools_history_and_one_model_call(monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "offline-fixture-key")
    monkeypatch.setenv("AGENT_BASE_URL", "http://test/v1")
    upstream = StubConversationHTTP()
    history = [{"role": "user", "text": "Earlier buyer text"}, {"role": "assistant", "text": "Earlier answer", "action": "products", "products": [{"product_id": "milk-10", "name": "Milk"}]}]
    provider = ToolProvider(provider="openai_compatible", model="fixture-conversation-model", client=upstream)
    result = await provider.converse("What lactose-free milks do you have?", history)
    assert result["name"] == "search_catalog" and result["arguments"]["properties"] == ["lactose-free"]
    assert len(upstream.calls) == 1
    body = json.loads(upstream.calls[0].content)
    assert body["max_tokens"] == 800 and body["tool_choice"] == "required"
    assert {tool["function"]["name"] for tool in body["tools"]} == {"reply", "show_limits", "show_usual_order", "search_catalog", "ask", "draft_order", "place_order"}
    content = json.loads(body["messages"][1]["content"])
    assert content == {"history": history, "current_message": "What lactose-free milks do you have?"}


def test_buyer_message_policy_supports_grouped_quantities_weekdays_and_ordinal_memory():
    monday = next_monday().isoformat()
    assert quantity_is_stated(1000, "L", ["I need 1,000 litres by Monday"])
    assert quantity_is_stated(1000, "L", ["Tarvitsen 1 000 litraa maanantai"])
    assert not quantity_is_stated(1000, "L", ["Order 200 litres by Monday"])
    assert not quantity_is_stated(200, "L", ["Order 20 litres"])
    assert deadline_is_stated(monday, ["I need it by Monday"])
    assert not deadline_is_stated(monday, ["Order 200 litres of milk"])
    history = [{"role": "assistant", "action": "products", "text": "Matches", "products": [{"product_id": "first"}, {"product_id": "chosen", "name": "Second"}]}]
    assert selected_product_id("I'll take the second one", history) == "chosen"


@pytest.mark.asyncio
async def test_matcher_product_id_is_exact_and_reports_missing_manufacturer_property(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.seed_fixture()
    matcher = Matcher(catalog, ValioClient(tmp_path, fixture=True))
    base = {"product_type": "an inaccurate model label", "quantity": 20, "unit": "L", "deadline": next_monday().isoformat(), "properties": ["lactose-free"]}
    selected = await matcher.match({**base, "product_id": "milk-12"}, "hotel")
    assert selected["product_selection"]["satisfies_properties"] is True
    assert selected["need"]["product_type"] == "drinking milk"
    assert len(selected["options"]) == 1 and selected["options"][0]["lines"][0]["id"] == "milk-12"
    missing = await matcher.match({**base, "product_id": "milk-unknown"}, "hotel")
    assert missing["product_selection"]["satisfies_properties"] is False
    assert "lactose-free" in missing["product_selection"]["missing_properties"]
    assert "does not satisfy" in missing["product_selection"]["message"]
