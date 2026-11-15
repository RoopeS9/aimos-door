from fastapi.testclient import TestClient

from door.main import create_app


def test_gate_cookie_does_not_contain_access_code_and_covers_mutations(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_CODE", "private-demo-code")
    with TestClient(create_app(tmp_path, fixture=True), client=("127.0.0.1", 50000)) as client:
        assert client.get("/").status_code == 401
        assert client.post("/demo/reset").status_code == 401
        assert client.get("/.well-known/jwks.json").status_code == 200
        bad = client.post("/demo/unlock", data={"access_code": "wrong"})
        assert bad.status_code == 403 and "private-demo-code" not in bad.text
        accepted = client.post("/demo/unlock", data={"access_code": "private-demo-code"}, follow_redirects=False)
        assert accepted.status_code == 303
        assert "private-demo-code" not in accepted.headers["set-cookie"]
        assert "HttpOnly" in accepted.headers["set-cookie"]
        assert client.get("/").status_code == 200
        session = client.post("/demo/buyer-session", json={"customer_id": "hotel"})
        assert session.status_code == 200
        assert client.post("/demo/reset", headers={"X-Buyer-Session": session.json()["token"]}).status_code == 200


def test_forwarded_client_addresses_have_separate_limits(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_ACCESS_CODE", raising=False)
    monkeypatch.setenv("DEMO_RATE_LIMIT", "2")
    with TestClient(create_app(tmp_path, fixture=True), client=("127.0.0.1", 50000)) as client:
        first = {"X-Forwarded-For": "192.0.2.1, 127.0.0.1"}
        other = {"X-Forwarded-For": "192.0.2.2, 127.0.0.1"}
        assert client.get("/demo/customers", headers=first).status_code == 200
        assert client.get("/demo/customers", headers=first).status_code == 200
        blocked = client.get("/demo/customers", headers=first)
        assert blocked.status_code == 429 and blocked.headers["retry-after"]
        assert client.get("/demo/customers", headers=other).status_code == 200
        assert client.get("/healthz", headers=first).status_code == 200


def test_untrusted_peer_cannot_rotate_forwarded_address_to_avoid_limit(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_ACCESS_CODE", raising=False)
    monkeypatch.setenv("DEMO_RATE_LIMIT", "1")
    with TestClient(create_app(tmp_path, fixture=True), client=("198.51.100.10", 50000)) as client:
        assert client.get("/demo/customers", headers={"X-Forwarded-For": "192.0.2.1"}).status_code == 200
        assert client.get("/demo/customers", headers={"X-Forwarded-For": "192.0.2.2"}).status_code == 429
