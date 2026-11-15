"""Engineering-owned milestone 4 acceptance cases J05-J15."""
from __future__ import annotations

from datetime import timedelta
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess

import httpx
import pytest

from conftest import ROOT, assert_live_model, auth, buyer, human_auth, matching, need, order, verify
from door.bot_runtime import BotRuntime
from reference_bot.client import ReferenceBot
from reference_bot.providers import EMPTY_TOOL_QUESTION, ToolProvider, local_today, next_monday


def test_J05(live):
    # Read the running server's licensed index without any upstream/model call.
    first = live.get("/usual-order", params={"customer_id": "restaurant"}, headers=human_auth(live, "restaurant")).json()
    assert first["synthetic"] is True and "synthetic" in first["label"].lower()
    assert 12 <= len(first["lines"]) <= 15
    assert len({line["id"] for line in first["lines"]}) == len(first["lines"])
    index = Path(os.getenv("DATA_DIR", ROOT / "data")) / "catalog.sqlite3"
    with sqlite3.connect(f"file:{index.resolve()}?mode=ro", uri=True) as db:
        assert db.execute("SELECT COUNT(*) FROM catalog").fetchone()[0] == 17546
        categories = set()
        for line in first["lines"]:
            row = db.execute("SELECT data FROM catalog WHERE id=?", (str(line["id"]),)).fetchone()
            assert row is not None, "Every usual-order line must exist in the real licensed catalog."
            product = json.loads(row[0])
            assert line["category"] == product["category"]
            categories.add(product["category"])
    assert len(categories) >= 5
    assert live.post("/demo/reset", headers=human_auth(live)).status_code == 200
    second = live.get("/usual-order", params={"customer_id": "restaurant"}, headers=human_auth(live, "restaurant")).json()
    assert second["lines"] == first["lines"] and second["history"] == first["history"]
    assert second["anchor_id"] == first["anchor_id"]
    assert len(live.get("/limits", params={"customer_id": "restaurant"}, headers=human_auth(live, "restaurant")).json()["proposals"]) == 1


def test_J06(client):
    hotel = buyer(client, "hotel")["mandate_payload"]["limits"]
    restaurant = buyer(client, "restaurant")["mandate_payload"]["limits"]
    assert restaurant["per_order_cap"] == 400 < hotel["per_order_cap"]
    assert restaurant["monthly_budget"] == 6000 < hotel["monthly_budget"]


def test_J07(client):
    before = client.get("/limits", params={"customer_id": "hotel"}, headers=human_auth(client)).json()
    old = next(row for row in before["rows"] if row["id"] == "monthly_budget")
    assert isinstance(old["used"], (int, float)) and not isinstance(old["used"], bool)
    assert isinstance(old["remaining"], (int, float)) and not isinstance(old["remaining"], bool)
    assert old["used"] >= 0 and old["remaining"] >= 0
    made = order(client)
    assert made["status"] == "committed"
    after = client.get("/limits", params={"customer_id": "hotel"}, headers=human_auth(client)).json()
    current = next(row for row in after["rows"] if row["id"] == "monthly_budget")
    assert current["used"] > old["used"]
    assert current["remaining"] < old["remaining"]
    assert current["used"] + current["remaining"] == pytest.approx(current["signed_ceiling"], abs=0.01)


def test_J08(client):
    result = matching(client, request_need=need(simulate_limited_stock=True))
    document = client.post("/offers", json={**auth(client), "option": result["options"][0]}).json()
    held = order(client, document=document)
    assert held["status"] == "requires_escalation"
    reason = " ".join(check["reason"] for check in held["checks"] if not check["passed"])
    shortage = re.search(r"stock (?:is )?short|short(?:age|fall)(?: of| in)? stock|insufficient stock", reason, re.I)
    assert shortage, "The held order must first explain that stock is short."
    late_dates = sorted({line["delivery_date"][:10] for line in held["offer_payload"]["lines"] if line["delivery_date"][:10] > held["offer_payload"]["need"]["deadline"]})
    assert late_dates
    assert any(day in reason and shortage.start() < reason.index(day) for day in late_dates), "State the stock shortage before the actual later delivery date."


@pytest.mark.live
def test_J09(live):
    comparable = []
    for message in ("I need 1,000 litres of lactose-free milk by Monday.", "Tarvitsen 1000 litraa laktoositonta maitoa maanantaiksi."):
        assert live.post("/demo/reset", headers=human_auth(live)).status_code == 200
        response = live.post("/bot/chat", json={"customer_id": "hotel", "message": message})
        assert response.status_code == 200, response.text
        result = response.json()
        assert_live_model(result)
        assert result["order"]["status"] == "committed" and result["order"]["receipt"]
        assert all(check["passed"] for check in result["order"]["checks"])
        verify(result["order"]["receipt"], live.get("/.well-known/jwks.json").json())
        normalized = result["need"]
        assert normalized["quantity"] == 1000 and normalized["unit"] == "L"
        assert normalized["product_type"] == "drinking milk" and normalized["properties"] == ["lactose-free"]
        assert normalized["deadline"] == next_monday().isoformat()
        comparable.append(sorted((line["id"], line["packs"], line["quantity"], line["delivery_date"], line["unit_price"]) for line in result["order"]["offer_payload"]["lines"]))
    assert comparable[0] == comparable[1], "The Finnish request must produce the same product/pack/date/price order as English."


