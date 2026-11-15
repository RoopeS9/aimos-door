import json

import httpx
import pytest

from door.decisions import DecisionLabeler
from door.model_budget import ModelLimitError, admit


@pytest.mark.asyncio
async def test_unreachable_model_caches_rule_fallback_without_inventing_claims(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-placeholder")
    calls = []

    async def unavailable(self, url, **kwargs):
        calls.append((url, kwargs["json"]))
        raise httpx.ConnectError("offline fixture")

    monkeypatch.setattr(httpx.AsyncClient, "post", unavailable)
    labeler = DecisionLabeler(tmp_path)
    product = {"id": "fixture", "names": {"en": "Milk"}, "product_type": "drinking milk", "properties": {}}
    first = await labeler.classify(product)
    assert first == await labeler.classify(product)
    assert first["label"] == "drinking milk" and first["source"] == "inferred" and first["method"] == "rules"
    assert product["properties"] == {}
    assert len(calls) == 1
    assert set(calls[0][1]["questions"]) == {"product_type"}


@pytest.mark.asyncio
async def test_model_label_and_probabilities_persist_once(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-placeholder")
    calls = []

    async def response(self, url, **kwargs):
        calls.append(url)
        return httpx.Response(200, request=httpx.Request("POST", url), json={"answers": {"product_type": {"type": "choice", "choice": "sour milk", "probabilities": {"sour milk": .98, "drinking milk": .02}}}})

    monkeypatch.setattr(httpx.AsyncClient, "post", response)
    product = {"id": "fixture", "names": {"en": "Sour milk"}, "product_type": "sour milk"}
    first = await DecisionLabeler(tmp_path).classify(product)
    second = await DecisionLabeler(tmp_path).classify(product)
    assert first == second and len(calls) == 1
    assert first["label"] == "sour milk" and first["method"] == "decision model"
    assert first["probabilities"]["sour milk"] == .98


@pytest.mark.asyncio
async def test_fixture_labels_never_call_model(tmp_path, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("An offline fixture tried to call a model")
    monkeypatch.setattr(httpx.AsyncClient, "post", forbidden)
    assert (await DecisionLabeler(tmp_path, enabled=False).classify({"id": "fixture", "product_type": "cream"}))["label"] == "cream"


def test_daily_model_cap_is_shared_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_DAILY_CAP", "2")
    admit(tmp_path, "bot")
    admit(tmp_path, "decisions")
    with pytest.raises(ModelLimitError):
        admit(tmp_path, "bot")
