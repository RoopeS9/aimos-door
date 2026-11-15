#!/usr/bin/env python3
"""The door as MCP tools: a stdio server any MCP client (Claude, Cursor, ...) can use to buy.

It wraps the door's public HTTP operations only, the same ones examples/minimal_agent.py calls.
The agent can search, match a need or the usual order, get a signed offer, place an order and read the result.
It cannot approve a held order, undo, change a limit or reset: those belong to the buyer, and
no tool for them exists here. Options and signed offers stay in this process; the model passes
short ids such as "opt-1" and "offer-1".

    uv run --group mcp python examples/mcp_server.py --door http://127.0.0.1:8766 --customer hotel

Identity: with --customer the demo mandate is read from /demo/customers. A real buyer hands
its agent the mandate it signed: set AIMO_MANDATE and AIMO_BOT_ID instead.
Other settings: AIMO_DOOR_URL, AIMO_ACCESS_CODE (only if the demo is behind an access code).
"""
from __future__ import annotations

import argparse
import os
import uuid
from typing import Any

import httpx
import jwt
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

INSTRUCTIONS = (
    "Purchasing through Aimo's door inside limits the buyer signed. Flow: search_catalogue, then match_need (or match_usual_order for a reorder), "
    "then request_offer, then place_order. The door decides: an order is committed, committed with a notice, "
    "or held for the buyer. A held order can only be approved by the buyer at its continue_url; "
    "relay that link and the failed checks, and never claim a held order was placed."
)


