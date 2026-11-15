from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from conftest import auth, buyer, fixture_offer, human_auth, need, offer, order, verify
from door.main import create_app
from door.security import digest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_ACCESS_CODE", raising=False)
    with TestClient(create_app(tmp_path, fixture=True)) as connection:
        yield connection


def identify(client, headers, customer_id="hotel"):
    response = client.post(
        "/demo/identify", json={"customer_id": customer_id, "method": "bank"}, headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def change(client, headers, proof, value, limit_id="per_order_cap", customer_id="hotel"):
    return client.post(
        f"/limits/{limit_id}/change",
        json={"customer_id": customer_id, "value": value},
        headers={**headers, **({"X-Identification-Proof": proof} if proof is not None else {})},
    )


def stored_proof(client, token):
    with client.app.state.service.store.connect() as db:
        return client.app.state.service.store.get(db, "identification_proof", digest(token))


def database_state(client):
    service = client.app.state.service
    with service.store.connect() as db:
        return {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
            for table in ("documents", "orders", "reservations", "records")
        }


def assert_refusal(client, response, status=403):
    assert response.status_code == status, response.text
    assert isinstance(response.json().get("detail"), str) and response.json()["detail"]


@contextmanager
def unchanged(client):
    before = database_state(client)
    yield
    assert database_state(client) == before


def priced_offer(client, total):
    """An otherwise valid door-signed fixture at one exact EUR amount."""
    authority = auth(client)
    original = offer(client, authority)
    line = copy.deepcopy(original["payload"]["lines"][0])
    line.update(
        pack_size=1, packs=2000, quantity=2000, unit="L",
        unit_price=total / 2000, total=total,
        raw_available=10000, reserved_at_match=0, available=10000,
        stock_shortfall=0, on_time=True,
    )
    requested = copy.deepcopy(original["payload"]["need"])
    requested.update(quantity=2000, unit="L")
    return fixture_offer(
        client, authority, lines=[line], total=total, need=requested,
        option_kind="single_product", overage=0,
        delivery_date=line["delivery_date"], on_time=True,
    )


def test_M01(client):
    """M01: identification is simulated, short-lived, opaque and session-bound."""
    hotel = human_auth(client)
    restaurant = human_auth(client, "restaurant")
    body = {"customer_id": "hotel", "method": "bank"}
    assert client.post("/demo/identify", json=body).status_code == 401
    row = buyer(client)
    bot = {"Authorization": f"Bearer {row['mandate']}", "X-Bot-Id": row["bot_id"]}
    assert client.post("/demo/identify", json=body, headers=bot).status_code == 401
    assert client.post("/demo/identify", json=body, headers=restaurant).status_code == 403
    assert client.post(
        "/demo/identify", json={"customer_id": "hotel", "method": "other"}, headers=hotel,
    ).status_code == 422

    before = datetime.now(timezone.utc)
    identified = identify(client, hotel)
    after = datetime.now(timezone.utc)
    assert identified["simulated"] is True
    assert identified["method"] == "Simulated bank identification"
    assert identified["authority"] == "May sign for Hotel chain (simulated company authorisation)"
    assert "fictional" in identified["person"]["name"].lower()
    assert isinstance(identified["proof"], str) and len(identified["proof"]) >= 32
    assert identified["proof"].count(".") != 2, "The proof is opaque, not a signed credential."
    expires = datetime.fromisoformat(identified["expires_at"])
    assert before + timedelta(minutes=5) <= expires <= after + timedelta(minutes=5, seconds=1)

    document = stored_proof(client, identified["proof"])
    assert document["customer_id"] == "hotel" and document["used_at"] is None
    assert document["simulated"] is True
    with client.app.state.service.store.connect() as db:
        session = client.app.state.service.store.get(db, "buyer_session", digest(hotel["X-Buyer-Session"]))
    assert document["buyer_session_id"] == session["id"]
    assert document["buyer_session_key"] == digest(hotel["X-Buyer-Session"])
    assert identified["proof"] not in json.dumps(document)
    assert identify(client, hotel)["proof"] != identified["proof"]


def test_M02(client):
    """M02: each change supersedes the mandate and appends a verifiable decision."""
    before = buyer(client)
    session = human_auth(client)
    proof = identify(client, session)
    response = change(client, session, proof["proof"], 3000)
    assert response.status_code == 200, response.text
    result = response.json()
    assert set(result) == {"limits", "change", "mandate_version", "record_id"}
    assert result["mandate_version"] == before["mandate_payload"]["version"] + 1
    assert result["change"]["limit_id"] == "per_order_cap"
    assert result["change"]["name"] == "Spend per order"
    assert result["change"]["old_value"] == 2000 and result["change"]["new_value"] == 3000
    assert result["change"]["identified_by"] == "Simulated bank identification"
    assert result["change"]["signed_by"] == proof["person"]["name"]
    current_limits = client.get("/limits", params={"customer_id": "hotel"}, headers=session)
    assert current_limits.status_code == 200 and result["limits"] == current_limits.json()
    assert next(row for row in result["limits"]["rows"] if row["id"] == "per_order_cap")["signed_ceiling"] == 3000

    after = buyer(client)
    assert after["mandate"] != before["mandate"]
    assert after["mandate_payload"]["version"] == result["mandate_version"]
    assert after["mandate_payload"]["limits"]["per_order_cap"] == 3000
    assert after["mandate_payload"]["limits"]["must_hold_categories"] == before["mandate_payload"]["limits"]["must_hold_categories"]
    assert after["mandate_payload"]["limits"]["allowed_categories"] == before["mandate_payload"]["limits"]["allowed_categories"]
    assert verify(after["mandate"], client.get("/demo/buyer-jwks").json()) == after["mandate_payload"]
    stale = client.get(
        "/limits", params={"customer_id": "hotel"},
        headers={"Authorization": f"Bearer {before['mandate']}", "X-Bot-Id": before["bot_id"]},
    )
    assert stale.status_code == 403, "The superseded mandate must no longer authorize calls."

    first_record = client.get(f"/records/{result['record_id']}", headers=session)
    assert first_record.status_code == 200, first_record.text
    record = first_record.json()
    assert {"outcome", "approved_by", "sentences", "hash", "previous_hash"} <= record.keys()
    assert record["outcome"] == "limit_changed" and record["buyer_copy"] is True
    assert record["created_at"] == result["change"]["at"]
    text = json.dumps(record["sentences"])
    for expected in (
        "Spend per order", "2000", "3000", result["change"]["signed_by"],
        "Simulated bank identification", result["change"]["at"],
    ):
        assert expected in text
    assert result["change"]["signed_by"] in record["approved_by"]
    saved_hash = record.pop("hash")
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    assert hashlib.sha256(canonical).hexdigest() == saved_hash

    second_proof = identify(client, session)
    monthly = change(client, session, second_proof["proof"], 18000, "monthly_budget")
    assert monthly.status_code == 200, monthly.text
    second = monthly.json()
    assert second["mandate_version"] == result["mandate_version"] + 1
    assert second["change"]["old_value"] == 20000 and second["change"]["new_value"] == 18000
    second_record = client.get(f"/records/{second['record_id']}", headers=session).json()
    assert second_record["previous_hash"] == saved_hash
    assert buyer(client)["mandate_payload"]["limits"] == {
        **before["mandate_payload"]["limits"], "per_order_cap": 3000, "monthly_budget": 18000,
    }


def test_M03(client):
    """M03: identification failures and invalid values leave the database untouched."""
    session = human_auth(client)
    other_session = human_auth(client)

    with unchanged(client):
        response = change(client, session, None, 3000)
        assert_refusal(client, response)

    proof = identify(client, session)["proof"]
    with unchanged(client):
        no_buyer = change(client, {}, proof, 3000)
        assert_refusal(client, no_buyer, 401)

    row = buyer(client)
    bot_headers = {"Authorization": f"Bearer {row['mandate']}", "X-Bot-Id": row["bot_id"]}
    with unchanged(client):
        bot_attempt = client.post(
            "/limits/per_order_cap/change", json={"customer_id": "hotel", "value": 3000},
            headers={**bot_headers, "X-Identification-Proof": proof},
        )
        assert_refusal(client, bot_attempt, 401)

    with unchanged(client):
        unknown = change(client, session, "not-a-real-proof", 3000)
        assert_refusal(client, unknown)

    expired_proof = identify(client, session)["proof"]
    document = stored_proof(client, expired_proof)
    document["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with client.app.state.service.store.transaction() as db:
        client.app.state.service.store.put(db, "identification_proof", digest(expired_proof), document)
    with unchanged(client):
        expired = change(client, session, expired_proof, 3000)
        assert_refusal(client, expired)

    wrong_customer_proof = identify(client, human_auth(client, "restaurant"), "restaurant")["proof"]
    with unchanged(client):
        wrong_customer = change(client, session, wrong_customer_proof, 3000)
        assert_refusal(client, wrong_customer)

    wrong_session_proof = identify(client, other_session)["proof"]
    with unchanged(client):
        wrong_session = change(client, session, wrong_session_proof, 3000)
        assert_refusal(client, wrong_session)

    used_proof = identify(client, session)["proof"]
    succeeded = change(client, session, used_proof, 3000)
    assert succeeded.status_code == 200, succeeded.text
    with unchanged(client):
        replay = change(client, session, used_proof, 3000)
        assert_refusal(client, replay)
        assert "used" in replay.json()["detail"].lower()

    for value in (0, -1, None, True, "3000", 10 ** 400, 3000):
        fresh_proof = identify(client, session)["proof"]
        with unchanged(client):
            response = change(client, session, fresh_proof, value)
            assert_refusal(client, response, 422)

    for limit_id, status, phrase in (("departure", 422, "proposal"), ("unknown", 404, "")):
        fresh_proof = identify(client, session)["proof"]
        with unchanged(client):
            response = change(client, session, fresh_proof, 3000, limit_id)
            assert_refusal(client, response, status)
            assert phrase in response.json()["detail"].lower()


def test_M04(client):
    """M04: new ceilings govern the next order without loosening earned freedom."""
    session = human_auth(client)
    original_limits = client.get("/limits", params={"customer_id": "hotel"}, headers=session).json()
    original_departure = next(row for row in original_limits["rows"] if row["id"] == "departure")

    raised = change(client, session, identify(client, session)["proof"], 3000)
    assert raised.status_code == 200, raised.text
    after_raise = client.get("/limits", params={"customer_id": "hotel"}, headers=session).json()
    departure = next(row for row in after_raise["rows"] if row["id"] == "departure")
    assert departure["current_freedom"] == original_departure["current_freedom"]
    assert departure["signed_ceiling"] == original_departure["signed_ceiling"]

    committed = order(client, auth(client), priced_offer(client, 2500))
    assert committed["total"] == 2500
    assert committed["checks"][5]["passed"] and committed["status"] == "committed"

    lowered = change(client, session, identify(client, session)["proof"], 2000)
    assert lowered.status_code == 200, lowered.text
    after_lower = client.get("/limits", params={"customer_id": "hotel"}, headers=session).json()
    spend = next(row for row in after_lower["rows"] if row["id"] == "per_order_cap")
    departure = next(row for row in after_lower["rows"] if row["id"] == "departure")
    assert spend["current_freedom"] <= spend["signed_ceiling"] == 2000
    assert departure["current_freedom"] == original_departure["current_freedom"]

    held = order(client, auth(client), priced_offer(client, 2500))
    assert not held["checks"][5]["passed"] and held["status"] == "requires_escalation"
    assert held["receipt"] is None

    reset = client.post("/demo/reset", json={"customer_id": "hotel"}, headers=session)
    assert reset.status_code == 200, reset.text
    restored = client.get("/limits", params={"customer_id": "hotel"}, headers=session).json()
    assert restored["mandate_version"] == 1
    values = {row["id"]: row["signed_ceiling"] for row in restored["rows"]}
    assert values["per_order_cap"] == 2000 and values["monthly_budget"] == 20000
