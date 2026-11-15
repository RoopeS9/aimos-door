"""Bot orchestration across the public HTTP contract, with no door internals."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
import uuid

import httpx

from reference_bot.providers import ProviderError, ProviderLimitError, interview_contract, next_monday, validate_basket, validate_plan
from reference_bot.conversation import (
    buyer_texts, deadline_is_stated, has_buying_intent, is_affirmative, is_negative,
    asks_about_limits, asks_about_usual, asks_for_cheapest, is_complete_instruction, is_undo_request,
    latest_draft, latest_products, product_is_stated, quantity_is_stated, selected_product_id, stated_properties,
)


class DoorAPIError(RuntimeError):
    pass


def event(kind: str, text: str) -> dict:
    return {"role": "assistant", "kind": kind, "text": text, "at": datetime.now(timezone.utc).isoformat()}


class ReferenceBot:
    def __init__(self, client: httpx.AsyncClient, provider=None, before_model=None, pending_confirmations=None, read_headers=None):
        self.client, self.provider, self.before_model = client, provider, before_model
        self.pending_confirmations = pending_confirmations if pending_confirmations is not None else {}
        # These contain only mandate/bot read authority, never a buyer session.
        self.read_headers = read_headers or {}

    def _admit_model(self):
        if self.before_model:
            self.before_model()

    @staticmethod
    def _buyer_property_plan(plan: dict, message: str, history: list[dict] | None = None) -> dict:
        """Rebuild the plan and restatement after enforcing buyer-authored claims."""
        need = plan.get("need") or {}
        if plan.get("intent") == "reorder" or need.get("product_type") == "usual basket":
            return plan
        properties = stated_properties(need.get("properties", []), buyer_texts(message, history or []))
        checked = validate_plan({"need": {**need, "properties": properties}}, message)
        return {**plan, **checked}

    async def preview(self, message: str, customer_id: str = "hotel") -> dict:
        if self.provider is None:
            raise ProviderError("No model provider is configured.")
        self._admit_model()
        plan = await self.provider.plan(message)
        if plan.get("intent") == "reorder":
            expanded = await self._expand_reorder(customer_id, plan)
            plan = {**plan, **expanded}
        else:
            plan = self._buyer_property_plan(plan, message)
        return {**plan, "messages": [event("restatement", plan["restatement"])], "stage": "preview"}

    async def _expand_reorder(self, customer_id: str, plan: dict) -> dict:
        usual = await self._request("GET", "/usual-order", params={"customer_id": customer_id}, headers=self.read_headers.get(customer_id, {}))
        if usual.get("customer_id") != customer_id or usual.get("anchor_confirmed") is not True or not usual.get("anchor_id") or not usual.get("lines"):
            raise DoorAPIError("This customer has no confirmed usual basket. State the products and quantities explicitly.")
        original = usual["lines"]
        lines = []
        for line in original:
            try:
                packs = float(line["packs"])
                if not math.isfinite(packs) or packs <= 0 or not packs.is_integer():
                    raise ValueError()
                properties = line.get("required_properties")
                if properties is None:
                    properties = [prop for prop, source in line.get("properties", {}).items() if source == "manufacturer"]
                lines.append({"product_id": str(line.get("id") or line["product_id"]), "product_type": line["product_type"], "name": line.get("name") or str(line.get("id")), "quantity": packs, "unit": "PACK", "properties": properties})
            except (KeyError, ValueError, TypeError):
                raise DoorAPIError("The door's usual basket has an incomplete pack quantity. No reorder was submitted.") from None
        adjustments = plan["reorder"]["adjustments"]
        changed = set()
        for adjustment in adjustments:
            selected = [index for index, line in enumerate(lines) if line["product_type"] == adjustment["product_type"]]
            if len(selected) != 1:
                raise DoorAPIError("The requested change does not identify exactly one product in the usual basket. Please name a specific line.")
            index = selected[0]
            old, requested = original[index], lines[index]
            unit = adjustment["unit"]
            if index not in changed:
                if unit == "PACK":
                    requested["quantity"] = float(old["packs"])
                elif str(old.get("unit", "")).upper() == unit:
                    requested["quantity"] = float(old["quantity"])
                else:
                    raise DoorAPIError("The requested adjustment uses a different unit from the confirmed basket. State it in the basket's unit.")
                requested["unit"] = unit
            elif requested["unit"] != unit:
                raise DoorAPIError("Use a single unit for changes to the same usual-order line.")
            requested["quantity"] += adjustment["quantity"]
            changed.add(index)
        need = {"product_type": "usual basket", "quantity": 1, "unit": "PACK", "properties": [], "deadline": plan["reorder"]["deadline"], "anchor_id": usual["anchor_id"], "lines": lines, "reorder_adjustments": adjustments, "request": plan.get("request", "")}
        return validate_basket(need, plan.get("request", ""))

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        # Tool arguments never supply a URL or arbitrary path. Only the public
        # purchasing contract below can be called by this bot.
        if (method, path) not in {
            ("GET", "/demo/customers"), ("GET", "/usual-order"), ("GET", "/limits"), ("GET", "/catalog/search"), ("POST", "/needs/match"),
            ("POST", "/offers"), ("POST", "/orders"),
        } and not ((method == "GET" and re.fullmatch(r"/orders/[A-Za-z0-9_-]{1,200}", path)) or (method == "POST" and re.fullmatch(r"/orders/[A-Za-z0-9_-]{1,200}/signoff", path))):
            raise DoorAPIError("This bot cannot call that API operation.")
        try:
            response = await self.client.request(method, path, **kwargs)
            if response.status_code >= 400:
                if response.status_code == 429:
                    try:
                        detail = response.json().get("detail", {})
                    except (ValueError, AttributeError):
                        detail = {}
                    if isinstance(detail, dict) and detail.get("reason") == "usage_or_credit_limit":
                        raise ProviderLimitError("The door's model provider reported a usage or credit limit.")
                raise DoorAPIError(f"The door refused {path} (HTTP {response.status_code}). No automatic correction was attempted.")
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except httpx.HTTPError:
            raise DoorAPIError("The door could not be reached. If an order request timed out, retry with the same idempotency key before submitting a new order.") from None
        except ValueError:
            raise DoorAPIError("The door returned an unreadable response.") from None

    async def execute(self, customer_id: str, need: dict, *, message: str = "", restatement: str | None = None, simulate_limited_stock: bool = False, idempotency_key: str | None = None, messages: list | None = None, history: list[dict] | None = None) -> dict:
        source_message = message or need.get("request", "")
        plan = validate_plan({"need": need, "restatement": restatement or ""}, source_message)
        plan = self._buyer_property_plan(plan, source_message, history)
        need, restatement = plan["need"], plan["restatement"]
        events = list(messages) if messages else [event("restatement", restatement)]
        customers = await self._request("GET", "/demo/customers")
        customer = next((item for item in customers.get("customers", []) if item.get("id") == customer_id), None)
        if not customer:
            raise DoorAPIError("Choose a valid demo customer.")
        identity = {"mandate": customer["mandate"], "bot_id": customer["bot_id"]}
        if simulate_limited_stock:
            need["simulate_limited_stock"] = True
        match = await self._request("POST", "/needs/match", json={**identity, "need": need})
        options = match.get("options") or []
        if not options:
            return {"stage": "complete", "need": need, "restatement": restatement, "match": match, "offer": None, "order": None, "messages": [*events, event("no_match", "The door found no matching option. No order was placed; the requested properties and deadline remain unchanged.")]}
        chosen = options[0]
        events.append(event("matching", f"The door returned {len(options)} ranked option(s), with the applied filters shown. I will request its {chosen.get('label', 'best')} option."))
        offer = await self._request("POST", "/offers", json={**identity, "option": chosen, "need": need})
        events.append(event("offer", "The door issued a signed offer. An offer reserves no stock."))
        request_key = idempotency_key or str(uuid.uuid4())
        order = await self._request("POST", "/orders", json={**identity, "offer": offer["offer"], "need": need}, headers={"Idempotency-Key": request_key})
        if order.get("status") == "committed":
            text = "The door committed the order inside the buyer's signed limits and issued a signed receipt."
            if isinstance(order.get("total"), (int, float)):
                text += f" Total €{order['total']:,.2f} excluding VAT."
            if order.get("notice"):
                text = str(order["notice"])
        elif order.get("status") == "requires_escalation":
            reasons = [item.get("reason", "A check needs attention.") for item in order.get("checks", []) if not item.get("passed")]
            text = "The door held the order for sign-off. " + " ".join(reasons)
        else:
            text = "The door did not commit the order. Review its decision record."
        events.append(event("outcome", text))
        return {"stage": "complete", "customer_id": customer_id, "need": need, "restatement": restatement, "match": match, "offer": offer, "order": order, "messages": events, "idempotency_key": request_key}

    async def chat(self, customer_id: str, message: str, **kwargs) -> dict:
        plan = await self.preview(message, customer_id)
        result = await self.execute(customer_id, plan["need"], message=message, restatement=plan["restatement"], messages=plan["messages"], **kwargs)
        return {**result, "model": plan.get("model")}

    @staticmethod
    def _conversation_response(action: str, reply: str, model: dict | None, **extra) -> dict:
        return {"stage": "conversation", "action": action, "reply": reply, "model": model, **extra}

    async def _limits_answer(self, customer_id: str, model: dict | None) -> dict:
        limits = await self._request("GET", "/limits", params={"customer_id": customer_id}, headers=self.read_headers.get(customer_id, {}))
        rows = {row.get("id"): row for row in limits.get("rows", [])}
        money = lambda value: f"€{float(value):,.2f}"
        parts = []
        if "per_order_cap" in rows:
            parts.append(f"up to {money(rows['per_order_cap']['signed_ceiling'])} per order")
        if "monthly_budget" in rows:
            budget = rows["monthly_budget"]
            used = f", of which {money(budget['used'])} is used and {money(budget['remaining'])} is left" if budget.get("used") is not None and budget.get("remaining") is not None else ""
            parts.append(f"a monthly budget of {money(budget['signed_ceiling'])}{used}")
        reply = "Your signed limits: " + "; ".join(parts) + "." if parts else "I could not read your signed limits just now."
        if "departure" in rows:
            departure = rows["departure"]
            freedom, ceiling = float(departure.get("current_freedom") or 0), float(departure.get("signed_ceiling") or 0)
            reply += f" When an order differs from your usual one, I may act alone up to {freedom:.0%} per line and with a notice up to {ceiling:.0%}; beyond that the order waits for your sign-off."
        reply += " Only you can change these limits, in the Limits view."
        return self._conversation_response("reply", reply, model)

    async def _usual_answer(self, customer_id: str, model: dict | None) -> dict:
        usual = await self._request("GET", "/usual-order", params={"customer_id": customer_id}, headers=self.read_headers.get(customer_id, {}))
        lines = usual.get("lines") or []
        if not lines:
            return self._conversation_response("reply", "You have no usual order yet: this customer has no order history. Tell me what you need and I will look it up.", model)
        def describe(line):
            quantity, unit = line.get("quantity"), line.get("unit")
            amount = f"{float(quantity):g} {unit}" if quantity is not None and unit else f"{line.get('packs', '?')} packs"
            return f"{amount} of {line.get('name', 'a product')}"
        total = f", about €{float(usual['total']):,.2f}" if usual.get("total") is not None else ""
        reply = f"Your usual order has {len(lines)} lines{total}: " + "; ".join(describe(line) for line in lines[:20]) + ". Say “same as last week” to reorder it, with any change you want. Nothing has been ordered."
        return self._conversation_response("reply", reply, model)

    async def _catalog_answer(self, customer_id: str, text: str, properties: list[str], model: dict | None, *, question: str | None = None, cheapest: bool = False) -> dict:
        if isinstance(text, (list, tuple)):
            text = " ".join(str(item) for item in text)
        text = str(text or "products").strip()[:500]
        properties = list(dict.fromkeys(str(prop) for prop in properties if isinstance(prop, str) and prop.strip()))[:10]
        delivery_date = next_monday().isoformat()
        result = await self._request("GET", "/catalog/search", params={"text": text, "properties": properties, "delivery_date": delivery_date, "customer_id": customer_id, "limit": 12 if cheapest else 6})
        products = result.get("products", [])
        best = None
        if cheapest and products:
            def unit_cost(product):
                found = re.search(r"(\d+(?:[.,]\d+)?)", str(product.get("unit_price") or ""))
                value = float(found[1].replace(",", ".")) if found else product.get("price_eur")
                return float(value) if value is not None else float("inf")
            products = sorted(products, key=lambda product: (product.get("availability") not in {"in_stock", "limited"}, unit_cost(product)))
            best = products[0] if products[0].get("availability") in {"in_stock", "limited"} and unit_cost(products[0]) != float("inf") else None
        products = products[:6]
        if question and not products:
            reply = f"I could not find “{text}” in Aimo's catalogue. Aimo sells food, drink and kitchen supplies; tell me what you are looking for in other words and I will search again."
            action = "clarification"
        elif question:
            reply = question
            action = "clarification"
        elif products:
            reply = f"Here is what Aimo has for {text}. Prices are per sales pack, excluding VAT, and availability is for delivery on {delivery_date}."
            if cheapest:
                price = best.get("unit_price") or f"€{float(best['price_eur']):,.2f} per pack" if best else ""
                reply = (f"The cheapest {text} in stock for {delivery_date} is {best['name']} at {price}. The list starts with the cheapest; prices exclude VAT." if best
                         else f"None of the {text} I found is in stock for {delivery_date}, so I cannot name a cheapest one. Here is the list by price.")
            action = "products"
        else:
            wanted = f" that is {' and '.join(properties)}" if properties else ""
            reply = f"I could not find “{text}”{wanted} in Aimo's catalogue. Try a more specific product name, or tell me what it is for."
            action = "clarification"
        return self._conversation_response(action, reply, model, products=products, delivery_date=delivery_date, clarification=question)

    async def _draft_answer(self, customer_id: str, plan: dict, model: dict | None, *, simulate_limited_stock: bool = False) -> dict:
        need, restatement = plan["need"], plan["restatement"]
        if simulate_limited_stock:
            need["simulate_limited_stock"] = True
        customers = await self._request("GET", "/demo/customers")
        customer = next((item for item in customers.get("customers", []) if item.get("id") == customer_id), None)
        if not customer:
            raise DoorAPIError("Choose a valid demo customer.")
        match = await self._request("POST", "/needs/match", json={"mandate": customer["mandate"], "bot_id": customer["bot_id"], "need": need})
        options = match.get("options") or []
        if not options:
            selection = match.get("product_selection") or {}
            question = selection.get("message") or f"I cannot match {restatement} Which comparable product and required properties should I check instead?"
            return await self._catalog_answer(customer_id, need.get("product_type", "products"), need.get("properties", []), model, question=question)
        chosen = options[0]
        selected_line = chosen["lines"][0]
        if selected_line.get("missing_properties"):
            missing = ", ".join(selected_line["missing_properties"])
            question = f"The selected product {selected_line.get('name', selected_line.get('id'))} does not satisfy the required {missing} from manufacturer data. Which listed alternative should I use?"
            return self._conversation_response("clarification", question, model, need=need, match=match, offer=None, order=None)
        packs = int(selected_line["packs"])
        draft = {
            "need": need,
            "restatement": restatement,
            "product_name": selected_line.get("name") or str(selected_line.get("product_id") or selected_line.get("id")),
            "pack": f"{float(selected_line['pack_size']):g} {need['unit']}",
            "packs": packs,
            "total_eur": float(chosen["total"]),
            "delivery_date": chosen["delivery_date"],
        }
        reply = f"Draft only: {draft['packs']} whole packs of {draft['product_name']} ({draft['pack']}), total €{draft['total_eur']:.2f} excluding VAT, proposed delivery {draft['delivery_date']}. Nothing is reserved and no order has been placed. Say 'yes, place it' to confirm."
        return self._conversation_response("draft", reply, model, draft=draft, need=need, restatement=restatement, match=match, offer=None, order=None)

    async def converse(self, customer_id: str, message: str, *, history: list[dict] | None = None, simulate_limited_stock: bool = False, idempotency_key: str | None = None) -> dict:
        if not isinstance(message, str) or not message.strip() or len(message) > 8000:
            raise ProviderError("Enter a message of between 1 and 8,000 characters.")
        if self.provider is None:
            raise ProviderError("No model provider is configured.")
        history = history or []
        # Questions the door can answer exactly are answered from the door, without a model.
        rules = {"provider": "door-rules", "id": "no-model", "fixture": type(self.provider).__name__ == "FixtureProvider", "attempts": 0}
        if asks_about_limits(message):
            return await self._limits_answer(customer_id, rules)
        if asks_about_usual(message):
            return await self._usual_answer(customer_id, rules)
        if is_undo_request(message) and any(turn.get("role") == "assistant" and turn.get("action") == "order" for turn in history):
            return self._conversation_response("reply", "I cannot undo or cancel an order myself; only you can. If the order went through with a notice, press “Undo this order” under it, which works until the order cutoff. If it is waiting for your sign-off, reject it in Sign-off. A committed order without a notice stays as it is.", rules)
        self._admit_model()
        decision = await self.provider.converse(message, history)
        name, arguments, model = decision.get("name"), decision.get("arguments") or {}, decision.get("model")
        texts = buyer_texts(message, history)

        # Draft confirmation is resolved from structured assistant history, not
        # from a newly model-authored need. Declines and edits never confirm it.
        remembered = latest_draft(history)
        confirming_draft = False
        if is_affirmative(message):
            if not remembered or not isinstance(remembered.get("need"), dict):
                return self._conversation_response("clarification", "There is no earlier draft to place. What product, quantity and delivery date do you need?", model)
            original_message = str(remembered["need"].get("request") or message)
            plan = validate_plan({"need": remembered["need"]}, original_message)
            if not quantity_is_stated(plan["need"]["quantity"], plan["need"]["unit"], texts):
                return self._conversation_response("clarification", "The draft quantity is not present in your own messages. How much do you want?", model)
            name, arguments = "place_order", {"need": plan["need"], "restatement": plan["restatement"]}
            confirming_draft = True
        elif is_negative(message):
            return self._conversation_response("reply", "Understood. No order was placed and the draft was not confirmed.", model)

        # A complete instruction must not depend on the model's mood. If it answered
        # with talk or a search although the message itself states intent, quantity
        # and date (or asks for the usual basket), plan it with the order tool alone.
        if not confirming_draft and name not in {"draft_order", "place_order"} and is_complete_instruction(message):
            self._admit_model()
            try:
                planned = await self.provider.plan(message)
            except ProviderError:
                planned = None
            if planned and planned.get("intent") == "reorder" and planned.get("reorder") is not None:
                name, arguments, model = "place_order", {"reorder": planned["reorder"], "restatement": planned.get("restatement", "")}, planned.get("model") or model
            elif planned and isinstance(planned.get("need"), dict):
                name, arguments, model = "place_order", {"need": planned["need"], "restatement": planned.get("restatement", "")}, planned.get("model") or model

        if name == "reply":
            reply = arguments.get("text")
            if not isinstance(reply, str) or not reply.strip() or len(reply) > 3000:
                return self._conversation_response("reply", "I can show what Aimo sells, check price and stock, and place orders inside your signed limits. What would you like to do?", model)
            return self._conversation_response("reply", reply.strip(), model)
        if name == "show_limits":
            return await self._limits_answer(customer_id, model)
        if name == "show_usual_order":
            return await self._usual_answer(customer_id, model)
        if name == "search_catalog":
            properties = stated_properties(arguments.get("properties", []), texts)
            return await self._catalog_answer(customer_id, arguments.get("text", "products"), properties, model, cheapest=asks_for_cheapest(message))
        if name == "ask":
            question = arguments.get("question")
            if not isinstance(question, str) or not question.strip():
                question = "Which product and quantity do you need?"
            if arguments.get("text"):
                properties = stated_properties(arguments.get("properties", []), texts)
                return await self._catalog_answer(customer_id, arguments["text"], properties, model, question=question.strip()[:1500])
            return self._conversation_response("clarification", question.strip()[:1500], model)
        if name not in {"draft_order", "place_order"}:
            return self._conversation_response("clarification", "Which product, quantity and delivery date do you need?", model)

        try:
            if arguments.get("reorder") is not None and name == "place_order":
                plan = validate_plan({"intent": "reorder", "reorder": arguments["reorder"], "restatement": arguments.get("restatement", "")}, message)
                plan = {**plan, **await self._expand_reorder(customer_id, plan)}
            else:
                raw_need = dict(arguments.get("need") or {})
                selected_id = selected_product_id(message, history)
                if selected_id:
                    raw_need["product_id"] = selected_id
                if not isinstance(raw_need.get("properties"), list):
                    raw_need["properties"] = []
                if not raw_need.get("deadline") or (name == "draft_order" and not deadline_is_stated(str(raw_need.get("deadline")), texts)):
                    # No date from the buyer: propose the next delivery day and ask before ordering.
                    raw_need["deadline"] = next_monday().isoformat()
                    name = "draft_order"
                plan = validate_plan({"need": raw_need, "restatement": arguments.get("restatement", "")}, message)
        except (ProviderError, DoorAPIError) as error:
            # A request the buyer can rephrase is a question back to them, not a failure.
            text = str(error)
            if text.startswith("The model returned"):
                chosen = next((item.get("name") for item in latest_products(history) if item.get("product_id") == selected_product_id(message, history)), None)
                text = (f"How much of {chosen} would you like, and for which delivery date?" if chosen
                        else "I need the product, the quantity and the delivery date to place that. Which of them should I add?")
            return self._conversation_response("clarification", text, model)

        plan = self._buyer_property_plan(plan, message, history)
        need = plan["need"]
        if need.get("product_type") != "usual basket":
            if not has_buying_intent(texts):
                return self._conversation_response("clarification", "Are you asking about the catalogue, or would you like to place an order?", model)
            if not product_is_stated(need, texts, history):
                return self._conversation_response("clarification", "Which product would you like? Please name a product; I did not find a shortlisted option to place in this request.", model)
            if not quantity_is_stated(need["quantity"], need["unit"], texts):
                return self._conversation_response("clarification", f"I cannot use the quantity {need['quantity']:g} {need['unit']} because it does not appear in your messages. What quantity would you like?", model)
            dated_by_buyer = deadline_is_stated(need["deadline"], texts)
            if name == "place_order" and not dated_by_buyer and not confirming_draft:
                name = "draft_order"

        if name == "draft_order":
            return await self._draft_answer(customer_id, plan, model, simulate_limited_stock=simulate_limited_stock)

        result = await self.execute(customer_id, need, message=message, restatement=plan["restatement"], simulate_limited_stock=simulate_limited_stock, idempotency_key=idempotency_key, history=history)
        if not result.get("order"):
            return await self._catalog_answer(customer_id, need.get("product_type", "products"), need.get("properties", []), model, question="I could not find one product that fits that order as it stands. Which of these should I use, or what should I change?")
        outcome = next((item["text"] for item in reversed(result.get("messages", [])) if item.get("kind") == "outcome"), "The order request was completed.")
        fields = {key: result.get(key) for key in ("customer_id", "need", "restatement", "match", "offer", "order", "messages", "idempotency_key")}
        return self._conversation_response("order", outcome, model, **fields)

    async def _held_order(self, customer_id: str, order_id: str) -> dict:
        if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", order_id):
            raise DoorAPIError("Choose a valid order to discuss.")
        order = await self._request("GET", f"/orders/{order_id}", headers=self.read_headers.get(customer_id, {}))
        if order.get("customer_id") != customer_id:
            raise DoorAPIError("This order belongs to a different customer.")
        if order.get("status") != "requires_escalation":
            raise DoorAPIError("Only an order awaiting sign-off needs this interview.")
        if not isinstance(order.get("question_spec"), dict):
            raise DoorAPIError("The door has no question specification for this order. Review its sign-off page.")
        interview_contract(order["question_spec"])
        return order

    async def interview(self, customer_id: str, order_id: str) -> dict:
        order = await self._held_order(customer_id, order_id)
        spec = order["question_spec"]
        answers, scopes = interview_contract(spec)
        values = {item.get("value") or item["id"] for item in answers}
        if order.get("approvable") is False or "approve" not in values:
            question = spec["needed"] + " Would you like to reject this order?"
            return {"stage": "interview", "order_id": order_id, "question": question, "question_spec": spec, "accepted_answers": answers, "scope_options": scopes, "model": None, "messages": [event("question", question)]}
        self._admit_model()
        result = await self.provider.interview(spec)
        return {"stage": "interview", "order_id": order_id, "question": result["question"], "question_spec": spec, "accepted_answers": answers, "scope_options": scopes, "model": result.get("model"), "messages": [event("question", result["question"])]}

    async def answer(self, customer_id: str, order_id: str, message: str, scope: str | None = None) -> dict:
        order = await self._held_order(customer_id, order_id)
        answers, _ = interview_contract(order["question_spec"])
        values = {item.get("value") or item["id"] for item in answers}
        reject_only = order.get("approvable") is False or "approve" not in values
        self._admit_model()
        result = await self.provider.interview(order["question_spec"], message, scope)
        if result.get("answer") is None or result.get("answer") not in values:
            fallback = "Approval is not available for this order. You can reject it or replace it with a new request." if reject_only else "Please clarify your choice."
            clarification = fallback if reject_only else (result.get("clarification") or fallback)
            return {"stage": "answer", "order_id": order_id, "clarification": clarification, "model": result.get("model"), "messages": [event("question", clarification)]}
        prepared = await self._request("POST", f"/orders/{order_id}/signoff", json={"action": "prepare_answer", "customer_id": customer_id, "answer": result["answer"], "scope": result["scope"]})
        pending = prepared.get("pending_answer") or prepared.get("order", {}).get("pending_answer")
        if not isinstance(pending, dict) or not pending.get("confirmation_summary"):
            raise DoorAPIError("The door did not return a confirmation summary. The order remains held.")
        if len(self.pending_confirmations) >= 4096:
            self.pending_confirmations.pop(next(iter(self.pending_confirmations)))
        self.pending_confirmations[(customer_id, order_id)] = {"confirmation_summary": pending["confirmation_summary"], "question_hash": pending.get("question_hash")}
        return {"stage": "answer", "order_id": order_id, "confirmation_summary": pending["confirmation_summary"], "pending_answer": pending, "model": result.get("model"), "messages": [event("confirmation", pending["confirmation_summary"] + " Please confirm before I submit this answer.")]}

    async def confirm(self, customer_id: str, order_id: str, confirmed: bool, confirmation_summary: str | None = None, question_hash: str | None = None, buyer_session: str | None = None) -> dict:
        if confirmed is not True:
            raise DoorAPIError("Confirm the displayed summary before submitting this answer.")
        held = await self._held_order(customer_id, order_id)
        pending = held.get("pending_answer")
        if not isinstance(pending, dict) or not pending.get("confirmation_summary"):
            raise DoorAPIError("There is no pending answer to confirm. Answer the question first.")
        shown = self.pending_confirmations.get((customer_id, order_id))
        if not shown or shown["confirmation_summary"] != pending["confirmation_summary"] or shown.get("question_hash") != pending.get("question_hash"):
            raise DoorAPIError("The pending answer changed since the summary was shown. Please answer again before confirming.")
        if (confirmation_summary is not None and confirmation_summary != shown["confirmation_summary"]) or (question_hash is not None and question_hash != shown.get("question_hash")):
            raise DoorAPIError("That confirmation does not match the displayed summary. Please review the latest answer.")
        answer, scope = pending.get("answer"), pending.get("scope")
        answers, scopes = interview_contract(held["question_spec"])
        if answer not in {item.get("value") or item["id"] for item in answers} or scope not in scopes:
            raise DoorAPIError("The pending answer no longer matches the door's question. Please answer again.")
        if not buyer_session:
            raise DoorAPIError("The console must forward the human's X-Buyer-Session for this confirmed step.")
        # Forward the human credential for this call only. It is deliberately not
        # stored on this bot, in pending confirmations, or reused for another call.
        order = await self._request("POST", f"/orders/{order_id}/signoff", json={"action": answer, "customer_id": customer_id, "answer": answer, "scope": scope, "confirmed": True, "confirmation_summary": pending["confirmation_summary"], "question_hash": pending.get("question_hash")}, headers={"X-Buyer-Session": buyer_session})
        self.pending_confirmations.pop((customer_id, order_id), None)
        text = "Your confirmed answer was recorded. " + ("The door committed the order and issued a receipt." if order.get("status") == "committed" else "The order was rejected and its reservation released." if order.get("status") == "rejected" else "Review the updated sign-off result.")
        return {"stage": "confirm", "order_id": order_id, "order": order, "confirmation_summary": pending["confirmation_summary"], "messages": [event("outcome", text)]}