def build_server(http: httpx.Client, *, customer: str | None = None, mandate: str | None = None, bot_id: str | None = None) -> MCPServer:
    """Build the server over any httpx-compatible client (a FastAPI TestClient works in tests)."""
    if not customer and not (mandate and bot_id):
        raise ValueError("Give --customer for the demo, or set AIMO_MANDATE and AIMO_BOT_ID.")
    server = MCPServer("aimos-door", instructions=INSTRUCTIONS)
    options: dict[str, dict] = {}   # short id -> {"option_id", "need"}
    offers: dict[str, dict] = {}    # short id -> {"offer", "need", "key"}

    def call(method: str, path: str, **kwargs) -> dict:
        try:
            response = http.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise ToolError(f"The door could not be reached: {error}") from error
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise ToolError(f"The door refused ({response.status_code}): {detail}")
        return response.json()

    def identity() -> dict:
        # The demo mandate is re-read each time: a buyer's limit change supersedes the old one.
        if customer:
            row = next((row for row in call("GET", "/demo/customers")["customers"] if row["id"] == customer), None)
            if not row:
                raise ToolError(f"Unknown demo customer: {customer}")
            return {"mandate": row["mandate"], "bot_id": row["bot_id"], "customer_id": row["id"]}
        claims = jwt.decode(mandate, options={"verify_signature": False})
        return {"mandate": mandate, "bot_id": bot_id, "customer_id": claims.get("customer_id")}

    def body() -> dict:
        who = identity()
        return {"mandate": who["mandate"], "bot_id": who["bot_id"]}

    def read_headers() -> tuple[dict, str]:
        who = identity()
        return {"Authorization": f"Bearer {who['mandate']}", "X-Bot-Id": who["bot_id"]}, who["customer_id"]

    def order_view(order: dict, include_receipt: bool = False) -> dict:
        view = {key: order.get(key) for key in ("id", "status", "tier", "total", "notice", "continue_url", "record_id", "earliest_orderable_date")}
        view["checks"] = [{"name": c["name"], "passed": c["passed"], "reason": c["reason"]} for c in order.get("checks", [])]
        view["lines"] = [{"product_id": l.get("product_id"), "name": l.get("name"), "packs": l.get("packs"), "total": l.get("total"), "delivery_date": l.get("delivery_date")} for l in (order.get("offer_payload") or {}).get("lines", [])]
        view["receipt_issued"] = bool(order.get("receipt"))
        for key in ("approvable", "requires_price_confirmation"):
            if key in order:
                view[key] = order[key]
        if order.get("question_spec"):
            view["question_for_buyer"] = order["question_spec"].get("needed")
        if include_receipt:
            view["receipt"] = order.get("receipt")
        if order.get("status") == "requires_escalation":
            view["next_step"] = "Held. Only the buyer can approve or reject it, at continue_url."
        return view

    def remember(result: dict, need: dict) -> dict:
        found = []
        for option in result.get("options", []):
            short = f"opt-{len(options) + 1}"
            options[short] = {"option_id": option["option_id"], "need": need}
            found.append({
                "option": short, "label": option.get("label"), "total_eur": option.get("total"), "delivery_date": option.get("delivery_date"),
                "on_time": option.get("on_time"), "partial": option.get("partial"), "overage": option.get("overage"),
                "lines": [{"product_id": l.get("product_id"), "name": l.get("name"), "packs": l.get("packs"), "pack_size": l.get("pack_size"), "quantity": l.get("quantity"), "unit": l.get("unit"), "unit_price": l.get("unit_price"), "total": l.get("total"), "missing_properties": l.get("missing_properties")} for l in option.get("lines", [])],
            })
        return {"options": found, "filters_applied": result.get("filters_applied"), "product_selection": result.get("product_selection"), "earliest_orderable_date": result.get("earliest_orderable_date")}

    @server.tool()
    def search_catalogue(text: str, properties: list[str] | None = None, delivery_date: str | None = None, limit: int = 10) -> dict[str, Any]:
        """Browse what the door sells. With delivery_date (YYYY-MM-DD) each product has the buyer's
        price and availability for that date. Creates no offer and no order."""
        params: dict[str, Any] = {"text": text, "limit": max(1, min(limit, 100))}
        if properties:
            params["properties"] = properties
        if delivery_date:
            params.update(customer_id=identity()["customer_id"], delivery_date=delivery_date)
            return call("GET", "/catalog/search", params=params)
        products = call("GET", "/catalog/search", params=params)["products"]
        return {"products": [{"product_id": p["id"], "name": p.get("name"), "product_type": p.get("product_type"), "properties": sorted(p.get("properties") or {})} for p in products]}

    @server.tool()
    def match_need(product_type: str, quantity: float, unit: str, deadline: str, properties: list[str] | None = None, product_id: str | None = None) -> dict[str, Any]:
        """State a need and get up to three ranked options with whole packs, total price and delivery date.
        unit is L, KG or PACK; deadline is YYYY-MM-DD. Give properties only if the buyer stated them
        (for example "lactose-free"). product_id selects one exact product. Nothing is reserved."""
        need: dict[str, Any] = {"product_type": product_type, "properties": properties or [], "quantity": quantity, "unit": unit.upper(), "deadline": deadline}
        if product_id:
            need["product_id"] = product_id
        return remember(call("POST", "/needs/match", json={**body(), "need": need}), need)

    @server.tool()
    def request_offer(option: str) -> dict[str, Any]:
        """Ask the door for a signed, fixed-price offer on one option from match_need (for example "opt-1").
        The offer is valid for a few minutes and reserves no stock."""
        if option not in options:
            raise ToolError(f"Unknown option {option!r}. Call match_need first and pass one of its option ids.")
        chosen = options[option]
        result = call("POST", "/offers", json={**body(), "option_id": chosen["option_id"], "need": chosen["need"]})
        short = f"offer-{len(offers) + 1}"
        offers[short] = {"offer": result["offer"], "need": chosen["need"], "key": str(uuid.uuid4())}
        payload = result["payload"]
        return {"offer": short, "total_eur": payload.get("total"), "currency": payload.get("currency"), "prices_include_vat": payload.get("prices_include_vat"), "delivery_date": payload.get("delivery_date"), "expires_at": payload.get("expires_at"), "reserves_stock": payload.get("reserves_stock")}

    @server.tool()
    def place_order(offer: str) -> dict[str, Any]:
        """Place the order for a signed offer from request_offer (for example "offer-1"). The door runs
        its ten checks against the buyer's signed limits and commits, commits with a notice, or holds
        the order for the buyer. Calling again with the same offer returns the same order, never a second one."""
        if offer not in offers:
            raise ToolError(f"Unknown offer {offer!r}. Call request_offer first and pass its offer id.")
        held = offers[offer]
        order = call("POST", "/orders", json={**body(), "offer": held["offer"], "need": held["need"]}, headers={"Idempotency-Key": held["key"]})
        return order_view(order)

    @server.tool()
    def get_order(order_id: str, include_receipt: bool = False) -> dict[str, Any]:
        """Read an order's current state. include_receipt adds the door-signed receipt (a long JWS)."""
        headers, _ = read_headers()
        return order_view(call("GET", f"/orders/{order_id}", headers=headers), include_receipt)

    @server.tool()
    def get_limits() -> dict[str, Any]:
        """Read the limits the buyer signed: per-order cap, monthly budget with used and remaining,
        product scope, must-hold properties and how far the agent may depart from the usual order."""
        headers, customer_id = read_headers()
        return call("GET", "/limits", params={"customer_id": customer_id}, headers=headers)

    def usual_basket() -> dict:
        headers, customer_id = read_headers()
        return call("GET", "/usual-order", params={"customer_id": customer_id}, headers=headers)

    @server.tool()
    def get_usual_order() -> dict[str, Any]:
        """Read the buyer's confirmed usual basket, if the buyer has an order history."""
        usual = usual_basket()
        lines = [{"product_id": l.get("product_id"), "name": l.get("name"), "product_type": l.get("product_type"), "packs": l.get("packs"), "pack_size": l.get("pack_size"), "unit": l.get("unit"), "required_properties": l.get("required_properties")} for l in usual.get("lines", [])]
        return {"lines": lines, "total_eur": usual.get("total"), "label": usual.get("label"), "history_label": usual.get("history_label"), "synthetic": usual.get("synthetic")}

    @server.tool()
    def match_usual_order(deadline: str) -> dict[str, Any]:
        """Price the buyer's usual basket, unchanged, for a new delivery date (YYYY-MM-DD).
        Returns options to pass to request_offer, like match_need. Nothing is reserved."""
        usual = usual_basket()
        lines = [{"product_id": l["product_id"], "product_type": l.get("product_type"), "quantity": l["packs"], "unit": "PACK", "properties": l.get("required_properties") or []} for l in usual.get("lines", [])]
        if not lines or not usual.get("anchor_id"):
            raise ToolError("This buyer has no confirmed usual order.")
        need = {"product_type": "usual basket", "anchor_id": usual["anchor_id"], "properties": [], "deadline": deadline, "lines": lines}
        return remember(call("POST", "/needs/match", json={**body(), "need": need}), need)

    @server.tool()
    def get_record(record_id: str) -> dict[str, Any]:
        """Read the buyer-readable decision record for an order: what was asked, what was checked, and why."""
        return call("GET", f"/records/{record_id}")

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--door", default=os.getenv("AIMO_DOOR_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--customer", default=None, help="Demo customer (hotel or restaurant); omit to use AIMO_MANDATE and AIMO_BOT_ID")
    parser.add_argument("--access-code", default=os.getenv("AIMO_ACCESS_CODE"), help="Only if the demo is behind an access code")
    args = parser.parse_args()
    headers = {"X-Demo-Access-Code": args.access_code} if args.access_code else {}
    http = httpx.Client(base_url=args.door.rstrip("/"), headers=headers, timeout=60)
    try:
        server = build_server(http, customer=args.customer, mandate=os.getenv("AIMO_MANDATE"), bot_id=os.getenv("AIMO_BOT_ID"))
    except ValueError as error:
        parser.error(str(error))
    server.run("stdio")


if __name__ == "__main__":
    main()
