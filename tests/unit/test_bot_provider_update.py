"""Offline checks for the advisor's Verda, Finnish and deadline hardening update."""
from datetime import date, datetime
import json

from fastapi.testclient import TestClient
import httpx
import pytest

import reference_bot.providers as providers
from reference_bot.providers import ClarificationRequired, EMPTY_TOOL_QUESTION, FixtureProvider, ProviderError, ToolProvider, calendar_context, validate_basket, validate_plan, validate_reorder


FINNISH_REQUEST = "Tarvitsen 1 000 litraa laktoositonta maitoa tiistaiksi."
TODAY = date(2026, 11, 14)


def purchase(deadline="2026-11-17"):
    return {"intent": "new_order", "need": {"product_type": "drinking milk", "properties": ["lactose-free"], "quantity": 1000, "unit": "L", "deadline": deadline, "preferences": {}}, "restatement": "Etsin pyytämäsi maidon toimitettavaksi tiistaina."}


class StubHTTP:
    def __init__(self, result=None, *, handler=None):
        self.result = result if result is not None else purchase()
        self.handler = handler
        self.calls = []
        self.transport = httpx.MockTransport(self.respond)

    def respond(self, request):
        self.calls.append(request)
        body = json.loads(request.content)
        if self.handler:
            return self.handler(request, body)
        return httpx.Response(200, json={"model": body["model"], "choices": [{"message": {"content": None, "tool_calls": [{"type": "function", "function": {"name": body["tools"][0]["function"]["name"], "arguments": json.dumps(self.result)}}]}}]})

    async def post(self, url, *, headers, json):
        return await self.transport.handle_async_request(httpx.Request("POST", url, headers=headers, json=json))

    async def get(self, *args, **kwargs):
        pytest.fail("The generic provider must use the configured model without a discovery request.")


