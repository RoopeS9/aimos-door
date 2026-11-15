"""O. The door as MCP tools (examples/mcp_server.py)."""
from __future__ import annotations

import importlib.util

from fastapi.testclient import TestClient
from mcp import Client

from door.main import create_app
from conftest import ROOT, monday

HUMAN_ONLY = ("signoff", "sign_off", "approve", "reject", "undo", "reset", "change", "accept", "mandate", "session", "identify", "acknowledge")


def load_example():
    spec = importlib.util.spec_from_file_location("example_mcp_server", ROOT / "examples" / "mcp_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_O01_mcp_client_buys_and_cannot_approve(tmp_path):
    with TestClient(create_app(tmp_path, fixture=True)) as door:
        server = load_example().build_server(door, customer="hotel")
        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == {"search_catalogue", "match_need", "request_offer", "place_order", "get_order", "get_limits", "get_usual_order", "match_usual_order", "get_record"}
            assert not [name for name in names for word in HUMAN_ONLY if word in name]

            async def tool(name, **arguments):
                result = await client.call_tool(name, arguments)
                assert not result.is_error, result.content
                return result.structured_content

            products = (await tool("search_catalogue", text="milk", delivery_date=monday()))["products"]
            assert products and products[0]["price_eur"] is not None
            match = await tool("match_need", product_type="drinking milk", quantity=40, unit="L", deadline=monday(), properties=["lactose-free"], product_id=products[0]["product_id"])
            option = match["options"][0]
            assert option["option"] == "opt-1" and option["lines"][0]["product_id"] == products[0]["product_id"]
            offer = await tool("request_offer", option=option["option"])
            assert offer["offer"] == "offer-1" and offer["total_eur"] == option["total_eur"] and offer["reserves_stock"] is False
            order = await tool("place_order", offer=offer["offer"])
            assert order["status"] == "committed" and order["receipt_issued"] and len(order["checks"]) == 10
            assert all(check["passed"] for check in order["checks"])
            # The model never sees a bearer document, and a repeat is the same order.
            assert "eyJ" not in str([match, offer, order])
            assert (await tool("place_order", offer=offer["offer"]))["id"] == order["id"]
            assert (await tool("get_order", order_id=order["id"]))["status"] == "committed"
            assert (await tool("get_record", record_id=order["record_id"]))["order_id"] == order["id"]
            assert (await tool("get_limits"))["product_scope_label"]

            # A held order stays held: the agent gets the buyer's link and has no tool to approve it.
            big = await tool("match_need", product_type="drinking milk", quantity=5000, unit="L", deadline=monday(), properties=["lactose-free"])
            held = await tool("place_order", offer=(await tool("request_offer", option=big["options"][0]["option"]))["offer"])
            assert held["status"] == "requires_escalation" and held["continue_url"] and not held["receipt_issued"]
            unknown = await client.call_tool("request_offer", {"option": "opt-99"})
            assert unknown.is_error
