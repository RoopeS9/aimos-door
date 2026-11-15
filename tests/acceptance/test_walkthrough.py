"""Advisor-owned cases N05–N11 and N13, found by walking through the chat."""
from __future__ import annotations

import asyncio
import json

import pytest

from reference_bot.client import DoorAPIError, ReferenceBot
from reference_bot.conversation import has_buying_intent, is_complete_instruction
from reference_bot.providers import ToolProvider
from test_buyer_testing import _browse_fixture, _converse, _inject_bot_http
from test_conversation import order_count


def test_N05(client):
    """Limits questions are answered from the door, never with 'I cannot see them'."""
    connection = _inject_bot_http(client)
    try:
        before = order_count(client)
        for question in ("What are my limits?", "How much can I spend per order?"):
            answer = _converse(client, question)
            assert answer["action"] == "reply" and "€2,000.00 per order" in answer["reply"]
            assert "monthly budget of €20,000.00" in answer["reply"] and "Only you can change" in answer["reply"]
            assert "cannot see" not in answer["reply"].lower() and "can't see" not in answer["reply"].lower()
        assert order_count(client) == before
    finally:
        asyncio.run(connection.aclose())


def test_reorder_survives_a_model_that_lists_the_basket(client):
    """If the model answers the reorder sentence with the listing tool, the bot still places the reorder."""
    from reference_bot.providers import FixtureProvider

    class Lister(FixtureProvider):
        async def converse(self, message, history):
            return {"name": "show_usual_order", "arguments": {}, "model": {"fixture": True}}

    connection = _inject_bot_http(client)
    try:
        client.app.state.bot_provider = Lister()
        before = order_count(client, "restaurant")
        placed = _converse(client, "Same as last week, plus 5 litres more milk.", customer_id="restaurant")
        assert placed["action"] == "order" and order_count(client, "restaurant") == before + 1
    finally:
        asyncio.run(connection.aclose())


def test_N06(client):
    """Asking what the usual order contains lists it and orders nothing."""
    connection = _inject_bot_http(client)
    try:
        before = order_count(client, "restaurant")
        answer = _converse(client, "What is in my usual order?", customer_id="restaurant")
        assert answer["action"] == "reply" and "usual order has" in answer["reply"] and "Nothing has been ordered" in answer["reply"]
        assert answer.get("order") is None and order_count(client, "restaurant") == before
        assert not is_complete_instruction("What is in my usual order?")
        assert is_complete_instruction("Same as last week")
        placed = _converse(client, "Same as last week", customer_id="restaurant")
        assert placed["action"] == "order" and order_count(client, "restaurant") == before + 1
    finally:
        asyncio.run(connection.aclose())


def test_N07(client):
    """The many ways of asking to buy all count as buying intent."""
    for phrase in ("Give me 50 litres of milk by Monday", "get me 10 packs of butter", "I'll take 20 litres of the second one",
                   "I want 5 kg of cheese", "we need 12 litres of cream", "send me 3 packs of coffee",
                   "2 litres of cream by Monday", "10 packs of the third one by Friday"):
        assert has_buying_intent([phrase]), phrase
    assert has_buying_intent(["2 litres of cream and 3 kg of butter by Monday", "the cream first"])
    assert not has_buying_intent(["What lactose-free milk do you have?"])
    assert not is_complete_instruction("2 litres of cream and 3 kg of butter by Monday")
    from reference_bot.conversation import quantity_is_stated
    assert quantity_is_stated(3, "PACK", ["Order 3 mops by Monday"]) and quantity_is_stated(12, "PACK", ["12 of the first one by Monday"])
    assert not quantity_is_stated(3, "PACK", ["Order mops by Monday 3.11."])
    assert not quantity_is_stated(16, "PACK", ["some butter by 16 Nov"])
    assert not quantity_is_stated(300, "PACK", ["I need coffee for 300 guests"])
    connection = _inject_bot_http(client)
    try:
        before = order_count(client)
        bare = _converse(client, "50 litres of lactose-free milk by Monday")
        assert bare["action"] == "order" and bare["need"]["quantity"] == 50 and order_count(client) == before + 1
        given = _converse(client, "Give me 20 litres of lactose-free milk by Monday")
        assert given["action"] == "order" and given["need"]["quantity"] == 20
    finally:
        asyncio.run(connection.aclose())


