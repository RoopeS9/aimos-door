from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

import httpx
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from conftest import ROOT, auth, buyer, new_mandate, offer, order, verify


def test_H01(client):
    previous_hash = None
    for _ in range(2):
        made = order(client)
        response = client.get(f"/records/{made['record_id']}")
        assert response.status_code == 200
        record = response.json()
        assert record["buyer_copy"] is True
        assert record["request"] and record["filters_applied"] and record["options_considered"]
        assert len(record["checks"]) == 10 and record["outcome"] == "committed" and record["approved_by"]
        assert len(record["sentences"]) >= 14
        assert all(isinstance(sentence, str) and len(sentence.split()) >= 3 for sentence in record["sentences"])
        assert all(any(check["reason"] in sentence for sentence in record["sentences"]) for check in record["checks"])
        assert record["previous_hash"] == previous_hash
        actual_hash = record.pop("hash")
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        assert hashlib.sha256(canonical).hexdigest() == actual_hash
        receipt = verify(made["receipt"], client.get("/.well-known/jwks.json").json())
        assert receipt["record_hash"] == actual_hash
        previous_hash = actual_hash


def test_H02(client, tmp_path):
    receipt = order(client)["receipt"]
    receipt_file, jwks_file = tmp_path / "receipt.jws", tmp_path / "jwks.json"
    receipt_file.write_text(receipt)
    jwks_file.write_text(json.dumps(client.get("/.well-known/jwks.json").json()))
    command = [sys.executable, str(ROOT / "scripts/verify_receipt.py"), str(receipt_file), "--jwks", str(jwks_file)]
    verified = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert verified.returncode == 0 and json.loads(verified.stdout)["verified"] is True
    head, encoded, signature = receipt.split(".")
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    payload["record_hash"] = "0" * 64
    changed = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    receipt_file.write_text(".".join((head, changed, signature)))
    tampered = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert tampered.returncode != 0 and "FAILED" in tampered.stderr


def test_H03(client):
    source = (ROOT / "docs/standards.md").read_text().lower()
    for text in ("implemented", "mapped", "openapi", "jws", "jwk", "gtin", "ucp", "ap2", "peppol", "cancellation"):
        assert text in source
    held = order(client, new_mandate(client, per_order_cap=1))
    assert held["status"] == "requires_escalation" and held["continue_url"]


def test_H04(client):
    for line in offer(client)["payload"]["lines"]:
        product = client.app.state.service.catalog.get(line["id"])
        if product["gtin"]:
            assert line["gtin"] == product["gtin"]
        assert line["origin"] == product["origin"] and line["origin"]["source"] == "manufacturer"
        assert line["labels"] == product["labels"]
        assert all(label["source"] == "manufacturer" for label in line["labels"])