@pytest.fixture
def generic_config(monkeypatch):
    monkeypatch.delenv("AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("AGENT_BASE_URL", raising=False)
    monkeypatch.setenv("AGENT_API_KEY", "fixture-generic-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-anthropic-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-openrouter-key")
    monkeypatch.setattr(providers, "local_today", lambda: TODAY)


@pytest.mark.asyncio
async def test_default_generic_verda_provider_uses_only_its_key_and_finnish_tool_request(generic_config):
    upstream = StubHTTP()
    result = await ToolProvider(client=upstream).plan(FINNISH_REQUEST)
    assert len(upstream.calls) == 1
    request = upstream.calls[0]
    assert str(request.url) == "https://inference.verda.com/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer fixture-generic-key"
    assert "x-api-key" not in request.headers and "fixture-anthropic-key" not in str(request.headers)
    body = json.loads(request.content)
    assert body["model"] == "deepseek-4-1-flash-1017" and body["max_tokens"] == 800
    assert body["tool_choice"] == {"type": "function", "function": {"name": "match_need"}}
    assert body["messages"][1]["content"] == FINNISH_REQUEST
    assert "laktoositon" in body["messages"][0]["content"]
    assert "2026-11-17 | Tuesday | tiistai" in body["messages"][0]["content"]
    assert result["need"]["quantity"] == 1000 and result["need"]["properties"] == ["lactose-free"]
    assert result["need"]["deadline"] == "2026-11-17" and result["model"]["provider"] == "openai_compatible"


@pytest.mark.asyncio
async def test_generic_openrouter_base_and_explicit_model_are_supported(generic_config, monkeypatch):
    monkeypatch.setenv("AGENT_BASE_URL", "https://openrouter.ai/api/v1/")
    monkeypatch.setenv("AGENT_MODEL", "deepseek/fixture-flash")
    upstream = StubHTTP()
    result = await ToolProvider(provider="openai_compatible", client=upstream).plan(FINNISH_REQUEST)
    request = upstream.calls[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer fixture-generic-key"
    assert json.loads(request.content)["model"] == "deepseek/fixture-flash"
    assert result["model"]["id"] == "deepseek/fixture-flash" and len(upstream.calls) == 1


def test_calendar_has_exactly_fourteen_upcoming_bilingual_days(generic_config):
    table = calendar_context()
    rows = [line for line in table.splitlines() if line[:4].isdigit()]
    assert len(rows) == 14
    assert rows[0] == "2026-11-15 | Sunday | sunnuntai"
    assert rows[-1] == "2026-11-28 | Saturday | lauantai"
    assert "2026-11-14 (Saturday / lauantai)" in table
    for english, finnish in zip(("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"), ("maanantai", "tiistai", "keskiviikko", "torstai", "perjantai", "lauantai", "sunnuntai")):
        assert english in table and finnish in table


@pytest.mark.parametrize("content", [None, "", "   "])
@pytest.mark.asyncio
async def test_kimi_empty_response_asks_fixed_question_once_without_retry(generic_config, content):
    def response(request, body):
        assert body["model"] == "kimi-k3-1-pro" and body["max_tokens"] == 800
        return httpx.Response(200, json={"choices": [{"message": {"content": content, "tool_calls": []}}]})
    upstream = StubHTTP(handler=response)
    with pytest.raises(ClarificationRequired) as error:
        await ToolProvider(model="kimi-k3-1-pro", client=upstream).plan(FINNISH_REQUEST)
    assert error.value.question == EMPTY_TOOL_QUESTION
    assert error.value.reason == "no_tool_call" and len(upstream.calls) == 1
    assert error.value.model["id"] == "kimi-k3-1-pro"


@pytest.mark.asyncio
async def test_nonempty_model_clarification_is_returned_without_echoing_configured_key(generic_config):
    def response(request, body):
        return httpx.Response(200, json={"choices": [{"message": {"content": "Mille toimituspäivälle tarvitset maidon? fixture-generic-key"}}]})
    upstream = StubHTTP(handler=response)
    with pytest.raises(ClarificationRequired) as error:
        await ToolProvider(client=upstream).plan(FINNISH_REQUEST)
    assert "Mille toimituspäivälle" in error.value.question
    assert "fixture-generic-key" not in error.value.question and len(upstream.calls) == 1


@pytest.mark.parametrize("kind", ["new", "reorder", "basket"])
def test_every_plan_shape_rejects_past_deadlines_in_code(generic_config, kind):
    with pytest.raises(ClarificationRequired) as error:
        if kind == "new":
            validate_plan(purchase("2026-11-13"), FINNISH_REQUEST)
        elif kind == "reorder":
            validate_reorder({"reorder": {"deadline": "2026-11-13", "adjustments": []}}, "Sama kuin viime viikolla")
        else:
            validate_basket({"deadline": "2026-11-13", "anchor_id": "fixed-anchor", "lines": [{"product_id": "milk", "quantity": 30, "unit": "L", "properties": ["lactose-free"]}]}, "Sama kuin viime viikolla")
    assert error.value.reason == "past_deadline"
    assert "2026-11-13" in error.value.question and "menneisyydessä" in error.value.question


def test_deadline_boundary_uses_helsinki_calendar(monkeypatch):
    class Clock:
        @staticmethod
        def now(zone):
            assert zone.key == "Europe/Helsinki"
            return datetime(2026, 11, 15, 0, 30, tzinfo=zone)
    monkeypatch.setattr(providers, "datetime", Clock)
    assert providers.local_today() == date(2026, 11, 15)
    with pytest.raises(ClarificationRequired):
        providers.validated_deadline("2026-11-14")
    assert providers.validated_deadline("2026-11-15") == "2026-11-15"


@pytest.mark.asyncio
async def test_finnish_fixture_supports_quantity_property_weekday_and_reorder(generic_config):
    result = await FixtureProvider().plan(FINNISH_REQUEST)
    assert result["need"]["quantity"] == 1000
    assert result["need"]["properties"] == ["lactose-free"]
    assert result["need"]["deadline"] == "2026-11-17"
    reorder = await FixtureProvider().plan("Sama kuin viime viikolla, lisäksi 5 litraa enemmän maitoa.")
    assert reorder["intent"] == "reorder"
    assert reorder["reorder"]["adjustments"] == [{"product_type": "drinking milk", "quantity": 5, "unit": "L"}]


@pytest.mark.parametrize("stage", ["preview", "all", "execute"])
def test_past_date_http_response_is_question_and_never_reaches_door(tmp_path, generic_config, stage):
    from door.main import create_app
    upstream = StubHTTP(purchase("2026-11-13"))
    with TestClient(create_app(tmp_path, fixture=True)) as client:
        client.app.state.bot_provider = ToolProvider(provider="openai_compatible", client=upstream)
        class ForbiddenDoor:
            async def request(self, *args, **kwargs):
                pytest.fail("A past-date plan must not reach any door endpoint.")
        client.app.state.bot_http_client = ForbiddenDoor()
        body = {"stage": stage, "customer_id": "hotel", "message": FINNISH_REQUEST}
        if stage == "execute":
            body["need"] = purchase("2026-11-13")["need"]
        response = client.post("/bot/chat", json=body)
        assert response.status_code == 200
        result = response.json()
        assert result["stage"] == "clarification" and result["status"] == "requires_clarification"
        assert result["reason"] == "past_deadline" and result["question"] == result["clarification"]
        assert result["need"] is None and result["order"] is None and result["messages"][0]["kind"] == "question"
        assert len(upstream.calls) == (0 if stage == "execute" else 1)
        with client.app.state.service.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_empty_model_http_response_never_reaches_door(tmp_path, generic_config):
    from door.main import create_app
    upstream = StubHTTP(handler=lambda request, body: httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": []}}]}))
    with TestClient(create_app(tmp_path, fixture=True)) as client:
        client.app.state.bot_provider = ToolProvider(provider="openai_compatible", client=upstream)
        class ForbiddenDoor:
            async def request(self, *args, **kwargs):
                pytest.fail("An empty model response must not reach the door.")
        client.app.state.bot_http_client = ForbiddenDoor()
        response = client.post("/bot/chat", json={"customer_id": "hotel", "message": FINNISH_REQUEST})
        assert response.status_code == 200 and response.json()["question"] == EMPTY_TOOL_QUESTION
        assert response.json()["order"] is None and len(upstream.calls) == 1


@pytest.mark.asyncio
async def test_calendar_and_output_cap_apply_to_interview_model_calls(generic_config):
    spec = {"needed": "Choose the milk brand", "answers": [{"value": "approve", "cost": {"price": 10}}, {"value": "reject", "cost": {"price": 0}}], "scope_options": ["just_this_once", "always", "always_ask"]}
    def response(request, body):
        name = body["tools"][0]["function"]["name"]
        arguments = {"question": "Saako tarjottua maitomerkkiä käyttää?"} if name == "ask_buyer" else {"answer": "approve", "scope": "just_this_once", "clarification": ""}
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"function": {"name": name, "arguments": json.dumps(arguments)}}]}}]})
    upstream = StubHTTP(handler=response)
    provider = ToolProvider(provider="openai_compatible", client=upstream)
    await provider.interview(spec)
    answer = await provider.interview(spec, "Käytä sitä vain tällä kertaa", "just_this_once")
    assert answer["answer"] == "approve"
    assert len(upstream.calls) == 2
    for request in upstream.calls:
        body = json.loads(request.content)
        assert body["max_tokens"] == 800
        assert "2026-11-17 | Tuesday | tiistai" in body["messages"][0]["content"]
        assert "2026-11-28 | Saturday | lauantai" in body["messages"][0]["content"]


@pytest.mark.asyncio
async def test_generic_network_failure_uses_bounded_primary_retry_then_default_fallback(generic_config):
    def failure(request, body):
        raise httpx.ConnectError("offline fixture")
    upstream = StubHTTP(handler=failure)
    with pytest.raises(ProviderError, match="unavailable"):
        await ToolProvider(provider="openai_compatible", client=upstream).plan(FINNISH_REQUEST)
    assert len(upstream.calls) == 3
    assert [json.loads(request.content)["model"] for request in upstream.calls] == [
        "deepseek-4-1-flash-1017", "deepseek-4-1-flash-1017", "kimi-k3-1-pro",
    ]
