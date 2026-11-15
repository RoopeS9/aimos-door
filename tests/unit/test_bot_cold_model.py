import json

import httpx
import pytest

from door.bot_runtime import BotRuntime
from reference_bot.providers import ProviderError, ToolProvider


class RecoveringHTTP:
    def __init__(self, failures):
        self.failures = failures
        self.calls = []

    async def post(self, url, *, headers, json):
        self.calls.append(json)
        if len(self.calls) <= self.failures:
            return httpx.Response(503, json={"error": {"message": "cold fixture"}})
        arguments = {
            "intent": "new_order",
            "need": {"product_type": "drinking milk", "properties": ["lactose-free"], "quantity": 1000, "unit": "L", "deadline": "2099-01-04", "preferences": {}},
            "restatement": "I will order 1,000 litres of lactose-free milk by 2099-01-04.",
        }
        return httpx.Response(200, json={"model": json["model"], "choices": [{"message": {"tool_calls": [{"function": {"name": "match_need", "arguments": __import__("json").dumps(arguments)}}]}}]})


@pytest.fixture
def generic_credentials(monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "offline-cold-model-key")
    monkeypatch.delenv("AGENT_FALLBACK_MODEL", raising=False)


@pytest.mark.asyncio
async def test_one_primary_retry_succeeds_without_fallback(generic_credentials):
    upstream = RecoveringHTTP(failures=1)
    result = await ToolProvider(provider="openai_compatible", model="primary-model", client=upstream).plan("1,000 litres of milk")
    assert [call["model"] for call in upstream.calls] == ["primary-model", "primary-model"]
    assert all(call["max_tokens"] == 800 for call in upstream.calls)
    assert result["model"]["id"] == result["model"]["answered_by"] == "primary-model"
    assert result["model"]["attempts"] == 2 and "fallback" not in result["model"]


@pytest.mark.asyncio
async def test_second_primary_failure_uses_default_fallback_exactly_once(generic_credentials):
    upstream = RecoveringHTTP(failures=2)
    result = await ToolProvider(provider="openai_compatible", model="primary-model", client=upstream).plan("1,000 litres of milk")
    assert [call["model"] for call in upstream.calls] == ["primary-model", "primary-model", "kimi-k3-1-pro"]
    assert result["model"]["id"] == result["model"]["answered_by"] == "kimi-k3-1-pro"
    assert result["model"]["primary_model"] == "primary-model"
    assert result["model"]["attempts"] == 3 and result["model"]["fallback"] is True


@pytest.mark.asyncio
async def test_failed_fallback_is_not_retried(generic_credentials):
    upstream = RecoveringHTTP(failures=3)
    with pytest.raises(ProviderError, match="temporarily unavailable"):
        await ToolProvider(provider="openai_compatible", model="primary-model", client=upstream).plan("1,000 litres of milk")
    assert len(upstream.calls) == 3


@pytest.mark.asyncio
async def test_timeout_gets_one_primary_retry_and_no_fallback(generic_credentials):
    class TimeoutOnceHTTP(RecoveringHTTP):
        async def post(self, url, *, headers, json):
            if not self.calls:
                self.calls.append(json)
                raise TimeoutError("offline timeout fixture")
            return await super().post(url, headers=headers, json=json)

    upstream = TimeoutOnceHTTP(failures=0)
    result = await ToolProvider(provider="openai_compatible", model="primary-model", client=upstream).plan("1,000 litres of milk")
    assert [call["model"] for call in upstream.calls] == ["primary-model", "primary-model"]
    assert result["model"]["answered_by"] == "primary-model"
    assert result["model"]["attempts"] == 2 and "fallback" not in result["model"]


class Clock:
    value = 0

    def __call__(self):
        return self.value


@pytest.mark.asyncio
async def test_runtime_warms_at_start_and_no_more_than_once_per_idle_interval(tmp_path, generic_credentials, monkeypatch):
    monkeypatch.setenv("MODEL_DAILY_CAP", "20")
    upstream = RecoveringHTTP(failures=0)
    provider = ToolProvider(provider="openai_compatible", model="primary-model", client=upstream)
    clock = Clock()
    runtime = BotRuntime(tmp_path, clock=clock, provider=provider)
    await runtime.tick()
    assert len(upstream.calls) == 1 and upstream.calls[0]["max_tokens"] == 8
    assert runtime.status == {"state": "ready", "model": {"provider": "openai_compatible", "id": "primary-model"}}
    clock.value = 299
    await runtime.tick()
    assert len(upstream.calls) == 1
    runtime.admit_request()
    clock.value = 300
    await runtime.tick()
    assert len(upstream.calls) == 1, "Recent real bot activity defers an idle warm-up."
    clock.value = 599
    await runtime.tick()
    assert len(upstream.calls) == 2
    assert all("tools" not in call for call in upstream.calls)
