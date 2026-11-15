from __future__ import annotations

import ast
import asyncio
from datetime import datetime
import json
import time

import httpx
import pytest

from conftest import ROOT, assert_live_model, auth, buyer, human_auth, matching, need, order, verify

PROMPT = "I need 1,000 litres of lactose-free milk by Monday."


def inject_http(client, events=None):
    async def record(request):
        if events is not None:
            events.append((request.method, request.url.path))
    connection = httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver", event_hooks={"request": [record]})
    client.app.state.bot_http_client = connection
    return connection


@pytest.mark.live
def test_G01(live):
    assert live.post("/demo/reset", headers=human_auth(live)).status_code == 200
    started = time.monotonic()
    response = live.post("/bot/chat", json={"customer_id": "hotel", "message": PROMPT})
    elapsed = time.monotonic() - started
    assert response.status_code == 200, response.text
    result = response.json()
    assert elapsed < 90
    assert_live_model(result)
    assert result["order"]["status"] == "committed" and result["order"]["tier"] == "Commit"
    assert result["order"]["receipt"] and all(check["passed"] for check in result["order"]["checks"])
    receipt = verify(result["order"]["receipt"], live.get("/.well-known/jwks.json").json())
    assert "mandate" in receipt["approved_by"] and receipt["record_hash"]
    assert result["need"]["quantity"] == 1000 and result["need"]["unit"] == "L" and "lactose-free" in result["need"]["properties"]
    assert all(line["price_source"] == "Valio mock API" for option in result["match"]["options"] for line in option["lines"])


def test_G02(client):
    connection = inject_http(client)
    try:
        preview = client.post("/bot/chat", json={"customer_id": "hotel", "message": PROMPT, "stage": "preview"})
        assert preview.status_code == 200, preview.text
        result = preview.json()
        assert "1,000" in result["restatement"] or "1000" in result["restatement"]
        assert "lactose-free" in result["restatement"] and result["need"]["deadline"] in result["restatement"]
        assert "litres" in result["restatement"].lower()
        with client.app.state.service.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        timestamp = datetime.fromisoformat(result["messages"][0]["at"])
        completed = client.post("/bot/chat", json={"customer_id": "hotel", "message": PROMPT, "stage": "execute", "need": result["need"], "restatement": result["restatement"]})
        assert completed.status_code == 200, completed.text
        actual = completed.json()["order"]
        assert actual["status"] == "committed" and timestamp < datetime.fromisoformat(actual["created_at"])
        source = (ROOT / "frontend/src/App.tsx").read_text()
        assert "stage: 'preview'" in source and "stage: 'execute'" in source
        assert source.index("stage: 'preview'") < source.index("stage: 'execute'")
    finally:
        asyncio.run(connection.aclose())


@pytest.mark.live
def test_G03(live):
    # Verify the limited-stock feature before spending an act-2 model call.
    limited = matching(live, request_need=need(simulate_limited_stock=True))
    assert len(limited["options"]) == 3
    assert any(option.get("stock_shortfall") or option.get("partial") or any(line.get("simulated_stock") for line in option["lines"]) for option in limited["options"]), "Limited-stock matching has not been implemented."
    result = live.post("/bot/chat", json={"customer_id": "hotel", "message": PROMPT, "simulate_limited_stock": True})
    assert result.status_code == 200, result.text
    payload = result.json()
    assert_live_model(payload)
    held = payload["order"]
    assert held["status"] == "requires_escalation" and held["continue_url"] and held["reserved"]
    page = live.get(held["continue_url"])
    assert page.status_code == 200 and '<div id="root"' in page.text
    assert any(not check["passed"] and check["reason"] for check in held["checks"])
    approved = live.post(f"/orders/{held['id']}/signoff", json={"action": "approve", "confirmed": True}, headers=human_auth(live))
    assert approved.status_code == 200 and approved.json()["status"] == "committed"
    verify(approved.json()["receipt"], live.get("/.well-known/jwks.json").json())


