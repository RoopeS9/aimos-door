from __future__ import annotations

import json
from pathlib import Path
import re

import psutil

from conftest import LIVE_URL, ROOT


def test_A01(live):
    readme = (ROOT / "README.md").read_text() + "\n\n" + (ROOT / "docs/SETUP.md").read_text()
    assert "make run" in readme, "Document one start command in README.md or docs/SETUP.md."
    makefile = (ROOT / "Makefile").read_text()
    assert re.search(r"^run:.*", makefile, re.M)
    health = live.get("/healthz")
    assert health.status_code == 200 and health.json()["status"] == "ok"
    page = live.get("/")
    assert page.status_code == 200 and '<div id="root"' in page.text
    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', page.text)
    assert assets
    for asset in assets:
        assert live.get(asset).status_code == 200


def test_A02(client):
    schema = client.get("/openapi.json")
    assert schema.status_code == 200 and schema.json()["openapi"].startswith("3.")
    required = {
        "/healthz": "get", "/.well-known/jwks.json": "get", "/catalog/search": "get",
        "/mandates": "post", "/mandates/{mandate_id}": "get", "/needs/match": "post",
        "/offers": "post", "/orders": "post", "/orders/{order_id}": "get",
        "/orders/{order_id}/undo": "post", "/orders/{order_id}/signoff": "post",
        "/records/{record_id}": "get", "/usual-order": "get", "/limits": "get",
        "/limits/proposals/{proposal_id}/accept": "post", "/limits/{limit_id}/reset": "post",
        "/bot/chat": "post", "/demo/customers": "get", "/demo/reset": "post",
    }
    for path, method in required.items():
        assert method in schema.json()["paths"].get(path, {}), f"Missing OpenAPI operation: {method} {path}"
    docs = client.get("/docs")
    assert docs.status_code == 200 and "SwaggerUIBundle" in docs.text and "/openapi.json" in docs.text


def test_A03(client):
    response = client.get("/.well-known/jwks.json")
    assert response.status_code == 200
    keys = response.json()["keys"]
    assert keys and any(key["alg"] == "ES256" and key["kty"] == "EC" and key["crv"] == "P-256" for key in keys)
    assert all("d" not in key and "x" in key and "y" in key for key in keys)


def test_A04():
    from scripts.check_secrets import scan
    assert (ROOT / "frontend/dist/index.html").is_file(), "Build the client before scanning its bundle."
    assert (ROOT / "data/server.log").is_file(), "Retain the exercised server log for secret scanning."
    assert scan() == []


def test_A05(live):
    health = live.get("/healthz").json()
    assert health["products"] == 17546
    # Warm real catalog reads; measuring a temporary fixture process would not prove A05.
    for category in ("10400", "10405", None):
        response = live.get("/catalog/search", params={"limit": 100, **({"category": category} if category else {})})
        assert response.status_code == 200 and response.json()["products"]
    assert health["memory_rss_bytes"] < 400 * 1024 * 1024
    try:
        measured = psutil.Process(health["pid"]).memory_info().rss
    except (psutil.AccessDenied, PermissionError):
        measured = live.get("/healthz").json()["memory_rss_bytes"]
    assert measured < 400 * 1024 * 1024, f"Warmed server RSS is {measured / 1024**2:.1f} MiB."
