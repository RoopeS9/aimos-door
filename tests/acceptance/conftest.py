"""Acceptance harness: real HTTP for LIVE cases, isolated fixtures otherwise."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import uuid

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from door.main import create_app

ROOT = Path(__file__).resolve().parents[2]
LIVE_URL = os.getenv("ACCEPTANCE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def monday():
    today = datetime.now(timezone.utc).date()
    return (today + timedelta(days=(7 - today.weekday()) % 7 or 7)).isoformat()


def need(**changes):
    return {"product_type": "drinking milk", "properties": ["lactose-free"], "quantity": 1000, "unit": "L", "deadline": monday(), **changes}


def buyer(client, customer_id="hotel"):
    response = client.get("/demo/customers")
    assert response.status_code == 200, response.text
    return next(row for row in response.json()["customers"] if row["id"] == customer_id)


def auth(client, customer_id="hotel"):
    row = buyer(client, customer_id)
    return {"mandate": row["mandate"], "bot_id": row["bot_id"]}


def human_auth(client, customer_id="hotel"):
    response = client.post("/demo/buyer-session", json={"customer_id": customer_id})
    assert response.status_code == 200, response.text
    return {"X-Buyer-Session": response.json()["token"]}


def read_auth(client, customer_id="hotel"):
    row = buyer(client, customer_id)
    return {"Authorization": f"Bearer {row['mandate']}", "X-Bot-Id": row["bot_id"]}


def new_mandate(client, customer_id="hotel", **limits):
    payload = copy.deepcopy(buyer(client, customer_id)["mandate_payload"])
    payload["jti"] = str(uuid.uuid4())
    payload["version"] += 1
    payload["limits"].update(limits)
    token = client.app.state.service.keys.sign_buyer(customer_id, payload)
    response = client.post("/mandates", json={"mandate": token}, headers=human_auth(client, customer_id))
    assert response.status_code == 200, response.text
    return {"mandate": token, "bot_id": payload["bot_id"]}


def matching(client, authority=None, request_need=None):
    authority = authority or auth(client)
    response = client.post("/needs/match", json={**authority, "need": request_need or need()})
    assert response.status_code == 200, response.text
    return response.json()


def offer(client, authority=None, request_need=None, index=0):
    authority = authority or auth(client)
    options = matching(client, authority, request_need)["options"]
    assert options, "The requested fixture need must have at least one matching option."
    response = client.post("/offers", json={**authority, "option": options[index]})
    assert response.status_code == 200, response.text
    return response.json()


def order(client, authority=None, document=None, key=None, request_need=None):
    authority = authority or auth(client)
    document = document or offer(client, authority, request_need)
    response = client.post("/orders", json={**authority, "offer": document["offer"]}, headers={"Idempotency-Key": key or str(uuid.uuid4())})
    assert response.status_code == 200, response.text
    return response.json()


def verify(token, jwks):
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    key = next(key for key in jwks["keys"] if key["kid"] == header["kid"])
    assert key["kty"] == "EC" and key["crv"] == "P-256" and "d" not in key
    return jwt.decode(token, jwt.PyJWK.from_dict(key).key, algorithms=["ES256"])


def assert_live_model(result):
    metadata = result["model"]
    assert metadata["fixture"] is False
    provider = os.getenv("AGENT_PROVIDER", "openai_compatible")
    assert metadata["provider"] == provider, "LIVE acceptance must use the configured real bot provider."
    expected_model = os.getenv("AGENT_MODEL")
    if expected_model:
        assert metadata["id"] == expected_model, "LIVE acceptance must use the configured model."


def fixture_offer(client, authority=None, **payload_changes):
    """Create an upstream fixture at the signed offer boundary, never alter buyer limits."""
    authority = authority or auth(client)
    original = offer(client, authority)
    payload = copy.deepcopy(original["payload"])
    payload.update(payload_changes)
    payload["jti"] = str(uuid.uuid4())
    token = client.app.state.service.keys.sign_door(payload)
    with client.app.state.service.store.transaction() as db:
        client.app.state.service.store.put(db, "offer", payload["jti"], {"offer": token, "payload": payload})
    return {"offer": token, "payload": payload}


def held_for_preference(client):
    authority = new_mandate(client, soft_preferences={"required": {"brand": "A preferred brand"}})
    held = order(client, authority)
    assert held["status"] == "requires_escalation"
    assert not held["checks"][7]["passed"]
    return authority, held


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_ACCESS_CODE", raising=False)
    application = create_app(tmp_path, fixture=True)
    with TestClient(application) as connection:
        yield connection


@pytest.fixture
def live():
    with httpx.Client(base_url=LIVE_URL, timeout=90) as connection:
        response = connection.get("/healthz")
        assert response.status_code == 200, "Start the documented local server before acceptance."
        assert response.json().get("fixture") is False, "LIVE acceptance must never use fixture mode."
        yield connection