def test_N08(client, monkeypatch):
    """A reorder change the bot cannot place is a question back, not a 5xx."""
    async def refuse(self, customer_id, plan):
        raise DoorAPIError("The requested change does not identify exactly one product in the usual basket. Please name a specific line.")
    monkeypatch.setattr(ReferenceBot, "_expand_reorder", refuse)
    connection = _inject_bot_http(client)
    try:
        before = order_count(client, "restaurant")
        answer = _converse(client, "Same as last week, plus 5 litres more milk.", customer_id="restaurant")
        assert answer["action"] == "clarification" and "exactly one product" in answer["reply"]
        assert order_count(client, "restaurant") == before
    finally:
        asyncio.run(connection.aclose())


def test_N09(client):
    """Asked to undo an order, the bot says who can and never claims nothing was ordered."""
    connection = _inject_bot_http(client)
    try:
        history = [{"role": "user", "text": "Same as last week, plus 5 litres more milk."},
                   {"role": "assistant", "text": "This order is committed by approval by silence.", "action": "order"}]
        before = order_count(client, "restaurant")
        for request in ("Actually undo that", "cancel it"):
            answer = _converse(client, request, history=history, customer_id="restaurant")
            assert answer["action"] == "reply" and "Undo this order" in answer["reply"] and "Sign-off" in answer["reply"]
            assert "nothing" not in answer["reply"].lower().split("undo this order")[0]
        assert order_count(client, "restaurant") == before
    finally:
        asyncio.run(connection.aclose())


@pytest.mark.asyncio
async def test_N10(monkeypatch):
    """The model is given the door's own rules, and told not to send the buyer elsewhere."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("conversation_policy_stub", Path(__file__).parents[1] / "unit" / "test_conversation_policy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    StubConversationHTTP = module.StubConversationHTTP
    monkeypatch.setenv("AGENT_API_KEY", "offline-fixture-key")
    monkeypatch.setenv("AGENT_BASE_URL", "http://test/v1")
    upstream = StubConversationHTTP()
    provider = ToolProvider(provider="openai_compatible", model="fixture-conversation-model", client=upstream)
    await provider.converse("What happens if stock runs out?", [])
    system = json.loads(upstream.calls[0].content)["messages"][0]["content"]
    for fact in ("never substitutes silently", "order cutoffs depend on the product", "Never tell the buyer to contact",
                 "only the buyer can", "simulated", "language of the buyer"):
        assert fact in system, fact


def test_N11(client):
    """'Which is the cheapest?' names the cheapest product in stock and lists it first."""
    connection = _inject_bot_http(client)
    try:
        before = order_count(client)
        answer = _converse(client, "Which lactose-free milk is the cheapest?")
        assert answer["action"] == "products" and answer["products"]
        stocked = [product for product in answer["products"] if product["availability"] in {"in_stock", "limited"}]
        if stocked:
            assert answer["reply"].startswith("The cheapest") and stocked[0]["name"] in answer["reply"]
            assert answer["products"][0] == stocked[0]
        else:
            assert "cannot name a cheapest" in answer["reply"]
        assert order_count(client) == before
    finally:
        asyncio.run(connection.aclose())


def test_N13(tmp_path):
    """'Ice cream' finds ice cream; 'cream' still finds cream."""
    catalog = _browse_fixture(tmp_path)
    cream = catalog.search_relevant("cream", limit=6)
    assert cream and all(product["id"].startswith("cream-") for product in cream), [product["id"] for product in cream]
    ice = catalog.search_relevant("ice cream", limit=6)
    assert ice and ice[0]["id"].startswith("ice-"), [product["id"] for product in ice]
    assert sum(product["id"].startswith("ice-") for product in ice) >= 4, [product["id"] for product in ice]