def test_G04(client):
    page = client.get("/")
    assert page.status_code == 200 and '<div id="root"' in page.text
    source = (ROOT / "frontend/src/App.tsx").read_text()
    built = "\n".join(path.read_text() for path in (ROOT / "frontend/dist/assets").glob("*.js"))
    for text in ("Chat", "Door's answer", "Sign-off", "Limits view", "Record", "customer-select", "Real data", "Simulated operations"):
        assert text in source and text in built, text
    assert "reset" in source.lower() and "/demo/reset" in source
    customers = client.get("/demo/customers").json()["customers"]
    assert {row["id"] for row in customers} == {"hotel", "restaurant"}
    original = {row["id"]: row["mandate_payload"]["limits"] for row in customers}
    made = order(client)
    assert client.app.state.service.reservations()
    reset = client.post("/demo/reset", headers=human_auth(client))
    assert reset.status_code == 200 and reset.json()["status"] == "reset"
    assert client.app.state.service.reservations() == {}
    assert client.get(f"/orders/{made['id']}", headers=human_auth(client)).status_code == 404
    assert {row["id"]: row["mandate_payload"]["limits"] for row in client.get("/demo/customers").json()["customers"]} == original


def test_G05(client):
    files = list((ROOT / "reference_bot").glob("*.py"))
    assert files
    for path in files:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                modules = []
            assert not any(module == "door" or module.startswith("door.") or module.split(".")[0] in {"sqlite3", "sqlalchemy", "aiosqlite"} for module in modules), path.name
            if isinstance(node, ast.Call):
                assert not (isinstance(node.func, ast.Name) and node.func.id == "open"), "The reference bot must not open the database or internal files."
    calls = []
    connection = inject_http(client, calls)
    try:
        response = client.post("/bot/chat", json={"customer_id": "hotel", "message": PROMPT})
        assert response.status_code == 200 and response.json()["order"]["status"] == "committed"
        assert calls == [("GET", "/demo/customers"), ("POST", "/needs/match"), ("POST", "/offers"), ("POST", "/orders")]
    finally:
        asyncio.run(connection.aclose())


def test_G06(client):
    from conftest import held_for_preference
    _, held = held_for_preference(client)
    assert held["question_spec"], "The door must return a question specification before testing the interview."
    connection = inject_http(client)
    try:
        question = client.post("/bot/chat", json={"customer_id": "hotel", "stage": "interview", "order_id": held["id"], "question_spec": held["question_spec"]})
        assert question.status_code == 200
        assert question.json()["question"] and question.json()["messages"]
        answer = client.post("/bot/chat", json={"customer_id": "hotel", "stage": "answer", "order_id": held["id"], "message": "Use that brand just this once", "scope": "just_this_once"})
        assert answer.status_code == 200 and answer.json()["confirmation_summary"]
        assert client.get(f"/orders/{held['id']}", headers=human_auth(client)).json()["status"] == "requires_escalation"
        confirmed = client.post("/bot/chat", json={"customer_id": "hotel", "stage": "confirm", "order_id": held["id"], "confirmed": True}, headers=human_auth(client))
        assert confirmed.status_code == 200 and confirmed.json()["order"]["status"] == "committed"
    finally:
        asyncio.run(connection.aclose())


@pytest.mark.live
def test_G07(live):
    assert live.post("/demo/reset", headers=human_auth(live)).status_code == 200
    baseline = live.get("/usual-order", params={"customer_id": "restaurant"}, headers=human_auth(live, "restaurant")).json()
    assert len(baseline["history"]) == 12
    limits = live.get("/limits", params={"customer_id": "restaurant"}, headers=human_auth(live, "restaurant")).json()
    assert len(limits["proposals"]) == 1 and limits["proposals"][0]["evidence"]
    before = next(row for row in limits["rows"] if row["id"] == "departure")
    response = live.post("/bot/chat", json={"customer_id": "restaurant", "message": "Same as last week, plus 5 litres more milk."})
    assert response.status_code == 200, response.text
    result = response.json()
    assert_live_model(result)
    made = result["order"]
    assert made["status"] == "committed" and made["tier"] == "Notify"
    assert "undo" in made["notice"].lower() and "approval by silence" in made["notice"].lower()
    withdrawn = live.post(f"/orders/{made['id']}/undo", headers=human_auth(live, "restaurant"))
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "withdrawn"
    after = next(row for row in live.get("/limits", params={"customer_id": "restaurant"}, headers=human_auth(live, "restaurant")).json()["rows"] if row["id"] == "departure")
    assert after["current_freedom"] < before["current_freedom"] and after["reason"] != before["reason"]