class StubProviderHTTP:
    def __init__(self, *, arguments=None, silent=False):
        self.calls = 0
        self.arguments, self.silent = arguments, silent
        self.transport = httpx.MockTransport(self.respond)

    def respond(self, request):
        self.calls += 1
        body = json.loads(request.content)
        message = {"content": "", "tool_calls": []} if self.silent else {"tool_calls": [{"function": {"name": "match_need", "arguments": json.dumps(self.arguments)}}]}
        return httpx.Response(200, json={"model": body["model"], "choices": [{"message": message}]})

    async def post(self, url, *, headers, json):
        return await self.transport.handle_async_request(httpx.Request("POST", url, headers=headers, json=json))


class NoPurchasingCalls:
    async def request(self, *args, **kwargs):
        pytest.fail("A clarification must make no purchasing HTTP call.")


def install_stub(client, monkeypatch, upstream):
    monkeypatch.setenv("AGENT_API_KEY", "acceptance-fixture-key")
    client.app.state.bot_provider = ToolProvider(provider="openai_compatible", model="fixture-model", client=upstream)
    client.app.state.bot_http_client = NoPurchasingCalls()


def test_J10(client, monkeypatch):
    past = (local_today() - timedelta(days=1)).isoformat()
    upstream = StubProviderHTTP(arguments={"intent": "new_order", "need": need(deadline=past)})
    install_stub(client, monkeypatch, upstream)
    result = client.post("/bot/chat", json={"customer_id": "hotel", "message": "Milk for yesterday"})
    assert result.status_code == 200
    body = result.json()
    assert body["stage"] == "clarification" and body["reason"] == "past_deadline"
    assert body["question"] and past in body["question"] and body["order"] is None
    assert upstream.calls == 1
    with client.app.state.service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_J11(client, monkeypatch):
    upstream = StubProviderHTTP(silent=True)
    install_stub(client, monkeypatch, upstream)
    response = client.post("/bot/chat", json={"customer_id": "hotel", "message": "Please help me order"})
    assert response.status_code == 200
    result = response.json()
    assert result["stage"] == "clarification" and result["question"] == EMPTY_TOOL_QUESTION
    assert result["order"] is None and upstream.calls == 1
    with client.app.state.service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


@pytest.mark.live
def test_J12(live):
    for scope in ("just_this_once", "always"):
        assert live.post("/demo/reset", headers=human_auth(live)).status_code == 200
        request_need = need(quantity=10, preferences={"required": {"brand": "J12 preferred brand"}})
        held = order(live, request_need=request_need)
        assert held["status"] == "requires_escalation" and held["question_spec"]["kind"] == "preference"
        assert held["checks"][3]["passed"] and held["checks"][4]["passed"]
        before = buyer(live)
        base = {"customer_id": "hotel", "order_id": held["id"]}
        question = live.post("/bot/chat", json={**base, "stage": "interview"})
        assert question.status_code == 200, question.text
        assert_live_model(question.json())
        assert question.json()["question"].strip()
        assert question.json()["question_spec"]["hash"] == held["question_spec"]["hash"]
        message = "I approve the offered brand just this once." if scope == "just_this_once" else "Always accept this offered brand for drinking milk."
        answer = live.post("/bot/chat", json={**base, "stage": "answer", "scope": scope, "message": message})
        assert answer.status_code == 200, answer.text
        prepared = answer.json()
        assert_live_model(prepared)
        pending = prepared["pending_answer"]
        assert pending["answer"] == "approve" and pending["scope"] == scope
        assert prepared["confirmation_summary"]
        assert live.get(f"/orders/{held['id']}", headers=human_auth(live)).json()["status"] == "requires_escalation"
        assert buyer(live)["mandate"] == before["mandate"]
        confirmed = live.post("/bot/chat", json={**base, "stage": "confirm", "confirmed": True, "confirmation_summary": prepared["confirmation_summary"], "question_hash": pending["question_hash"]}, headers=human_auth(live))
        assert confirmed.status_code == 200, confirmed.text
        made = confirmed.json()["order"]
        assert made["status"] == "committed" and made["receipt"]
        verify(made["receipt"], live.get("/.well-known/jwks.json").json())
        after = buyer(live)
        if scope == "just_this_once":
            assert after["mandate"] == before["mandate"]
        else:
            signed = verify(after["mandate"], live.get("/demo/buyer-jwks").json())
            assert signed["version"] > before["mandate_payload"]["version"]
            assert signed["limits"] == before["mandate_payload"]["limits"]
            assert any(entry["scope"] == "always" and entry["product_type"] == "drinking milk" and entry["answer"] == "approve" for entry in signed["learned_preferences"])


