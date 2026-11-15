"""Engineering-owned acceptance cases N01-N03 and N12 from buyer testing."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from conftest import auth, buyer, human_auth, matching, need, new_mandate, order as place_order
from door.catalog import Catalog
from door.mandates import DEMO_ALLOWED_CATEGORIES, DEMO_MUST_HOLD_CATEGORIES
from reference_bot.conversation import stated_properties
from reference_bot.providers import next_monday


def _inject_bot_http(client):
    connection = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app), base_url="http://testserver",
    )
    client.app.state.bot_http_client = connection
    return connection


def _converse(client, message, *, history=None, customer_id="hotel"):
    response = client.post("/bot/chat", json={
        "stage": "converse", "customer_id": customer_id, "message": message,
        "history": history or [],
    })
    assert response.status_code == 200, response.text
    return response.json()


class _PropertyProvider:
    """A hostile model stub: requirements and restatements cannot be trusted."""

    async def converse(self, message, history):
        metadata = {"provider": "fixture", "id": "invented-property-stub", "fixture": True}
        if "same as last week" in message.casefold():
            return {
                "name": "place_order",
                "arguments": {"reorder": {"deadline": next_monday().isoformat(), "adjustments": []}, "restatement": "Invented property."},
                "model": metadata,
            }
        need = {
            "product_type": "drinking milk", "properties": ["FREE_FROM_LACTOSE"],
            "quantity": 12, "unit": "PACK", "deadline": next_monday().isoformat(),
            "preferences": {},
        }
        return {
            "name": "place_order",
            "arguments": {"need": need, "restatement": "12 packs of lactose-free wine."},
            "model": metadata,
        }


def _browse_fixture(path):
    catalog = Catalog(path / "browse.sqlite3")
    groups = {
        "oat": ("plant-drinks", "other", "Oat drink {index} 1 L", "Kauramaito {index} 1 L", 6),
        "milk": ("dairy-milk", "drinking milk", "Dairy milk {index} 1 L", "Maito {index} 1 L", 6),
        "chocolate": ("sweets", "other", "Milk chocolate {index} 100 g", "Maitosuklaa {index} 100 g", 4),
        "wine": ("wines", "other", "Cellar wine {index} 750 ml", "Viini {index} 750 ml", 6),
        "vinegar": ("vinegars", "other", "Wine vinegar {index} 500 ml", "Viinietikka {index} 500 ml", 4),
        "sauce": ("sauces", "other", "Wine sauce {index} 400 g", "Viinikastike {index} 400 g", 4),
        "cream": ("creams", "cream", "Whipping cream {index} 3 dl", "Kuohukerma {index} 3 dl", 6),
        "ice-cream": ("frozen-desserts", "other", "Vanilla ice cream {index} 500 ml", "Vaniljajäätelö {index} 500 ml", 4),
        "butter": ("butter", "butter", "Butter {index} 400 g", "Voi {index} 400 g", 6),
        "croissant": ("bakery", "other", "Butter croissant {index} 80 g", "Voicroissant {index} 80 g", 4),
        "dish": ("kitchenware", "other", "Butter dish {index} 1 pcs", "Voastia {index} 1 kpl", 4),
    }
    for prefix, (category, kind, english, finnish, count) in groups.items():
        for index in range(1, count + 1):
            names = {"en": english.format(index=index), "fi": finnish.format(index=index)}
            catalog.upsert({
                "id": f"{prefix}-{index}", "gtin": None, "names": names,
                "name": names["en"], "category": category, "brand": "Browse fixture",
                "properties": {}, "product_type": kind, "storage_class": 1,
            })
    return catalog


def test_N01(tmp_path):
    """Browsing ranks the requested shelf above products that merely share a word."""
    catalog = _browse_fixture(tmp_path)
    for query, prefix in (
        ("oat milk", "oat"), ("kauramaito", "oat"), ("milk", "milk"),
        ("wine", "wine"), ("cream", "cream"), ("butter", "butter"),
    ):
        products = catalog.search_relevant(query, limit=6)
        assert len(products) == 6, (query, [product["id"] for product in products])
        assert all(product["id"].startswith(prefix + "-") for product in products), (
            query, [product["id"] for product in products]
        )


def test_N02(client):
    """Only buyer-authored properties survive planning, matching and restatement."""
    assert stated_properties(
        ["lactose-free", "gluten-free", "organic"], ["What wine do you have?"],
    ) == []
    for proposed, buyer_text, canonical in (
        ("lactose free", "Make it Lactose-Free", "lactose-free"),
        ("FREE_FROM_LACTOSE", "Tarvitsen laktoositonta maitoa", "lactose-free"),
        ("FREE_FROM_GLUTEN", "gluten_free, please", "gluten-free"),
        ("gluten-free", "gluteenitonta", "gluten-free"),
        ("organic", "organic", "organic"),
        ("organic", "luomua", "organic"),
        ("cane-sugar", "cane sugar", "cane-sugar"),
    ):
        assert stated_properties([proposed], [buyer_text]) == [canonical]

    new_mandate(client, must_hold=[])
    client.app.state.bot_provider = _PropertyProvider()
    connection = _inject_bot_http(client)
    try:
        history = [
            {"role": "user", "text": "What wine do you have?"},
            {"role": "assistant", "text": "The first product.", "action": "products", "products": [
                {"product_id": "milk-unknown", "name": "Fixture milk with no claim"},
            ], "draft": None},
        ]
        result = _converse(client, "I'll take 12 packs of the first one by Monday", history=history)
        assert result["action"] == "order" and result["order"]["status"] == "committed"
        assert result["need"]["product_id"] == "milk-unknown"
        assert result["need"]["properties"] == result["match"]["need"]["properties"] == []
        assert "lactose" not in result["restatement"].casefold()
        assert result["order"]["checks"][3]["passed"], result["order"]["checks"][3]
        assert not any(line["missing_properties"] for line in result["order"]["offer_payload"]["lines"])

        stated = _converse(client, "I'll take 12 packs of lactose free milk by Monday")
        assert stated["need"]["properties"] == ["lactose-free"]
        assert "lactose-free" in stated["restatement"]

        # The legacy execute route applies the same rule even if an old preview
        # supplied an invented property and a model-authored restatement.
        executed = client.post("/bot/chat", json={
            "stage": "execute", "customer_id": "hotel",
            "message": "Order 12 packs of milk by Monday",
            "need": {
                "product_type": "drinking milk", "properties": ["lactose-free"],
                "quantity": 12, "unit": "PACK", "deadline": next_monday().isoformat(),
                "preferences": {},
            },
            "restatement": "Invented lactose-free requirement.",
        })
        assert executed.status_code == 200, executed.text
        assert executed.json()["need"]["properties"] == []
        assert "lactose" not in executed.json()["restatement"].casefold()
        assert executed.json()["order"]["status"] == "committed"

        # Confirmed properties belong to the basket, not to model invention.
        usual = client.get(
            "/usual-order", params={"customer_id": "restaurant"},
            headers=human_auth(client, "restaurant"),
        ).json()["lines"]
        expected = {
            str(line.get("id") or line["product_id"]): line.get("required_properties", [])
            for line in usual
        }
        reordered = _converse(
            client, "Same as last week", customer_id="restaurant",
        )
        assert reordered["need"]["product_type"] == "usual basket"
        assert {line["product_id"]: line["properties"] for line in reordered["need"]["lines"]} == expected
        assert any(expected.values()), "The fixture must exercise confirmed basket properties."
    finally:
        asyncio.run(connection.aclose())


def test_N03(client):
    """A no-later-date cutoff hold is honestly reject-only, never a dead end."""
    service = client.app.state.service
    requested = need()["deadline"]
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    # K08 opens a later cutoff; N03 closes the requested day and all seven days
    # that the matcher is allowed to probe for a later offer.
    for offset in range(8):
        day = (datetime.fromisoformat(requested).date() + timedelta(days=offset)).isoformat()
        service.valio.fixture_availability[("milk-10", day)] = {
            "available": 10000, "delivery_date": day, "cutoff": past,
            "sales_block": False, "logistic_model": "Stock", "deliverable": True,
            "subject_to_supplier": False, "source": "synthetic N03 no-later-date cutoff fixture",
        }
    authority = auth(client)
    request_need = need(quantity=100, product_ids=["milk-10"])
    matched = matching(client, authority, request_need)
    assert matched["earliest_orderable_date"] is None
    assert matched["options"][0]["kind"] == "shortage_unresolved"

    held = place_order(client, authority, request_need=request_need)
    assert held["status"] == "requires_escalation"
    assert held["approvable"] is False and held["earliest_orderable_date"] is None
    assert [answer["value"] for answer in held["question_spec"]["answers"]] == ["reject"]
    needed = held["question_spec"]["needed"]
    assert "cannot be delivered" in needed and requested in needed
    assert "cutoff" in needed and "no later delivery date is known" in needed
    assert "rejected or replaced by a new request" in needed
    assert "cutoff" in held["checks"][4]["reason"].casefold()

    human = human_auth(client)
    fetched_response = client.get(f"/orders/{held['id']}", headers=human)
    assert fetched_response.status_code == 200, fetched_response.text
    fetched = fetched_response.json()
    assert fetched["status"] == "requires_escalation" and fetched["approvable"] is False
    assert fetched["question_spec"]["hash"] == held["question_spec"]["hash"]
    assert fetched["offer"] == held["offer"], "A terminal hold must not invent a fresh offer."

    refused = client.post(
        f"/orders/{held['id']}/signoff", json={"action": "approve"}, headers=human,
    )
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail == {"message": needed, "earliest_orderable_date": None}

    connection = _inject_bot_http(client)
    try:
        interview = client.post("/bot/chat", json={
            "stage": "interview", "customer_id": "hotel", "order_id": held["id"],
        })
        assert interview.status_code == 200, interview.text
        question = interview.json()
        assert [answer["value"] for answer in question["accepted_answers"]] == ["reject"]
        assert "approv" not in question["question"].casefold()
        assert "accept" not in question["question"].casefold()

        approval = client.post("/bot/chat", json={
            "stage": "answer", "customer_id": "hotel", "order_id": held["id"],
            "message": "approve", "scope": "just_this_once",
        })
        assert approval.status_code == 200, approval.text
        assert "confirmation_summary" not in approval.json()
        assert "not available" in approval.json()["clarification"]

        rejection = client.post("/bot/chat", json={
            "stage": "answer", "customer_id": "hotel", "order_id": held["id"],
            "message": "reject", "scope": "just_this_once",
        })
        assert rejection.status_code == 200, rejection.text
        pending = rejection.json()["pending_answer"]
        assert pending["answer"] == "reject"
        confirmed = client.post("/bot/chat", headers=human, json={
            "stage": "confirm", "customer_id": "hotel", "order_id": held["id"],
            "confirmed": True, "confirmation_summary": pending["confirmation_summary"],
            "question_hash": pending["question_hash"],
        })
        assert confirmed.status_code == 200, confirmed.text
        rejected = confirmed.json()["order"]
        assert rejected["status"] == "rejected" and rejected["reserved"] == []
        with service.store.connect() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM reservations WHERE order_id=? AND active=1", (held["id"],),
            ).fetchone()[0] == 0
    finally:
        asyncio.run(connection.aclose())


def _exact_product_need(product_id, *, product_type="other", properties=None, quantity=1, unit="PACK"):
    return need(
        product_type=product_type, product_id=product_id, product_ids=[product_id],
        properties=properties or [], quantity=quantity, unit=unit,
    )


def test_N12(client):
    """Food and drink are explorable; only dairy inherits the lactose-free hold."""
    non_food = (
        "200", "300", "329", "630", "631", "632", "633", "639", "640",
        "641", "643", "645", "677", "678", "777", "900",
    )
    for customer_id in ("hotel", "restaurant"):
        row = buyer(client, customer_id)
        limits = row["mandate_payload"]["limits"]
        assert limits["allowed_categories"] == list(DEMO_ALLOWED_CATEGORIES)
        assert limits["must_hold_categories"] == list(DEMO_MUST_HOLD_CATEGORIES)
        assert all(not any(category.startswith(prefix) for prefix in non_food) for category in limits["allowed_categories"])
        assert limits["allowed_categories_label"] == row["product_scope_label"] == "Food and drink"
        assert limits["must_hold_label"] == row["must_hold_label"] == "Lactose-free for dairy products"

    limits_view = client.get("/limits", params={"customer_id": "hotel"}, headers=human_auth(client)).json()
    assert limits_view["product_scope_label"] == "Food and drink"
    assert limits_view["must_hold_label"] == "Lactose-free for dairy products"

    # A plant drink shares category family 104 with milk but is not in a dairy
    # subfamily. With no buyer-stated property, neither check may invent a hold.
    oat_need = _exact_product_need("oat-drink", quantity=2)
    oat_match = matching(client, request_need=oat_need)
    assert oat_match["need"]["properties"] == []
    assert oat_match["options"][0]["lines"][0]["id"] == "oat-drink"
    oat = place_order(client, request_need=oat_need)
    assert oat["status"] == "committed", oat["checks"]
    checks = {check["name"]: check for check in oat["checks"]}
    assert checks["Product scope"]["passed"] and checks["Must-hold properties"]["passed"]
    assert not oat["offer_payload"]["lines"][0]["missing_properties"]

    dairy_need = _exact_product_need(
        "milk-unknown", product_type="drinking milk", quantity=2, unit="L",
    )
    dairy = place_order(client, request_need=dairy_need)
    assert dairy["status"] == "requires_escalation"
    assert dairy["checks"][2]["passed"] and not dairy["checks"][3]["passed"]
    assert "no manufacturer statement" in dairy["checks"][3]["reason"]
    question = dairy["question_spec"]
    assert question["kind"] == "property_confirmation"
    assert question["missing_properties"] == [{
        "product_id": "milk-unknown", "product_name": "Fixture milk with no claim",
        "property": "lactose-free", "contradicted": False,
    }]
    assert {answer["value"] for answer in question["answers"]} == {"approve", "reject"}

    cleaning = place_order(client, request_need=_exact_product_need("cleaning-spray"))
    assert cleaning["status"] == "requires_escalation"
    assert not cleaning["checks"][2]["passed"] and cleaning["checks"][3]["passed"]
    assert "outside the buyer's allowed categories" in cleaning["checks"][2]["reason"]
    assert cleaning["offer_payload"]["lines"][0]["category"].startswith("631")

    # Category scoping never discards a property the buyer stated directly.
    stated = place_order(client, request_need=_exact_product_need("462090", properties=["lactose-free"]))
    assert stated["checks"][2]["passed"] and not stated["checks"][3]["passed"]
    assert stated["question_spec"]["kind"] == "property_confirmation"
    assert stated["question_spec"]["missing_properties"][0]["product_id"] == "462090"

    reset = client.post("/demo/reset", headers=human_auth(client))
    assert reset.status_code == 200, reset.text
    for customer_id in ("hotel", "restaurant"):
        limits = buyer(client, customer_id)["mandate_payload"]["limits"]
        assert limits["allowed_categories"] == list(DEMO_ALLOWED_CATEGORIES)
        assert limits["must_hold_categories"] == list(DEMO_MUST_HOLD_CATEGORIES)

    # Exercise the three existing acts through their actual fixture bot and door
    # APIs, not just their individual check functions.
    connection = _inject_bot_http(client)
    try:
        act_one = client.post("/bot/chat", json={
            "customer_id": "hotel", "message": "I need 1,000 litres of lactose-free milk by Monday.",
        })
        assert act_one.status_code == 200, act_one.text
        first = act_one.json()["order"]
        assert first["status"] == "committed" and first["receipt"]
        assert all(check["passed"] for check in first["checks"])

        act_two = client.post("/bot/chat", json={
            "customer_id": "hotel", "message": "I need 1,000 litres of lactose-free milk by Monday.",
            "simulate_limited_stock": True,
        })
        assert act_two.status_code == 200, act_two.text
        second = act_two.json()["order"]
        assert second["status"] == "requires_escalation" and second["reserved"]
        approved = client.post(
            f"/orders/{second['id']}/signoff", json={"action": "approve"}, headers=human_auth(client),
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "committed" and approved.json()["receipt"]

        baseline = client.get(
            "/usual-order", params={"customer_id": "restaurant"}, headers=human_auth(client, "restaurant"),
        ).json()
        before = client.get(
            "/limits", params={"customer_id": "restaurant"}, headers=human_auth(client, "restaurant"),
        ).json()
        act_three = client.post("/bot/chat", json={
            "customer_id": "restaurant", "message": "Same as last week, plus 5 litres more milk.",
        })
        assert act_three.status_code == 200, act_three.text
        third = act_three.json()["order"]
        assert third["status"] == "committed" and third["tier"] == "Notify" and third["notice"]
        assert {line["id"] for line in third["offer_payload"]["lines"]} == {line["id"] for line in baseline["lines"]}
        undone = client.post(
            f"/orders/{third['id']}/undo", headers=human_auth(client, "restaurant"),
        )
        assert undone.status_code == 200 and undone.json()["status"] == "withdrawn"
        old_freedom = next(row for row in before["rows"] if row["id"] == "departure")["current_freedom"]
        after = client.get(
            "/limits", params={"customer_id": "restaurant"}, headers=human_auth(client, "restaurant"),
        ).json()
        assert next(row for row in after["rows"] if row["id"] == "departure")["current_freedom"] < old_freedom
    finally:
        asyncio.run(connection.aclose())