def test_I01():
    assert (ROOT / "Dockerfile").is_file(), "Supply the one-container build before running container acceptance."
    env = dict(os.environ)
    # Load privately and pass by inherited environment names, never shell expansion or printed argv.
    for name, value in dotenv_values(ROOT / ".env").items():
        if value is not None:
            env.setdefault(name, value)
    assert env.get("VALIO_API_KEY"), "The container's real first-start download requires VALIO_API_KEY."
    tag = "aimos-door-acceptance:local"
    build = subprocess.run(["docker", "build", "-t", tag, "."], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert build.returncode == 0, "docker build failed; inspect the build output privately."
    name = "aimos-door-acceptance-" + uuid.uuid4().hex[:10]
    container = None
    try:
        run = subprocess.run(["docker", "run", "--detach", "--name", name, "--publish", "127.0.0.1::8000", "--env", "VALIO_API_KEY", "--env", "OPENROUTER_API_KEY", "--env", "AGENT_PROVIDER=openrouter", "--env", "AGENT_WARMUP_ENABLED=0", "--env", "DATA_DIR=/app/data", tag], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
        assert run.returncode == 0, "The container did not start with environment-only configuration."
        container = run.stdout.strip()
        port = subprocess.run(["docker", "port", container, "8000/tcp"], capture_output=True, text=True, check=True, timeout=15).stdout.strip().rsplit(":", 1)[-1]
        base = f"http://127.0.0.1:{port}"
        until, health = time.monotonic() + 240, None
        while time.monotonic() < until:
            try:
                response = httpx.get(base + "/healthz", timeout=2)
                if response.status_code == 200:
                    health = response.json()
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.25)
        assert health is not None and health["status"] == "ok" and health["products"] == 17546 and health["fixture"] is False
        assert httpx.get(base + "/", timeout=10).status_code == 200
        check = subprocess.run(["docker", "exec", container, "python", "-c", "from pathlib import Path; import sqlite3; p=Path('/app/data'); assert (p/'valio_aimo_junction26_product_data.json').stat().st_size>80000000; assert (p/'valio_aimo_junction26_delivery_reliability.csv').exists(); assert (p/'valio_aimo_junction26_carbon_footprint.csv').exists(); assert sqlite3.connect(p/'catalog.sqlite3').execute('select count(*) from catalog').fetchone()[0]==17546"], capture_output=True, text=True, timeout=15)
        assert check.returncode == 0, "First start must fetch the licensed files and build the index inside the container."
    finally:
        if container:
            subprocess.run(["docker", "rm", "--force", container], capture_output=True, timeout=30)


def test_I02():
    readme = (ROOT / "README.md").read_text() + "\n\n" + (ROOT / "docs/SETUP.md").read_text()
    lowered = readme.lower()
    for text in ("aimo", "buyer", "signed", "make run", "docker", "real", "simulated", "licence", "architecture"):
        assert text in lowered
    for name in ("VALIO_API_KEY", "OPENROUTER_API_KEY", "AGENT_PROVIDER", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "AGENT_MODEL", "DECISION_MODEL", "DATA_DIR", "PUBLIC_BASE_URL", "DEMO_ACCESS_CODE", "DOOR_SIGNING_KEY"):
        assert name in readme
    paragraphs = [paragraph.strip() for paragraph in readme.split("\n\n") if paragraph.strip() and not paragraph.lstrip().startswith(("#", "|", "```", "-"))]
    assert any(len(paragraph.split()) >= 35 for paragraph in paragraphs), "Include the one-paragraph product description."
    assert any(8 <= len(paragraph.split()) <= 45 and "door" in paragraph.lower() for paragraph in paragraphs), "Include the one-sentence product description."
    assert "MIT License" in (ROOT / "LICENSE").read_text()
    assert (ROOT / "render.yaml").is_file() and (ROOT / "docs/assumptions.md").is_file()


def test_I03(tmp_path, monkeypatch):
    from door.main import create_app
    from reference_bot.providers import FixtureProvider
    monkeypatch.setenv("DEMO_ACCESS_CODE", "offline-gate-code")
    monkeypatch.setenv("DEMO_RATE_LIMIT", "5")
    with TestClient(create_app(tmp_path / "gate", fixture=True)) as guarded:
        assert guarded.get("/").status_code in (401, 403)
        assert guarded.post("/bot/chat", json={"stage": "preview", "message": "10 litres milk"}).status_code in (401, 403)
        headers = {"X-Demo-Access-Code": "offline-gate-code"}
        assert guarded.get("/", headers=headers).status_code == 200
        statuses = [guarded.post("/bot/chat", headers=headers, json={"stage": "preview", "message": "10 litres milk"}).status_code for _ in range(8)]
        assert 200 in statuses and 429 in statuses, "The public bot must enforce a per-IP request cap."
    monkeypatch.delenv("DEMO_ACCESS_CODE")
    monkeypatch.setenv("DEMO_RATE_LIMIT", "100")
    monkeypatch.setenv("MODEL_DAILY_CAP", "1")
    with TestClient(create_app(tmp_path / "budget", fixture=True)) as limited:
        limited.app.state.service.fixture = False
        limited.app.state.bot_provider = FixtureProvider()
        first = limited.post("/bot/chat", json={"stage": "preview", "message": "10 litres milk"})
        second = limited.post("/bot/chat", json={"stage": "preview", "message": "10 litres milk"})
        assert first.status_code == 200 and second.status_code == 429
        assert "daily" in json.dumps(second.json()).lower()


def test_I04(tmp_path, monkeypatch, capsys):
    unit = subprocess.run(["make", "test"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert unit.returncode == 0, unit.stdout + unit.stderr
    import scripts.run_acceptance as runner
    cases = (ROOT / "ACCEPTANCE.md").read_text()
    ids = re.findall(r"^\| ([A-K]\d{2}) \| (\d) \|", cases, re.M)
    (tmp_path / "ACCEPTANCE.md").write_text(cases)
    (tmp_path / "STATUS.md").write_text("# Acceptance runner fixture\n")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    def execute(command, **kwargs):
        report_arg = next(part for part in command if part.startswith("--junitxml="))
        report = Path(report_arg.split("=", 1)[1])
        report.write_text('<testsuites><testsuite>' + ''.join(f'<testcase name="test_{case_id}" />' for case_id, _ in ids) + '</testsuite></testsuites>')
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(runner.subprocess, "run", execute)
    assert runner.main() == 0
    output = capsys.readouterr().out
    assert "| ID |" in output and "PASS" in output
    assert all(f"| {case_id} | M{milestone} | PASS |" in output for case_id, milestone in ids)


def test_I05(client, monkeypatch):
    from reference_bot.providers import ToolProvider
    from test_bot_console import PROMPT, inject_http
    monkeypatch.setenv("AGENT_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-openrouter-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    calls = []
    def response(request):
        calls.append(request)
        assert request.url.host == "openrouter.ai"
        assert "x-api-key" not in request.headers
        if request.method == "GET":
            assert request.url.path == "/api/v1/models"
            return httpx.Response(200, json={"data": [{"id": "anthropic/claude-opus-5.5"}]})
        body = json.loads(request.content)
        assert body["model"] == "anthropic/claude-opus-5.5" and body["tools"][0]["function"]["name"] == "match_need"
        from conftest import need
        return httpx.Response(200, json={"model": body["model"], "choices": [{"message": {"tool_calls": [{"type": "function", "function": {"name": "match_need", "arguments": json.dumps({"need": need(), "restatement": "I will find the requested milk."})}}]}}]})
    transport = httpx.MockTransport(response)
    upstream = httpx.AsyncClient(transport=transport)
    # The explicit transport is local; bypass the suite's external-network guard only for this stub.
    original_send = httpx.AsyncClient.send
    async def stub_send(self, request, *args, **kwargs):
        if self is upstream:
            return await self._send_single_request(request)
        return await original_send(self, request, *args, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, "send", stub_send)
    client.app.state.bot_provider = ToolProvider(client=upstream)
    connection = inject_http(client)
    try:
        result = client.post("/bot/chat", json={"customer_id": "hotel", "message": PROMPT})
        assert result.status_code == 200, result.text
        payload = result.json()
        assert payload["model"]["provider"] == "openrouter"
        assert payload["order"]["status"] == "committed" and payload["order"]["receipt"]
        assert [request.method for request in calls] == ["GET", "POST"]
    finally:
        asyncio.run(connection.aclose())
        asyncio.run(upstream.aclose())