def test_J13():
    path = ROOT / "docker-compose.caddy.yml"
    assert path.is_file()
    env = dict(os.environ, PROXY_NETWORK="acceptance-door-proxy")
    result = subprocess.run(["docker", "compose", "-f", str(path), "config", "--format", "json", "--no-env-resolution"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, "The Caddy compose file must pass docker compose config (output withheld to protect environment values)."
    document = json.loads(result.stdout)
    service = document["services"]["door"]
    assert not service.get("ports"), "Caddy reaches the container network; no host port may be published."
    networks = document["networks"]
    attached = service["networks"]
    assert any(name in attached and item.get("external") and item.get("name") == "acceptance-door-proxy" for name, item in networks.items())
    assert "default" in attached and not networks["default"].get("external", False)
    readme = (ROOT / "README.md").read_text() + "\n\n" + (ROOT / "docs/SETUP.md").read_text()
    assert "@door host door.{$DOMAIN}" in readme
    assert re.search(r"handle\s+@door\s*\{\s*reverse_proxy\s+door:8000\s*\}", readme)
    assert "PROXY_NETWORK" in readme


def test_J14(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from door.main import create_app
    monkeypatch.setenv("DEMO_RATE_LIMIT", "10000")
    monkeypatch.setenv("DEMO_ACCESS_CODE", "")
    monkeypatch.setenv("AGENT_WARMUP_ENABLED", "0")
    # Isolate J14's daily model cap from K03's separately tested chat bucket.
    monkeypatch.setenv("BOT_CHAT_RATE_LIMIT", "1000")
    with TestClient(create_app(tmp_path, fixture=True)) as client:
        upstream = StubProviderHTTP(arguments={"intent": "new_order", "need": need(quantity=10)})
        install_stub(client, monkeypatch, upstream)
        client.app.state.service.fixture = False
        monkeypatch.delenv("MODEL_DAILY_CAP", raising=False)
        for _ in range(300):
            response = client.post("/bot/chat", json={"stage": "preview", "customer_id": "hotel", "message": "10 litres of lactose-free milk by Monday"})
            assert response.status_code == 200, "All 300 admitted calls must reach the stub provider."
        assert upstream.calls == 300
        blocked = client.post("/bot/chat", json={"stage": "preview", "customer_id": "hotel", "message": "10 litres of lactose-free milk by Monday"})
        assert blocked.status_code == 429 and upstream.calls == 300
        message = json.dumps(blocked.json()).lower()
        assert "daily" in message and "cap" in message


class ColdModelHTTP:
    def __init__(self):
        self.calls = []
        self.bot_calls = 0

    async def post(self, url, *, headers, json):
        self.calls.append(json)
        if "tools" not in json:
            return httpx.Response(200, json={"model": json["model"], "choices": [{"message": {"content": "OK"}}]})
        self.bot_calls += 1
        if self.bot_calls <= 2:
            return httpx.Response(503, json={"error": {"message": "cold model"}})
        arguments = {
            "intent": "new_order",
            "need": {"product_type": "drinking milk", "properties": ["lactose-free"], "quantity": 1000, "unit": "L", "deadline": "2099-01-04", "preferences": {}},
            "restatement": "I will order 1,000 litres of lactose-free milk by 2099-01-04.",
        }
        return httpx.Response(200, json={"model": json["model"], "choices": [{"message": {"tool_calls": [{"function": {"name": "match_need", "arguments": globals()["json"].dumps(arguments)}}]}}]})


@pytest.mark.asyncio
async def test_J15(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "acceptance-cold-model-key")
    monkeypatch.delenv("AGENT_FALLBACK_MODEL", raising=False)
    monkeypatch.setenv("MODEL_DAILY_CAP", "10")
    upstream = ColdModelHTTP()
    provider = ToolProvider(provider="openai_compatible", model="production-flash", client=upstream)
    now = 0
    runtime = BotRuntime(tmp_path, clock=lambda: now, provider=provider)
    await runtime.tick()
    assert len(upstream.calls) == 1 and upstream.calls[0]["max_tokens"] == 8
    now = 299
    await runtime.tick()
    assert len(upstream.calls) == 1
    now = 300
    preview = await ReferenceBot(client=None, provider=provider).preview("1,000 litres of lactose-free milk")
    assert [call["model"] for call in upstream.calls[1:]] == ["production-flash", "production-flash", "kimi-k3-1-pro"]
    assert all(call["max_tokens"] == 800 for call in upstream.calls[1:])
    assert preview["model"]["id"] == preview["model"]["answered_by"] == "kimi-k3-1-pro"
    assert preview["model"]["attempts"] == 3 and preview["model"]["fallback"] is True
    assert len(upstream.calls) == 4, "One warm-up, two primary attempts and exactly one fallback; no loops."
    await runtime.tick()
    assert len(upstream.calls) == 4, "Real bot activity suppresses the next idle warm-up."
