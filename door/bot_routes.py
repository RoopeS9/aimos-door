"""HTTP hosting for the portable reference bot; policy stays in the Door."""
from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Body, Header, HTTPException, Request
import httpx

from door.model_budget import ModelLimitError, admit
from door.protection import DemoProtection
from reference_bot.client import DoorAPIError, ReferenceBot, event
from reference_bot.providers import ClarificationRequired, FixtureProvider, ProviderError, ProviderLimitError, ToolProvider

router = APIRouter(tags=["Reference bot"])


@router.post("/bot/chat")
async def chat(request: Request, body: dict = Body(...), x_buyer_session: str | None = Header(None)):
    stage = body.get("stage", "all")
    if stage not in {"preview", "execute", "all", "converse", "interview", "answer", "confirm"}:
        raise HTTPException(422, "Stage must be preview, execute, all, converse, interview, answer, or confirm.")
    customer_id = body.get("customer_id", "hotel")
    if customer_id not in {"hotel", "restaurant"}:
        raise HTTPException(422, "Choose hotel or restaurant.")
    service = request.app.state.service
    provider = getattr(request.app.state, "bot_provider", None)
    if provider is None:
        provider = FixtureProvider() if service.fixture else ToolProvider()
        request.app.state.bot_provider = provider
    internal_token = getattr(request.app.state, "bot_internal_token", None)
    if internal_token is None:
        internal_token = secrets.token_urlsafe(32)
        request.app.state.bot_internal_token = internal_token
    client = getattr(request.app.state, "bot_http_client", None)
    owns_client = client is None
    if owns_client:
        base = os.getenv("DOOR_API_BASE_URL") or os.getenv("PUBLIC_BASE_URL") or "http://127.0.0.1:8000"
        headers = {
            "X-Forwarded-For": DemoProtection.client_address(request),
            "X-Door-Internal-Bot": internal_token,
        }
        access_code = request.headers.get("X-Demo-Access-Code")
        if access_code:
            headers["X-Demo-Access-Code"] = access_code
        cookies = {"demo_access": request.cookies["demo_access"]} if request.cookies.get("demo_access") else {}
        client = httpx.AsyncClient(base_url=base.rstrip("/"), headers=headers, cookies=cookies, timeout=65, follow_redirects=False)
    before_model = (lambda: admit(service.data_dir, "bot")) if not service.fixture else None
    if isinstance(provider, ToolProvider):
        # Count every real request, including retry/fallback and warm-up, rather
        # than charging once for a plan which may require several attempts.
        if not service.fixture and provider.before_request is None:
            provider.before_request = before_model
        before_model = None
    pending = getattr(request.app.state, "bot_pending_confirmations", None)
    if pending is None:
        pending = {}
        request.app.state.bot_pending_confirmations = pending
    read_headers = {
        row["id"]: {"Authorization": f"Bearer {row['mandate']}", "X-Bot-Id": row["bot_id"]}
        for row in service.customers()["customers"]
    }
    bot = ReferenceBot(client, provider, before_model, pending, read_headers)
    try:
        if stage == "converse":
            history = body.get("history", [])
            if not isinstance(history, list) or len(history) > 12:
                raise HTTPException(422, "History must contain at most the last 12 turns.")
            for turn in history:
                if not isinstance(turn, dict) or turn.get("role") not in {"user", "assistant"} or not isinstance(turn.get("text"), str) or len(turn["text"]) > 8000:
                    raise HTTPException(422, "Each history turn needs a user/assistant role and text of at most 8,000 characters.")
            return await bot.converse(customer_id, body.get("message", ""), history=history, simulate_limited_stock=bool(body.get("simulate_limited_stock", False)), idempotency_key=body.get("idempotency_key"))
        if stage == "interview":
            return await bot.interview(customer_id, body.get("order_id"))
        if stage == "answer":
            return await bot.answer(customer_id, body.get("order_id"), body.get("message", ""), body.get("scope"))
        if stage == "confirm":
            return await bot.confirm(customer_id, body.get("order_id"), body.get("confirmed") is True, body.get("confirmation_summary"), body.get("question_hash"), buyer_session=x_buyer_session)
        if stage in {"preview", "all"}:
            if stage == "preview":
                return await bot.preview(body.get("message", ""), customer_id)
            return await bot.chat(customer_id, body.get("message", ""), simulate_limited_stock=bool(body.get("simulate_limited_stock", False)), idempotency_key=body.get("idempotency_key"))
        need = body.get("need")
        if not isinstance(need, dict):
            raise HTTPException(422, "Execute requires the structured need returned by preview.")
        return await bot.execute(customer_id, need, message=body.get("message", ""), restatement=body.get("restatement"), simulate_limited_stock=bool(body.get("simulate_limited_stock", False)), idempotency_key=body.get("idempotency_key"))
    except ClarificationRequired as error:
        if stage == "converse":
            return {"stage": "conversation", "action": "clarification", "reply": error.question, "clarification": error.question, "reason": error.reason, "order": None, "model": error.model}
        return {"stage": "clarification", "status": "requires_clarification", "question": error.question, "clarification": error.question, "reason": error.reason, "messages": [event("question", error.question)], "need": None, "order": None, "model": error.model}
    except (ProviderLimitError, ModelLimitError) as error:
        raise HTTPException(429, {"message": str(error), "blocked": True, "reason": "usage_or_credit_limit"}) from None
    except (ProviderError, DoorAPIError) as error:
        raise HTTPException(502, str(error)) from None
    finally:
        if owns_client:
            await client.aclose()
