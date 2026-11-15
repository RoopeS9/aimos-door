import copy
import json

import httpx
import pytest

from reference_bot.client import DoorAPIError, ReferenceBot
from reference_bot.providers import FixtureProvider, ProviderError, ProviderLimitError, ToolProvider


SPEC = {
    "needed": "whether to accept the offered milk brand",
    "product_type": "drinking milk",
    "preferences": [{"name": "brand", "requested": "Preferred", "offered": "Alternative"}],
    "answers": [{"value": "approve", "label": "Use this offered brand", "cost": {"price": 120, "date": "2026-11-16"}}, {"value": "reject", "label": "Reject the order", "cost": {"price": 0, "date": None}}],
    "scope_options": ["just_this_once", "always", "always_ask"],
}


@pytest.mark.asyncio
async def test_interview_requires_confirmation_and_uses_server_pending_answer():
    held = {"id": "held-order", "customer_id": "hotel", "status": "requires_escalation", "question_spec": copy.deepcopy(SPEC)}
    calls, model_calls = [], []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json=copy.deepcopy(held))
        body = json.loads(request.content)
        if body["action"] == "prepare_answer":
            assert body["scope"] == "just_this_once" and "confirmed" not in body
            held["pending_answer"] = {"answer": body["answer"], "scope": body["scope"], "confirmation_summary": "Use Alternative milk only for this order, for EUR 120 by 2026-11-16.", "question_hash": "bound-to-offer"}
        else:
            assert body == {"action": "approve", "customer_id": "hotel", "answer": "approve", "scope": "just_this_once", "confirmed": True, "confirmation_summary": held["pending_answer"]["confirmation_summary"], "question_hash": "bound-to-offer"}
            held.update(status="committed", receipt="fixture-receipt")
        return httpx.Response(200, json=copy.deepcopy(held))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://testserver") as client:
        bot = ReferenceBot(client, FixtureProvider(), lambda: model_calls.append("admitted"))
        question = await bot.interview("hotel", "held-order")
        assert question["question"] and question["accepted_answers"] == SPEC["answers"]
        prepared = await bot.answer("hotel", "held-order", "Use that brand just this once", "just_this_once")
        assert prepared["confirmation_summary"] == held["pending_answer"]["confirmation_summary"]
        assert held["status"] == "requires_escalation"
        with pytest.raises(DoorAPIError):
            await bot.confirm("hotel", "held-order", False)
        completed = await bot.confirm("hotel", "held-order", True, buyer_session="offline-buyer-session")
        assert completed["order"]["status"] == "committed"
        assert len(model_calls) == 2, "Confirmation is a human action and must not call a model."
        assert calls == [("GET", "/orders/held-order"), ("GET", "/orders/held-order"), ("POST", "/orders/held-order/signoff"), ("GET", "/orders/held-order"), ("POST", "/orders/held-order/signoff")]


@pytest.mark.asyncio
async def test_other_customer_cannot_interview_or_confirm_order():
    calls = []
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"id": "held-order", "customer_id": "hotel", "status": "requires_escalation", "question_spec": SPEC})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://testserver") as client:
        bot = ReferenceBot(client, FixtureProvider(), lambda: calls.append("model"))
        with pytest.raises(DoorAPIError, match="different customer"):
            await bot.interview("restaurant", "held-order")
        with pytest.raises(DoorAPIError, match="different customer"):
            await bot.answer("restaurant", "held-order", "approve", "always")
        with pytest.raises(DoorAPIError, match="different customer"):
            await bot.confirm("restaurant", "held-order", True, buyer_session="offline-buyer-session")
    assert calls == []


@pytest.mark.asyncio
async def test_ambiguous_answer_never_prepares_or_approves():
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"id": "held-order", "customer_id": "hotel", "status": "requires_escalation", "question_spec": SPEC})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://testserver") as client:
        bot = ReferenceBot(client, FixtureProvider())
        result = await bot.answer("hotel", "held-order", "I am not sure", "just_this_once")
        assert result["clarification"] and "confirmation_summary" not in result
        with pytest.raises(DoorAPIError, match="no pending answer"):
            await bot.confirm("hotel", "held-order", True, buyer_session="offline-buyer-session")


class StubModelHTTP:
    def __init__(self, handler):
        self.transport = httpx.MockTransport(handler)
    async def post(self, url, *, headers, json):
        return await self.transport.handle_async_request(httpx.Request("POST", url, headers=headers, json=json))


@pytest.mark.asyncio
async def test_real_adapter_asks_own_question_and_rejects_scope_widening(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-placeholder")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://test")
    def handler(request):
        body = json.loads(request.content)
        tool = body["tools"][0]
        assert body["tool_choice"] == {"type": "auto"}
        if tool["name"] == "ask_buyer":
            arguments = {"question": "May I use the offered milk brand for this order?"}
        else:
            assert tool["name"] == "prepare_buyer_answer"
            assert tool["input_schema"]["properties"]["answer"]["enum"] == ["approve", "reject", None]
            arguments = {"answer": "approve", "scope": "always", "clarification": ""}
        return httpx.Response(200, json={"content": [{"type": "tool_use", "name": tool["name"], "input": arguments}]})
    provider = ToolProvider(provider="anthropic", client=StubModelHTTP(handler))
    question = await provider.interview(SPEC)
    assert question["question"] == "May I use the offered milk brand for this order?"
    with pytest.raises(ProviderError, match="selected scope"):
        await provider.interview(SPEC, "Use that brand just once", "just_this_once")


def test_http400_credit_balance_errors_are_classified_without_echoing_body():
    response = httpx.Response(400, json={"error": {"message": "Your credit balance is too low; fixture-placeholder"}})
    with pytest.raises(ProviderLimitError) as error:
        ToolProvider._check_status(response)
    assert "fixture-placeholder" not in str(error.value)


@pytest.mark.asyncio
async def test_confirmation_cannot_silently_accept_a_changed_pending_summary():
    pending = {"answer": "approve", "scope": "just_this_once", "confirmation_summary": "Approve this milk brand just this once.", "question_hash": "offer-one"}
    held = {"id": "held-order", "customer_id": "hotel", "status": "requires_escalation", "question_spec": SPEC, "pending_answer": pending}
    def handler(request):
        if request.method == "POST":
            assert json.loads(request.content)["action"] == "prepare_answer", "A changed summary must not reach final sign-off."
        return httpx.Response(200, json=copy.deepcopy(held))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://testserver") as client:
        bot = ReferenceBot(client, FixtureProvider())
        result = await bot.answer("hotel", "held-order", "approve", "just_this_once")
        held["pending_answer"] = {**pending, "scope": "always", "confirmation_summary": "Approve this brand for all drinking milk orders."}
        with pytest.raises(DoorAPIError, match="changed since"):
            await bot.confirm("hotel", "held-order", True, result["confirmation_summary"], pending["question_hash"], buyer_session="offline-buyer-session")


@pytest.mark.asyncio
async def test_door_model_limit_is_preserved_but_ip_limit_is_not_misclassified():
    responses = [{"detail": {"reason": "usage_or_credit_limit", "message": "private upstream message"}}, {"detail": "The per-IP demo request limit has been reached."}]
    def handler(request):
        return httpx.Response(429, json=responses.pop(0))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://testserver") as client:
        bot = ReferenceBot(client, FixtureProvider())
        with pytest.raises(ProviderLimitError) as error:
            await bot.interview("hotel", "held-order")
        assert "private upstream" not in str(error.value)
        with pytest.raises(DoorAPIError):
            await bot.interview("hotel", "held-order")
