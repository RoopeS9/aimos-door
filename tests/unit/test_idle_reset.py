from concurrent.futures import ThreadPoolExecutor
import time

from fastapi.testclient import TestClient
import pytest

from door.bot_runtime import BotRuntime
from door.main import create_app


def put_marker(client, marker="survives"):
    with client.app.state.service.store.transaction() as db:
        client.app.state.service.store.put(db, "option", "idle-test", {"marker": marker})


def marker(client):
    with client.app.state.service.store.connect() as db:
        return client.app.state.service.store.get(db, "option", "idle-test")


def test_idle_reset_zero_disables_and_regular_activity_defers(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_CODE", "")
    monkeypatch.setenv("DEMO_IDLE_RESET_MINUTES", "0")
    now = [0.0]
    with TestClient(create_app(tmp_path / "disabled", fixture=True, idle_clock=lambda: now[0])) as client:
        assert client.get("/demo/customers").json()["idle_reset_at"] is None
        put_marker(client)
        now[0] = 100000
        response = client.get("/demo/customers")
        assert response.status_code == 200
        assert "X-Demo-Idle-Reset" not in response.headers
        assert response.json()["idle_reset_at"] is None
        assert marker(client) == {"marker": "survives"}

    monkeypatch.setenv("DEMO_IDLE_RESET_MINUTES", "0.01")  # 0.6 seconds
    now[0] = 0
    with TestClient(create_app(tmp_path / "active", fixture=True, idle_clock=lambda: now[0])) as client:
        assert client.get("/demo/customers").status_code == 200
        put_marker(client)
        for now[0] in (0.5, 1.0, 1.5):
            response = client.get("/demo/customers")
            assert "X-Demo-Idle-Reset" not in response.headers
            assert marker(client) is not None
        now[0] = 2.11
        response = client.get("/demo/customers")
        assert response.headers["X-Demo-Idle-Reset"] == "1"
        assert marker(client) is None

        # These endpoints neither reset nor refresh the last-activity clock.
        put_marker(client)
        now[0] = 100
        for path in ("/healthz", "/assets/k12-check.js", "/"):
            response = client.get(path)
            assert "X-Demo-Idle-Reset" not in response.headers
            assert marker(client) is not None
        now[0] = 100.59
        response = client.get("/demo/customers")
        assert response.headers["X-Demo-Idle-Reset"] == "1"
        assert marker(client) is None


@pytest.mark.asyncio
async def test_model_warmup_does_not_count_as_door_activity(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_CODE", "")
    monkeypatch.setenv("DEMO_IDLE_RESET_MINUTES", "20")
    now = [0.0]

    class WarmProvider:
        calls = 0

        async def warmup(self):
            self.calls += 1
            return "warm-model"

    provider = WarmProvider()
    with TestClient(create_app(tmp_path / "app", fixture=True, idle_clock=lambda: now[0])) as client:
        assert client.get("/demo/customers").status_code == 200
        put_marker(client)
        runtime = BotRuntime(tmp_path / "runtime", clock=lambda: now[0], provider=provider)
        client.app.state.bot_runtime = runtime
        now[0] = 1199
        await runtime.tick()
        assert provider.calls == 1 and marker(client) is not None
        now[0] = 1200
        response = client.get("/demo/customers")
        assert response.headers["X-Demo-Idle-Reset"] == "1"
        assert marker(client) is None


def test_simultaneous_requests_after_idle_reset_once(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_CODE", "")
    monkeypatch.setenv("DEMO_IDLE_RESET_MINUTES", "20")
    now = [0.0]
    with TestClient(create_app(tmp_path, fixture=True, idle_clock=lambda: now[0])) as client:
        assert client.get("/demo/customers").status_code == 200
        service = client.app.state.service
        original_reset = service.reset
        calls = []

        def counted_reset():
            calls.append(1)
            time.sleep(0.05)  # Let the other request arrive while the claim is held.
            return original_reset()

        service.reset = counted_reset
        now[0] = 1200
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: client.get("/demo/customers"), range(2)))
        assert all(response.status_code == 200 for response in responses)
        assert len(calls) == 1
        assert sum(response.headers.get("X-Demo-Idle-Reset") == "1" for response in responses) == 1
        timestamps = {response.json()["idle_reset_at"] for response in responses}
        assert len(timestamps) == 1 and None not in timestamps
