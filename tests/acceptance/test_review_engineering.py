"""Engineering-owned acceptance cases K01–K13 from the independent review."""
from __future__ import annotations

import copy
import inspect
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import auth, buyer, fixture_offer, human_auth, matching, need, new_mandate, offer as make_offer, order as place_order, read_auth
from door.bot_runtime import BotRuntime
from door.catalog import CATALOG_INDEX_VERSION, Catalog, normalize_product
from door.decisions import DecisionLabeler
from door.history import HistorySeedError
from door.main import create_app
from door.security import digest
from reference_bot.client import ReferenceBot
from reference_bot.providers import ToolProvider


def test_K01(client):
    """Every supported spelling follows the same canonical property path."""
    authority = auth(client)
    spellings = ("lactose-free", "lactose free", "Lactose-Free", "lactose_free", "FREE_FROM_LACTOSE")
    results = [matching(client, authority, need(properties=[spelling])) for spelling in spellings]
    def same_product_offer(option):
        return [(line["id"], line["packs"], line["quantity"], line["total"], line["missing_properties"]) for line in option["lines"]]

    baseline = same_product_offer(results[0]["options"][0])
    documents = []
    for result in results:
        assert result["options"], "A supported spelling must still produce an option."
        option = result["options"][0]
        assert result["need"]["properties"] == ["lactose-free"]
        assert same_product_offer(option) == baseline
        response = client.post("/offers", json={**authority, "option": option})
        assert response.status_code == 200, response.text
        documents.append(response.json())
    for document in documents[1:]:
        assert same_product_offer(document["payload"]) == same_product_offer(documents[0]["payload"])
        assert document["payload"]["need"] == documents[0]["payload"]["need"]
        assert document["payload"]["total"] == documents[0]["payload"]["total"]
    for document in documents:
        response = client.post(
            "/orders",
            json={**authority, "offer": document["offer"]},
            headers={"Idempotency-Key": f"K01-{uuid.uuid4()}"},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "committed", result["checks"][3]
        assert result["checks"][3]["passed"], result["checks"][3]


def test_K02(client, monkeypatch, tmp_path):
    """Provider credit/rate failures and the local cap use the rules label."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-test-key")
    service = client.app.state.service
    for scenario, provider_status in (("credit", 402), ("rate", 429), ("daily-cap", None)):
        calls = []

        async def provider(self, url, **kwargs):
            calls.append(url)
            return httpx.Response(provider_status, request=httpx.Request("POST", url))

        async def no_provider(*args, **kwargs):
            calls.append("unexpected")
            raise AssertionError("A capped decision call must not reach the provider.")

        monkeypatch.setattr(
            httpx.AsyncClient,
            "post",
            provider if provider_status is not None else no_provider,
        )
        monkeypatch.setenv("MODEL_DAILY_CAP", "0" if provider_status is None else "300")
        service.matcher.decision_labeler = DecisionLabeler(tmp_path / scenario, enabled=True)
        response = client.post("/needs/match", json={**auth(client), "need": need(quantity=10)})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["options"], scenario
        for option in result["options"]:
            for line in option["lines"]:
                assert line["product_type"] == "drinking milk"
                assert line["properties"]["lactose-free"] == "manufacturer"
                assert line["inference_method"] == "rules"
        assert bool(calls) is (provider_status is not None)


@pytest.mark.asyncio
async def test_K03(tmp_path, monkeypatch):
    """Warm-ups stay outside visitor budgets and dormant servers stop calling."""
    monkeypatch.setenv("AGENT_API_KEY", "offline-warmup-key")
    monkeypatch.setenv("MODEL_DAILY_CAP", "0")

    class WarmHTTP:
        def __init__(self):
            self.calls = []

        async def post(self, url, *, headers, json):
            self.calls.append(json)
            return httpx.Response(200, json={"model": json["model"], "choices": [{"message": {"content": "OK"}}]})

    upstream = WarmHTTP()
    provider = ToolProvider(provider="openai_compatible", model="offline-warm-model", client=upstream)
    now = [0]
    runtime = BotRuntime(tmp_path / "runtime", clock=lambda: now[0], provider=provider)
    await runtime.tick()
    for minute in (5, 10, 15, 20, 25):
        now[0] = minute * 60
        await runtime.tick()
    assert len(upstream.calls) == 6
    assert not (tmp_path / "runtime" / "model_usage.sqlite").exists(), "Warm-ups must not enter model usage accounting."

    now[0] = 30 * 60
    await runtime.tick()
    now[0] = 60 * 60
    await runtime.tick()
    assert len(upstream.calls) == 6, "There must be no warm-up after thirty idle minutes."
    monkeypatch.setenv("MODEL_DAILY_CAP", "300")
    runtime.admit_request()
    await runtime.tick()
    assert len(upstream.calls) == 7, "Real activity resumes warming."
    with sqlite3.connect(tmp_path / "runtime" / "model_usage.sqlite") as db:
        assert db.execute("SELECT SUM(calls) FROM model_usage").fetchone()[0] == 1, "Only the real request is charged."

    monkeypatch.setenv("BOT_CHAT_RATE_LIMIT", "2")
    monkeypatch.setenv("DEMO_RATE_LIMIT", "100")
    with TestClient(create_app(tmp_path / "chat-limit", fixture=True), client=("127.0.0.1", 50000)) as limited:
        first = {"X-Forwarded-For": "192.0.2.10"}
        other = {"X-Forwarded-For": "192.0.2.11"}
        body = {"stage": "preview", "customer_id": "hotel", "message": "10 litres of milk by Monday"}
        assert [limited.post("/bot/chat", headers=first, json=body).status_code for _ in range(2)] == [200, 200]
        blocked = limited.post("/bot/chat", headers=first, json=body)
        assert blocked.status_code == 429 and "bot chat" in blocked.json()["detail"]
        assert limited.post("/bot/chat", headers=other, json=body).status_code == 200

    monkeypatch.setenv("BOT_CHAT_RATE_LIMIT", "20")
    monkeypatch.setenv("DEMO_RATE_LIMIT", "1")
    with TestClient(create_app(tmp_path / "internal-limit", fixture=True), client=("127.0.0.1", 50000)) as app_client:
        address = {"X-Forwarded-For": "192.0.2.12"}
        app_client.app.state.bot_internal_token = "offline-internal-token"
        assert app_client.get("/demo/customers", headers=address).status_code == 200
        assert app_client.get("/demo/customers", headers=address).status_code == 429
        internal = {**address, "X-Door-Internal-Bot": "offline-internal-token"}
        assert app_client.get("/demo/customers", headers=internal).status_code == 200
        assert app_client.get("/demo/customers", headers=internal).status_code == 200


def test_K04(tmp_path, monkeypatch):
    """A failed restaurant quote leaves a live service and retries on demand."""
    import door.history as history_module

    original = history_module.build_history
    calls = []

    async def fail_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise HistorySeedError("Simulated unavailable restaurant quote.")
        return await original(*args, **kwargs)

    monkeypatch.setattr(history_module, "build_history", fail_once)
    with TestClient(create_app(tmp_path, fixture=True)) as client:
        health = client.get("/healthz")
        assert health.status_code == 200 and health.json()["status"] == "ok"
        assert health.json()["restaurant_history"]["state"] == "unavailable"
        assert health.json()["restaurant_history"]["seeded"] is False
        assert "retry" in health.json()["restaurant_history"]["message"].lower()

        hotel = place_order(client, request_need=need(quantity=10))
        assert hotel["status"] == "committed" and hotel["receipt"]

        usual = client.get("/usual-order", params={"customer_id": "restaurant"}, headers=human_auth(client, "restaurant"))
        assert usual.status_code == 200, usual.text
        assert 12 <= len(usual.json()["lines"]) <= 15
        assert len(usual.json()["history"]) == 12
        assert len(calls) == 2, "The first restaurant read performs one lazy retry, not a loop."
        restored = client.get("/healthz").json()["restaurant_history"]
        assert restored["state"] == "ready" and restored["seeded"] is True


def test_K05(client):
    """Mandates never substitute for a customer-bound simulated human login."""
    issued = client.post("/demo/buyer-session", json={"customer_id": "hotel"})
    assert issued.status_code == 200, issued.text
    login = issued.json()
    assert login["token"] and login["customer_id"] == "hotel" and login["expires_at"]
    assert login["simulated"] is True and "simulated" in login["label"].lower()
    human = {"X-Buyer-Session": login["token"]}
    wrong = human_auth(client, "restaurant")

    committed = place_order(client)
    assert committed["status"] == "committed"
    authority = new_mandate(client, per_order_cap=1)
    held = place_order(client, authority)
    assert held["status"] == "requires_escalation" and not held["checks"][5]["passed"]
    for body in ({"action": "approve"}, {"action": "reject"}, {"action": "edit", "quantity": 10}):
        attack = {**body, **authority, "customer_id": "hotel"}
        assert client.post(f"/orders/{held['id']}/signoff", json=attack).status_code == 401
        assert client.post(f"/orders/{held['id']}/signoff", json=attack, headers=wrong).status_code == 403
        current = client.get(f"/orders/{held['id']}", headers=read_auth(client)).json()
        assert current["status"] == "requires_escalation" and current["receipt"] is None

    assert client.post(f"/orders/{committed['id']}/undo", json=authority).status_code == 401
    assert client.post(f"/orders/{committed['id']}/undo", headers=wrong).status_code == 403
    assert client.get(f"/orders/{committed['id']}", headers=read_auth(client)).json()["status"] == "committed"

    proposal = client.get("/limits", params={"customer_id": "restaurant"}, headers=read_auth(client, "restaurant")).json()["proposals"][0]
    accept = {"customer_id": "restaurant", "confirmed": True, "mandate": "not-human-authority", "bot_id": "reference-bot"}
    endpoint = f"/limits/proposals/{proposal['id']}/accept"
    assert client.post(endpoint, json=accept).status_code == 401
    assert client.post(endpoint, json=accept, headers=human_auth(client)).status_code == 403
    reset_freedom = f"/limits/departure/reset"
    assert client.post(reset_freedom, json={"customer_id": "restaurant"}).status_code == 401
    assert client.post(reset_freedom, json={"customer_id": "restaurant"}, headers=human_auth(client)).status_code == 403

    # Register a real learned-preference entry, then prove its removal is also
    # human-only. Registration itself cannot be performed with a mandate alone.
    payload = copy.deepcopy(buyer(client)["mandate_payload"])
    payload.update(jti=str(uuid.uuid4()), version=payload["version"] + 1)
    payload["learned_preferences"] = [{
        "id": "K05-learned", "product_type": "drinking milk", "preference": "brand",
        "accepted_value": "Fixture", "scope": "always", "question": "May this brand be used?",
        "answer": "approve", "confirmation_summary": "Approve Fixture for drinking milk.",
    }]
    token = client.app.state.service.keys.sign_buyer("hotel", payload)
    assert client.post("/mandates", json={"mandate": token}).status_code == 401
    assert client.post("/mandates", json={"mandate": token}, headers=wrong).status_code == 403
    assert client.post("/mandates", json={"mandate": token}, headers=human).status_code == 200
    remove = f"/limits/K05-learned/reset"
    assert client.post(remove, json={"customer_id": "hotel", "confirmed": True}).status_code == 401
    assert client.post(remove, json={"customer_id": "hotel", "confirmed": True}, headers=wrong).status_code == 403

    original = make_offer(client)
    product = client.app.state.service.catalog.get("milk-unknown")
    line = {**original["payload"]["lines"][0], **product, "product_id": "milk-unknown", "pack_size": 1, "packs": 100, "quantity": 100, "unit_price": 1.2, "total": 120, "available": 10000}
    missing_document = fixture_offer(client, lines=[line], total=120, need=need(quantity=100))
    missing = place_order(client, document=missing_document)
    confirmation = {"action": "approve", "confirmations": [{"product_id": "milk-unknown", "property": "lactose-free", "confirmed": True}], "confirmed": True}
    assert client.post(f"/orders/{missing['id']}/signoff", json=confirmation).status_code == 401
    assert client.post(f"/orders/{missing['id']}/signoff", json=confirmation, headers=wrong).status_code == 403

    assert client.post("/demo/reset", json={"customer_id": "restaurant"}).status_code == 401
    assert client.post("/demo/reset", json={"customer_id": "restaurant"}, headers=human).status_code == 403

    approved = client.post(f"/orders/{held['id']}/signoff", json={"action": "approve"}, headers=human)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "committed"
    record = client.get(f"/records/{approved.json()['record_id']}").json()
    assert login["session_id"] in record["approved_by"]
    assert login["token"] not in json.dumps(record)
    assert "/demo/buyer-session" not in inspect.getsource(ReferenceBot), "The reference bot cannot mint a buyer session."


def test_K06(client):
    """Buyer/bot read authority is mandatory and mandate bearer tokens stay hidden."""
    made = place_order(client)
    wrong = human_auth(client, "restaurant")
    targets = (
        ("/usual-order", {"customer_id": "hotel"}),
        ("/limits", {"customer_id": "hotel"}),
        (f"/orders/{made['id']}", None),
    )
    for path, params in targets:
        assert client.get(path, params=params).status_code == 401
        assert client.get(path, params=params, headers=wrong).status_code == 403
        mandated = client.get(path, params=params, headers=read_auth(client))
        assert mandated.status_code == 200, mandated.text
        session = client.get(path, params=params, headers=human_auth(client))
        assert session.status_code == 200, session.text

    authority = auth(client)
    document = client.get(f"/mandates/{buyer(client)['mandate_id']}")
    assert document.status_code == 200
    assert set(document.json()) == {"payload"}
    assert document.json()["payload"] == buyer(client)["mandate_payload"]
    assert authority["mandate"] not in document.text
    assert "signed token" not in document.text.lower()


def test_K07(client, tmp_path):
    """Real allergen IDs and units outrank conflicting claims and confirmations."""
    raw_products = []
    for allergen_id, unit, prop, free_claim in (
        ("ML", "CONTAINS", "lactose-free", "FREE_FROM_LACTOSE"),
        ("AW", "MAY_CONTAIN", "gluten-free", "FREE_FROM_GLUTEN"),
    ):
        raw = {
            "id": f"K07-{allergen_id}", "category": "10400", "salesUnitGtin": "0000000000000",
            "synkkaData": {"names": [{"language": "en", "value": f"Contrary {allergen_id} milk 1 L"}], "brand": "Fixture", "classifications": [
                {"name": "allergen", "values": [{"id": allergen_id, "unit": unit}]},
                {"name": "nutritionalClaim", "values": [{"id": free_claim}, {"id": "FREE_FROM_LACTOSE"}]},
            ]},
        }
        raw_products.append(raw)
        product = normalize_product(raw)
        assert product["contradicted_properties"][prop] == "manufacturer"
        assert prop not in product["properties"], "A conflicting free-from claim cannot cancel contrary allergen data."
        client.app.state.service.catalog.upsert(product)

        original = make_offer(client)
        line = {**original["payload"]["lines"][0], **product, "product_id": product["id"], "pack_size": 1, "packs": 100, "quantity": 100, "unit_price": 1.2, "total": 120, "available": 10000}
        document = fixture_offer(client, lines=[line], total=120, need=need(quantity=100, properties=[prop]))
        held = place_order(client, document=document)
        assert held["status"] == "requires_escalation"
        assert not held["checks"][3]["passed"] and "contrary" in held["checks"][3]["reason"].lower()
        response = client.post(
            f"/orders/{held['id']}/signoff",
            json={"action": "approve", "confirmations": [{"product_id": product["id"], "property": prop, "confirmed": True}], "confirmed": True},
            headers=human_auth(client),
        )
        assert response.status_code == 409
        with client.app.state.service.store.connect() as db:
            assert not [entry for entry in client.app.state.service.store.list(db, "confirmation") if entry["product_id"] == product["id"]]

    source = tmp_path / "licensed-shaped-fixture.json"
    source.write_text(json.dumps(raw_products[:1]))
    catalog = Catalog(tmp_path / "versioned.sqlite3")
    assert catalog.needs_rebuild()
    assert catalog.build(source) == 1
    assert catalog.index_version() == CATALOG_INDEX_VERSION and not catalog.needs_rebuild()
    with catalog.connect() as db:
        db.execute("UPDATE catalog_metadata SET value='legacy' WHERE key='index_version'")
    assert catalog.needs_rebuild()
    catalog.build(source)
    assert catalog.index_version() == CATALOG_INDEX_VERSION and not catalog.needs_rebuild()


def test_K08(client):
    """A spent cutoff explains itself and leads to a confirmable later offer."""
    service = client.app.state.service
    requested = need()["deadline"]
    later = (datetime.fromisoformat(requested) + timedelta(days=2)).date().isoformat()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    open_cutoff = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
    for offset in range(8):
        day = (datetime.fromisoformat(requested) + timedelta(days=offset)).date().isoformat()
        service.valio.fixture_availability[("milk-10", day)] = {
            "available": 10000, "delivery_date": day,
            "cutoff": past if day < later else open_cutoff,
            "sales_block": False, "logistic_model": "Stock", "deliverable": True,
            "source": "synthetic cutoff fixture",
        }
    request_need = need(quantity=100, product_ids=["milk-10"])
    authority = auth(client)
    result = matching(client, authority, request_need)
    assert result["earliest_orderable_date"] == later
    assert result["options"] and result["options"][0]["kind"] == "all_later"
    option = result["options"][0]
    assert option["delivery_date"] == later and option["earliest_orderable_date"] == later

    response = client.post("/offers", json={**authority, "option": option})
    assert response.status_code == 200, response.text
    document = response.json()
    held = place_order(client, authority, document)
    assert held["status"] == "requires_escalation"
    assert held["earliest_orderable_date"] == later
    reason = held["checks"][4]["reason"]
    assert "cutoff" in reason.lower() and requested in reason and later in reason
    assert "stock is short" not in reason.lower()

    # Retain a stale signed offer to exercise the original review failure: its
    # requested-date cutoff is spent even though a later open-cutoff quote exists.
    stale_line = copy.deepcopy(document["payload"]["lines"][0])
    stale_line.update(delivery_date=requested, inventory_date=requested, cutoff=past, on_time=False, stock_shortfall=0)
    stale_document = fixture_offer(
        client, lines=[stale_line], total=stale_line["total"],
        delivery_date=requested, on_time=False, option_kind="single_product",
        need=document["payload"]["need"], requested_cutoff_passed=True,
        earliest_orderable_date=later,
    )
    stale_held = place_order(client, authority, stale_document)
    assert stale_held["status"] == "requires_escalation"
    human = human_auth(client)
    first = client.post(f"/orders/{stale_held['id']}/signoff", json={"action": "approve"}, headers=human)
    assert first.status_code == 200, first.text
    fresh = first.json()
    assert fresh["status"] == "requires_escalation" and fresh["requires_price_confirmation"]
    assert fresh["offer"] != stale_held["offer"]
    assert fresh["offer_payload"]["delivery_date"] == later
    assert fresh["earliest_orderable_date"] == later
    assert all(line["cutoff"] == open_cutoff for line in fresh["offer_payload"]["lines"])

    approved = client.post(
        f"/orders/{stale_held['id']}/signoff",
        json={"action": "approve", "offer": fresh["offer"]}, headers=human,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "committed" and approved.json()["receipt"]
    assert approved.json()["offer_payload"]["delivery_date"] == later


def test_K09(client):
    """Every human limit decision is a verifiable buyer-owned linked record."""
    restaurant_login = client.post("/demo/buyer-session", json={"customer_id": "restaurant"}).json()
    restaurant_human = {"X-Buyer-Session": restaurant_login["token"]}
    hotel_login = client.post("/demo/buyer-session", json={"customer_id": "hotel"}).json()
    hotel_human = {"X-Buyer-Session": hotel_login["token"]}

    proposal = client.get("/limits", params={"customer_id": "restaurant"}, headers=read_auth(client, "restaurant")).json()["proposals"][0]
    accepted = client.post(
        f"/limits/proposals/{proposal['id']}/accept",
        json={"customer_id": "restaurant", "confirmed": True}, headers=restaurant_human,
    )
    assert accepted.status_code == 200, accepted.text
    reset = client.post(
        "/limits/departure/reset", json={"customer_id": "restaurant"}, headers=restaurant_human,
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["record_id"] != accepted.json()["record_id"]

    payload = copy.deepcopy(buyer(client)["mandate_payload"])
    payload.update(jti=str(uuid.uuid4()), version=payload["version"] + 1)
    payload["learned_preferences"] = [{
        "id": "K09-learned", "product_type": "drinking milk", "preference": "brand",
        "accepted_value": "Fixture", "scope": "always", "question": "May this brand be used?",
        "answer": "approve", "confirmation_summary": "Approve Fixture for drinking milk.",
    }]
    token = client.app.state.service.keys.sign_buyer("hotel", payload)
    registered = client.post("/mandates", json={"mandate": token}, headers=hotel_human)
    assert registered.status_code == 200, registered.text
    removed = client.post(
        "/limits/K09-learned/reset", json={"customer_id": "hotel", "confirmed": True}, headers=hotel_human,
    )
    assert removed.status_code == 200, removed.text

    responses = (accepted, reset, registered, removed)
    expected_actions = ("proposal_accepted", "freedom_reset", "mandate_version_registered", "learned_preference_removed")
    records = []
    for response, action in zip(responses, expected_actions):
        body = response.json()
        assert body["record_id"] and body["record_hash"]
        fetched = client.get(f"/records/{body['record_id']}")
        assert fetched.status_code == 200, fetched.text
        record = fetched.json()
        assert record["decision_type"] == record["outcome"] == action
        assert record["buyer_copy"] is True and record["request"] and record["sentences"]
        assert body["record_hash"] == record["hash"]
        records.append(record)

    assert records[1]["previous_hash"] == records[0]["hash"]
    assert records[3]["previous_hash"] == records[2]["hash"]
    assert restaurant_login["session_id"] in records[0]["approved_by"]
    assert restaurant_login["session_id"] in records[1]["approved_by"]
    assert hotel_login["session_id"] in records[2]["approved_by"]
    assert hotel_login["session_id"] in records[3]["approved_by"]
    for record in records:
        stored_hash = record.pop("hash")
        assert digest(record) == stored_hash


def test_K12(tmp_path, monkeypatch):
    """The first door request after idle gets exactly the manual-reset state."""
    monkeypatch.setenv("DEMO_ACCESS_CODE", "")
    monkeypatch.setenv("DEMO_IDLE_RESET_MINUTES", "20")
    now = [0.0]
    application = create_app(tmp_path, fixture=True, idle_clock=lambda: now[0])
    with TestClient(application) as client:
        service = client.app.state.service
        initial = client.get("/demo/customers")
        assert initial.status_code == 200 and initial.json()["idle_reset_at"] is None
        hotel_session = human_auth(client, "hotel")
        restaurant_session = human_auth(client, "restaurant")

        made = place_order(client, request_need=need(quantity=10))
        assert made["status"] == "committed" and service.reservations()
        initial_restaurant = client.get("/limits", params={"customer_id": "restaurant"}, headers=restaurant_session).json()
        proposal = initial_restaurant["proposals"][0]
        accepted = client.post(
            f"/limits/proposals/{proposal['id']}/accept",
            json={"customer_id": "restaurant", "confirmed": True}, headers=restaurant_session,
        )
        assert accepted.status_code == 200 and not accepted.json()["proposals"]

        payload = copy.deepcopy(buyer(client)["mandate_payload"])
        payload.update(jti=str(uuid.uuid4()), version=payload["version"] + 1)
        payload["learned_preferences"] = [{
            "id": "K12-learned", "product_type": "drinking milk", "preference": "brand",
            "accepted_value": "Fixture", "scope": "always", "question": "May this brand be used?",
            "answer": "approve", "confirmation_summary": "Approve Fixture for drinking milk.",
        }]
        token = service.keys.sign_buyer("hotel", payload)
        assert client.post("/mandates", json={"mandate": token}, headers=hotel_session).status_code == 200
        used_before = next(row for row in client.get("/limits", headers=hotel_session).json()["rows"] if row["id"] == "monthly_budget")
        assert used_before["used"] > 0 and used_before["remaining"] < used_before["signed_ceiling"]

        now[0] = 20 * 60 + 1
        for path in ("/healthz", "/assets/K12-check.js", "/"):
            response = client.get(path)
            assert "X-Demo-Idle-Reset" not in response.headers
            with service.store.connect() as db:
                assert service.store.order(db, made["id"]) is not None, f"{path} must not trigger an idle reset."

        first = client.get("/demo/customers")
        assert first.status_code == 200, first.text
        assert first.headers["X-Demo-Idle-Reset"] == "1"
        reset_body = first.json()
        assert reset_body["idle_reset_at"] is not None
        assert datetime.fromisoformat(reset_body["idle_reset_at"]).tzinfo is not None
        second = client.get("/demo/customers")
        assert "X-Demo-Idle-Reset" not in second.headers
        assert second.json()["idle_reset_at"] == reset_body["idle_reset_at"]

        with service.store.connect() as db:
            assert service.store.order(db, made["id"]) is None
            assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert service.reservations() == {}
        assert client.get(f"/orders/{made['id']}", headers=read_auth(client)).status_code == 404

        # Manual reset preserves simulated buyer logins; idle reset must too.
        hotel_limits = client.get("/limits", headers=hotel_session)
        assert hotel_limits.status_code == 200, hotel_limits.text
        hotel_body = hotel_limits.json()
        budget = next(row for row in hotel_body["rows"] if row["id"] == "monthly_budget")
        assert budget["used"] == 0 and budget["remaining"] == budget["signed_ceiling"]
        assert hotel_body["learned_preferences"] == []

        restaurant_limits = client.get("/limits", params={"customer_id": "restaurant"}, headers=restaurant_session)
        assert restaurant_limits.status_code == 200, restaurant_limits.text
        restaurant_body = restaurant_limits.json()
        departure = next(row for row in restaurant_body["rows"] if row["id"] == "departure")
        initial_departure = next(row for row in initial_restaurant["rows"] if row["id"] == "departure")
        assert departure["current_freedom"] == initial_departure["current_freedom"] == 0.1
        assert restaurant_body["proposals"] == initial_restaurant["proposals"]


def test_K13(tmp_path, monkeypatch):
    from door.main import create_app
    code = "private-demo-code"
    monkeypatch.setenv("DEMO_ACCESS_CODE", code)
    monkeypatch.setenv("DEMO_ACCESS_MAX_FAILURES", "3")
    monkeypatch.setenv("DEMO_ACCESS_LOCKOUT_SECONDS", "600")
    monkeypatch.setenv("CLIENT_IP_HEADER", "Cf-Connecting-Ip")
    attacker, visitor, early = {"Cf-Connecting-Ip": "192.0.2.10"}, {"Cf-Connecting-Ip": "192.0.2.20"}, {"Cf-Connecting-Ip": "192.0.2.30"}
    with TestClient(create_app(tmp_path, fixture=True), client=("127.0.0.1", 50000)) as proxy:
        # A visitor who unlocked earlier holds a session cookie.
        unlocked = proxy.post("/demo/unlock", data={"access_code": code}, headers=early, follow_redirects=False)
        assert unlocked.status_code == 303
        cookie = {"demo_access": unlocked.cookies["demo_access"]}
        proxy.cookies.clear()
        # A correct code clears earlier failures from the same address.
        for _ in range(2):
            assert proxy.post("/demo/unlock", data={"access_code": "wrong"}, headers=visitor).status_code == 403
        assert proxy.post("/demo/unlock", data={"access_code": code}, headers=visitor, follow_redirects=False).status_code == 303
        proxy.cookies.clear()
        for _ in range(2):
            assert proxy.post("/demo/unlock", data={"access_code": "wrong"}, headers=visitor).status_code == 403
        # Wrong codes count on both paths: two by form, one by header.
        for _ in range(2):
            assert proxy.post("/demo/unlock", data={"access_code": "wrong"}, headers=attacker).status_code == 403
        assert proxy.get("/demo/customers", headers={**attacker, "X-Demo-Access-Code": "wrong"}).status_code == 401
        locked = proxy.post("/demo/unlock", data={"access_code": code}, headers=attacker, follow_redirects=False)
        assert locked.status_code == 429 and int(locked.headers["retry-after"]) > 0 and code not in locked.text
        by_header = proxy.get("/demo/customers", headers={**attacker, "X-Demo-Access-Code": code})
        assert by_header.status_code == 429 and by_header.headers["retry-after"]
        # Other addresses and earlier sessions are unaffected.
        assert proxy.get("/demo/customers", headers={**visitor, "X-Demo-Access-Code": code}).status_code == 200
        assert proxy.get("/demo/customers", headers=attacker, cookies=cookie).status_code == 200
        assert proxy.get("/healthz", headers=attacker).status_code == 200
    # An untrusted peer cannot rotate the edge header to escape the lockout.
    with TestClient(create_app(tmp_path / "direct", fixture=True), client=("198.51.100.10", 50000)) as direct:
        for index in range(3):
            assert direct.post("/demo/unlock", data={"access_code": "wrong"}, headers={"Cf-Connecting-Ip": f"192.0.2.{50 + index}"}).status_code == 403
        assert direct.post("/demo/unlock", data={"access_code": code}, headers={"Cf-Connecting-Ip": "192.0.2.99"}, follow_redirects=False).status_code == 429
