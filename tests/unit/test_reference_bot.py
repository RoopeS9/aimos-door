import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from reference_bot.client import ReferenceBot
from reference_bot.providers import FixtureProvider, ProviderError, ProviderLimitError, ToolProvider, next_monday


class MockProviderClient:
    """MockTransport is called directly; no network-capable client is created."""
    def __init__(self, handler):
        self.transport = httpx.MockTransport(handler)

    async def post(self, url, *, headers, json):
        request = httpx.Request("POST", url, headers=headers, json=json)
        return await self.transport.handle_async_request(request)

    async def get(self, url, *, headers):
        return await self.transport.handle_async_request(httpx.Request("GET", url, headers=headers))


def tool_arguments():
    return {"need": {"product_type": "drinking milk", "properties": ["lactose-free"], "quantity": 1000, "unit": "L", "deadline": next_monday().isoformat(), "preferences": {}}, "restatement": "I will find your milk by Monday."}


@pytest.mark.asyncio
async def test_anthropic_tool_adapter_honors_base_url_model_and_key_isolation(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-anthropic")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://test/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-openrouter")
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, request=request, json={"model": "fixture-model", "content": [{"type": "tool_use", "name": "match_need", "input": tool_arguments()}]})

    provider = ToolProvider(provider="anthropic", model="fixture-model", client=MockProviderClient(handler))
    result = await provider.plan("I need 1,000 litres of lactose-free milk by Monday.")
    assert str(captured[0].url) == "http://test/v1/messages"
    assert captured[0].headers["x-api-key"] == "fixture-anthropic"
    assert "authorization" not in captured[0].headers
    payload = json.loads(captured[0].content)
    assert payload["tool_choice"] == {"type": "auto"}
    assert payload["model"] == "fixture-model"
    assert result["need"]["quantity"] == 1000
    assert "1,000 litres" in result["restatement"] and "lactose-free" in result["restatement"]
    assert next_monday().isoformat() in result["restatement"]
    assert result["model"]["provider"] == "anthropic"


@pytest.mark.asyncio
async def test_openrouter_discovers_exact_model_and_works_without_anthropic_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-openrouter")
    captured = []

    def handler(request):
        captured.append(request)
        if request.method == "GET":
            assert "authorization" not in request.headers
            return httpx.Response(200, request=request, json={"data": [{"id": "anthropic/claude-opus-5.5"}, {"id": "irrelevant/model"}]})
        assert request.headers["authorization"] == "Bearer fixture-openrouter"
        assert "x-api-key" not in request.headers
        body = json.loads(request.content)
        assert body["model"] == "anthropic/claude-opus-5.5"
        return httpx.Response(200, request=request, json={"model": body["model"], "choices": [{"message": {"tool_calls": [{"function": {"name": "match_need", "arguments": json.dumps(tool_arguments())}}]}}]})

    provider = ToolProvider(provider="openrouter", client=MockProviderClient(handler))
    first = await provider.plan("1,000 litres of lactose-free milk by Monday")
    await provider.plan("1,000 litres of lactose-free milk by Monday")
    assert [request.method for request in captured] == ["GET", "POST", "POST"]
    assert first["model"]["id"] == "anthropic/claude-opus-5.5"
    assert first["need"]["properties"] == ["lactose-free"]


@pytest.mark.asyncio
async def test_provider_errors_do_not_echo_upstream_response_or_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-private-value")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://test")

    def handler(request):
        return httpx.Response(401, request=request, json={"error": "Rejected credential fixture-private-value"})

    with pytest.raises(ProviderError) as error:
        await ToolProvider(provider="anthropic", client=MockProviderClient(handler)).plan("1,000 litres of milk by Monday")
    assert "fixture-private-value" not in str(error.value)
    assert "HTTP 401" in str(error.value)


@pytest.mark.asyncio
async def test_provider_usage_limit_is_explicit_and_never_silently_falls_back(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-private-value")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://test")
    def handler(request):
        return httpx.Response(429, request=request, json={"error": "limited"})
    with pytest.raises(ProviderLimitError):
        await ToolProvider(provider="anthropic", client=MockProviderClient(handler)).plan("1,000 litres of milk by Monday")


@pytest.mark.asyncio
async def test_reference_bot_orders_through_public_http_only_and_preview_precedes_order(tmp_path):
    from door.main import create_app
    app = create_app(tmp_path, fixture=True)
    with TestClient(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            bot = ReferenceBot(client, FixtureProvider())
            preview = await bot.preview("I need 1,000 litres of lactose-free milk by Monday.")
            with app.state.service.store.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
            result = await bot.execute("hotel", preview["need"], messages=preview["messages"], idempotency_key="bot-fixture-order")
            assert result["order"]["status"] == "committed"
            assert result["order"]["receipt"]
            assert result["messages"][0]["kind"] == "restatement"
            assert result["messages"][0]["at"] <= result["order"]["created_at"]
            assert [item["kind"] for item in result["messages"]] == ["restatement", "matching", "offer", "outcome"]
            repeat = await bot.execute("hotel", preview["need"], idempotency_key="bot-fixture-order")
            assert repeat["order"]["receipt"] == result["order"]["receipt"]
