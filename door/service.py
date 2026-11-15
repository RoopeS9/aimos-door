"""Buyer-authorized decisions, signed documents and a simulated order ledger."""
from __future__ import annotations

import copy
import asyncio
import json
import math
import os
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import jwt
from fastapi import HTTPException

from door.security import SigningKeys, digest
from door.storage import Store
from door.autonomy import Autonomy
from door.catalog import normalize_property
from door.mandates import category_matches, demo_scope_limits


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def isotime() -> str:
    return utcnow().isoformat()


def parse_time(value: str | None) -> datetime:
    if not value:
        return utcnow() + timedelta(hours=4)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return result.replace(tzinfo=ZoneInfo("Europe/Helsinki")) if result.tzinfo is None else result


class DoorService(Autonomy):
    def __init__(self, data_dir: str | Path, catalog, valio, matcher, fixture: bool = False):
        self.data_dir = Path(data_dir)
        self.catalog, self.valio, self.matcher, self.fixture = catalog, valio, matcher, fixture
        self.store = Store(self.data_dir / "door.sqlite3")
        self.keys = SigningKeys(self.data_dir)
        self.idle_reset_at = None
        self.seed_demo()

    def seed_demo(self):
        with self.store.transaction() as db:
            for customer in ("hotel", "restaurant"):
                if self.store.get(db, "customer", customer):
                    continue
                payload = {
                    "kind": "mandate", "jti": f"{customer}-v1", "customer_id": customer,
                    "bot_id": "reference-bot", "version": 1, "iat": int(utcnow().timestamp()),
                    "exp": int((utcnow() + timedelta(days=365)).timestamp()),
                    "simulated_buyer_key": True,
                    "limits": {"per_order_cap": 2000 if customer == "hotel" else 400, "monthly_budget": 20000 if customer == "hotel" else 6000,
                               **demo_scope_limits(), "soft_preferences": {},
                               "departure_ceiling": 0.30, "current_freedom": 0.10},
                    "learned_preferences": [],
                }
                token = self.keys.sign_buyer(customer, payload)
                self.store.put(db, "mandate", payload["jti"], {"mandate": token, "payload": payload})
                self.store.put(db, "customer", customer, {"id": customer, "name": "Hotel chain" if customer == "hotel" else "Restaurant", "mandate_id": payload["jti"], "bot_id": "reference-bot", "history_label": "No order history"})

    def reset(self):
        with self.store.transaction() as db:
            seed = self.store.get(db, "history_seed", "restaurant")
            for table in ("orders", "reservations", "records"):
                db.execute(f"DELETE FROM {table}")
            db.execute("DELETE FROM documents WHERE kind NOT IN ('history_seed', 'buyer_session')")
        self.seed_demo()
        if seed:
            with self.store.transaction() as db:
                self._install_history(db, seed)
        return {"status": "reset", **self.customers()}

    def customers(self):
        with self.store.connect() as db:
            customers = self.store.list(db, "customer")
            for customer in customers:
                mandate = self.store.get(db, "mandate", customer["mandate_id"])
                customer["mandate"] = mandate["mandate"]
                customer["mandate_payload"] = mandate["payload"]
                limits = mandate["payload"]["limits"]
                customer["product_scope_label"] = limits.get("allowed_categories_label", "Buyer-signed product categories")
                customer["must_hold_label"] = limits.get("must_hold_label", "Buyer-required properties")
        return {"customers": customers, "idle_reset_at": self.idle_reset_at, "real": ["Product catalog", "Prices and stock from Valio's mock API"] if not self.fixture else [], "simulated": ["Order book and reservations", "Buyer signing keys", "Order history", "Buyer login sessions", "Strong identification and company authorisation"], "fixture": self.fixture}

    def create_buyer_session(self, customer_id: str):
        with self.store.connect() as db:
            if not self.store.get(db, "customer", customer_id):
                raise HTTPException(404, "Customer not found.")
        token = secrets.token_urlsafe(32)
        expires = utcnow() + timedelta(seconds=max(300, int(os.getenv("BUYER_SESSION_TTL_SECONDS", "28800"))))
        session = {
            "id": "buyer-session-" + uuid.uuid4().hex,
            "customer_id": customer_id,
            "created_at": isotime(),
            "expires_at": expires.isoformat(),
            "simulated": True,
        }
        # Only a one-way lookup key is stored. Records and API responses use the
        # public session id, never the bearer token or its storage hash.
        with self.store.transaction() as db:
            self.store.put(db, "buyer_session", digest(token), session)
        return {
            "token": token, "session_id": session["id"], "customer_id": customer_id,
            "expires_at": session["expires_at"], "simulated": True,
            "label": "Simulated buyer login; this is not production authentication.",
        }

    def identify(self, customer_id: str, method: str, buyer_token: str | None = None):
        """Issue a demo-only, session-bound proof; never contact an identity provider."""
        session = self.buyer_session(buyer_token, customer_id)
        if method != "bank":
            raise HTTPException(422, "Only simulated bank identification is available.")
        with self.store.connect() as db:
            customer = self.store.get(db, "customer", customer_id)
        if not customer:
            raise HTTPException(404, "Customer not found.")
        now = utcnow()
        proof = secrets.token_urlsafe(32)
        person = {"name": "Fictional Taylor Example"}
        identification = {
            "id": "identification-" + uuid.uuid4().hex,
            "customer_id": customer_id,
            "buyer_session_id": session["id"],
            "buyer_session_key": digest(buyer_token),
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
            "used_at": None,
            "simulated": True,
            "method": "Simulated bank identification",
            "person": person,
            "authority": f"May sign for {customer['name']} (simulated company authorisation)",
        }
        # The bearer proof is stored only by its one-way digest. It is neither a
        # mandate nor a real identity credential and is deleted by demo reset.
        with self.store.transaction() as db:
            self.store.put(db, "identification_proof", digest(proof), identification)
        return {
            "proof": proof, "expires_at": identification["expires_at"],
            "simulated": True, "method": identification["method"],
            "person": person, "authority": identification["authority"],
        }

    def buyer_session(self, token: str | None, customer_id: str | None = None):
        if not token:
            raise HTTPException(401, "A simulated buyer session is required in X-Buyer-Session.")
        with self.store.connect() as db:
            session = self.store.get(db, "buyer_session", digest(token))
        if not session or parse_time(session.get("expires_at")) <= utcnow():
            raise HTTPException(401, "The buyer session is invalid or expired.")
        if customer_id is not None and session["customer_id"] != customer_id:
            raise HTTPException(403, "This buyer session belongs to a different customer.")
        return session

    def authorize_read(self, customer_id: str, *, buyer_token=None, authorization=None, bot_id=None, expected_bot_id=None):
        if buyer_token:
            return self.buyer_session(buyer_token, customer_id)
        if not authorization or not bot_id:
            raise HTTPException(401, "Use X-Buyer-Session, or Authorization: Bearer <mandate> with X-Bot-Id.")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(401, "The mandate must use the Authorization Bearer scheme.")
        mandate = self.mandate(token, bot_id)
        if mandate["customer_id"] != customer_id or (expected_bot_id is not None and bot_id != expected_bot_id):
            raise HTTPException(403, "This mandate and bot identity do not authorize this customer's data.")
        return mandate

    def mandate(self, token: str | None, bot_id: str | None):
        if not token or not bot_id:
            raise HTTPException(401, "A valid buyer mandate and bot identity are required.")
        try:
            payload = self.keys.verify_buyer(token, bot_id)
            with self.store.connect() as db:
                registered = self.store.get(db, "mandate", payload["jti"])
                customer = self.store.get(db, "customer", payload["customer_id"])
            if registered is None or registered["mandate"] != token or not customer or customer["mandate_id"] != payload["jti"]:
                raise jwt.InvalidTokenError("Mandate is not registered")
            return payload
        except (jwt.InvalidTokenError, ValueError, TypeError):
            raise HTTPException(403, "The buyer mandate is invalid, expired, or names a different bot.") from None

    def register_mandate(self, token: str, buyer_token: str | None = None):
        try:
            payload = self.keys.verify_buyer(token)
        except (jwt.InvalidTokenError, ValueError, TypeError):
            raise HTTPException(403, "Only a valid buyer-signed mandate can be registered.") from None
        session = self.buyer_session(buyer_token, payload["customer_id"])
        limits = payload["limits"]
        if not isinstance(limits, dict) or any(not isinstance(limits.get(k), (int, float)) or not math.isfinite(limits[k]) or limits[k] < 0 for k in ("per_order_cap", "monthly_budget")):
            raise HTTPException(422, "The signed mandate must contain nonnegative finite spend limits.")
        with self.store.transaction() as db:
            previous = self.store.get(db, "mandate", payload["jti"])
            if previous and previous["mandate"] != token:
                raise HTTPException(409, "A mandate identifier cannot be changed in place; sign a new version.")
            self.store.put(db, "mandate", payload["jti"], {"mandate": token, "payload": payload})
            customer = self.store.get(db, "customer", payload["customer_id"])
            if customer:
                current = self.store.get(db, "mandate", customer["mandate_id"])
                if current and payload["jti"] != current["payload"]["jti"] and payload.get("version", 0) <= current["payload"].get("version", 0):
                    raise HTTPException(409, "A new mandate identifier must have a strictly newer signed version.")
                customer["mandate_id"] = payload["jti"]
                self.store.put(db, "customer", customer["id"], customer)
                for row in db.execute("SELECT id,payload FROM documents WHERE kind='freedom' AND id LIKE ?", (customer["id"] + ":%",)).fetchall():
                    state = json.loads(row["payload"])
                    ceiling = float(limits.get("departure_ceiling", .3))
                    if float(state.get("current_freedom", 0)) > ceiling:
                        state.update(current_freedom=ceiling, reason="The buyer signed a lower departure ceiling, so current freedom was reduced to stay within it.")
                        self.store.put(db, "freedom", row["id"], state)
            record = None
            if previous is None:
                record = self._decision_record(
                    db, customer_id=payload["customer_id"], action="mandate_version_registered",
                    subject_id=payload["jti"],
                    description=f"The buyer registered signed mandate version {payload.get('version', 1)} ({payload['jti']}); the door did not widen it.",
                    approved_by=f"simulated buyer session {session['id']}",
                    details={"version": payload.get("version", 1), "mandate_id": payload["jti"]},
                )
        result = {"id": payload["jti"], "mandate": token, "payload": payload}
        if record:
            result.update(record_id=record["id"], record_hash=record["hash"])
        return result

    def reservations(self, db=None):
        if db is None:
            with self.store.transaction() as connection:
                return self.reservations(connection)
        # Compare timestamps in Python: external API offsets need not be identical.
        for row in db.execute("SELECT order_id,product_id,delivery_date,cutoff FROM reservations WHERE active=1").fetchall():
            if parse_time(row["cutoff"]) <= utcnow():
                db.execute("UPDATE reservations SET active=0 WHERE order_id=? AND product_id=? AND delivery_date=?", (row["order_id"], row["product_id"], row["delivery_date"]))
        return {row["product_id"]: row["packs"] for row in db.execute("SELECT product_id,SUM(packs) packs FROM reservations WHERE active=1 GROUP BY product_id")}

    def reservation_pools(self, db=None, exclude_order=None):
        if db is None:
            with self.store.transaction() as connection:
                return self.reservation_pools(connection, exclude_order)
        self.reservations(db)
        rows = db.execute("SELECT product_id,delivery_date,SUM(packs) packs FROM reservations WHERE active=1 AND order_id != ? GROUP BY product_id,delivery_date", (exclude_order or "",))
        return {f"{row['product_id']}|{row['delivery_date']}": row["packs"] for row in rows}

    def _replace_reservations(self, db, order, payload):
        """Under BEGIN IMMEDIATE, replace the reservation without an oversell window."""
        pools = self.reservation_pools(db, order["id"])
        db.execute("DELETE FROM reservations WHERE order_id=?", (order["id"],))
        shortage = []
        for line in payload.get("lines", []):
            pid = str(line.get("id", line.get("product_id")))
            day = line.get("inventory_date") or line["delivery_date"]
            pool = f"{pid}|{day}"
            packs = float(line["packs"])
            raw_available = float(line.get("raw_available", float(line.get("available", 0)) + float(line.get("reserved_at_match", 0))))
            remaining = max(0, raw_available - pools.get(pool, 0))
            reservable = min(packs, remaining)
            if packs > remaining:
                shortage.append(line.get("name", pid))
            if reservable > 0 and parse_time(line.get("cutoff")) > utcnow():
                db.execute("INSERT INTO reservations(order_id,product_id,delivery_date,packs,cutoff) VALUES(?,?,?,?,?) ON CONFLICT(order_id,product_id,delivery_date) DO UPDATE SET packs=packs+excluded.packs", (order["id"], pid, day, reservable, line["cutoff"]))
                pools[pool] = pools.get(pool, 0) + reservable
        order["reserved"] = [dict(row) for row in db.execute("SELECT product_id,delivery_date,packs,cutoff FROM reservations WHERE order_id=? AND active=1", (order["id"],))]
        return shortage

    def _reservation_covers_order(self, db, order):
        self.reservations(db)
        held = {f"{row['product_id']}|{row['delivery_date']}": row["packs"] for row in db.execute("SELECT product_id,delivery_date,SUM(packs) packs FROM reservations WHERE order_id=? AND active=1 GROUP BY product_id,delivery_date", (order["id"],))}
        needed = {}
        for line in order["offer_payload"].get("lines", []):
            pool = f"{line['id']}|{line.get('inventory_date') or line['delivery_date']}"
            needed[pool] = needed.get(pool, 0) + float(line["packs"])
        return all(held.get(pool, 0) >= packs for pool, packs in needed.items())

    async def catalog_search(self, *, text="", properties=None, category=None, storage_class=None, limit=50, customer_id=None, delivery_date=None):
        """Browse publicly; add quotes only when a concrete customer/day is supplied."""
        if delivery_date is None:
            return {"products": self.catalog.search(text=text, category=category, properties=properties, storage_class=storage_class, limit=limit)}
        try:
            requested = date.fromisoformat(delivery_date).isoformat()
        except (ValueError, TypeError):
            raise HTTPException(422, "delivery_date must be an ISO calendar date.") from None
        with self.store.connect() as db:
            customer = self.store.get(db, "customer", str(customer_id or ""))
        if not customer:
            raise HTTPException(422, "A valid demo customer_id is required for prices and availability.")
        from door.matching import cutoff_passed, pack_content

        products = self.catalog.search_relevant(text, properties=properties, limit=min(int(limit), 6))
        if category:
            products = [product for product in products if product.get("category") == str(category)]
        if storage_class is not None:
            products = [product for product in products if str(product.get("storage_class")) == str(storage_class)]
        ids = [product["id"] for product in products]
        prices, stock = await asyncio.gather(self.valio.prices(customer_id, requested, ids), self.valio.availability(customer_id, requested, ids))
        rows = []
        for product in products:
            price, inventory = prices.get(product["id"], {}), stock.get(product["id"], {})
            pack_size = None
            if not price.get("error") and price.get("comparison_unit") and price.get("comparison_price"):
                try:
                    pack_size, _ = pack_content(product, price, str(price["comparison_unit"]).upper())
                except (ValueError, TypeError):
                    pack_size = None
            comparison_unit = str(price.get("comparison_unit") or "").upper()
            multiplier = float(product.get("sales_unit_multiplier") or 1)
            if pack_size and comparison_unit:
                if multiplier > 1 and pack_size >= multiplier:
                    pack = f"{multiplier:g} × {pack_size / multiplier:g} {comparison_unit}"
                else:
                    pack = f"{pack_size:g} {comparison_unit}"
            else:
                pack = str(product.get("sales_unit") or "sales pack")
            unit_price = price.get("unit_price") if not price.get("error") else None
            comparison_price = price.get("comparison_price") if not price.get("error") else None
            price_text = f"€{float(comparison_price):.2f} / {comparison_unit}" if comparison_price is not None and comparison_unit else None
            available = inventory.get("available")
            actual_date = inventory.get("delivery_date")
            terminal_unknown = inventory.get("subject_to_supplier") and (available is None or float(available) >= 999999)
            if not inventory or not inventory.get("deliverable") or inventory.get("sales_block"):
                availability, available_text = "unavailable", f"Unavailable for {requested}."
            elif actual_date and actual_date > requested:
                availability, available_text = "unavailable", f"Not available for {requested}; upstream offers {actual_date}."
            elif cutoff_passed(inventory.get("cutoff")):
                availability, available_text = "unavailable", "The order cutoff for this delivery has passed."
            elif terminal_unknown:
                availability, available_text = "unknown", "Supplier availability must be confirmed."
            else:
                try:
                    amount = float(available)
                except (TypeError, ValueError):
                    amount = 0
                if amount <= 0:
                    availability, available_text = "unavailable", f"No stock available for {requested}."
                elif amount <= 10 and amount < 9999999999:
                    availability, available_text = "limited", f"Limited stock: {amount:g} sales packs indicated for {requested}."
                else:
                    availability, available_text = "in_stock", f"Available for {requested}."
            rows.append({
                "product_id": product["id"], "name": product.get("name") or product["id"],
                "pack": pack,
                "price_eur": round(float(unit_price), 4) if isinstance(unit_price, (int, float)) else None,
                "unit_price": price_text, "availability": availability, "available_text": available_text,
                "properties": sorted(prop for prop, source in product.get("properties", {}).items() if source == "manufacturer"),
            })
        return {"products": rows, "delivery_date": requested}

    async def match(self, body: dict):
        mandate = self.mandate(body.get("mandate"), body.get("bot_id"))
        need = copy.deepcopy(body.get("need") or {})
        selected_product = self.catalog.get(str(need.get("product_id"))) if need.get("product_id") else None
        if selected_product:
            need["product_type"] = selected_product["product_type"]
        basket = need.get("product_type") == "usual basket"
        if basket:
            need.setdefault("quantity", 1)
            need.setdefault("unit", "PACK")
        try:
            quantity = float(need["quantity"])
            if not math.isfinite(quantity) or quantity <= 0:
                raise ValueError()
            date.fromisoformat(need["deadline"])
        except (KeyError, ValueError, TypeError):
            raise HTTPException(422, "A positive quantity and an ISO delivery deadline are required.") from None
        limits = mandate["limits"]
        must_hold_categories = limits.get("must_hold_categories")
        need["properties"] = sorted({normalize_property(str(prop)) for prop in (
            list(need.get("properties", []))
            # Legacy mandates without a category scope keep their global must-hold.
            + (list(limits.get("must_hold", [])) if must_hold_categories is None else [])
            + ([] if basket else list(limits.get("must_hold_by_type", {}).get(need.get("product_type"), [])))
        )})
        # A request can add preferences but cannot replace a buyer-signed one.
        signed_preferences = mandate["limits"].get("soft_preferences", {})
        requested_preferences = need.get("preferences", {})
        need["preferences"] = {**requested_preferences, **signed_preferences}
        for level in ("required", "soft"):
            if level in signed_preferences or level in requested_preferences:
                need["preferences"][level] = {**requested_preferences.get(level, {}), **signed_preferences.get(level, {})}
        with self.store.connect() as db:
            confirmations = {}
            for entry in self.store.list(db, "confirmation"):
                if entry["customer_id"] == mandate["customer_id"]:
                    confirmations.setdefault(entry["product_id"], []).append(entry["property"])
        pools = self.reservation_pools()
        if basket:
            result = await self._match_basket(mandate, need, pools, confirmations)
        else:
            scoped = {} if must_hold_categories is None else {
                "must_hold": list(limits.get("must_hold", [])),
                "must_hold_categories": must_hold_categories,
            }
            result = await self.matcher.match(need, mandate["customer_id"], reservations=pools, confirmations=confirmations, **scoped)
        with self.store.transaction() as db:
            for option in result["options"]:
                option["filters_applied"] = result.get("filters_applied", [])
                option["option_id"] = digest({"customer_id": mandate["customer_id"], "need": need, "option": option})
                self.store.put(db, "option", option["option_id"], {"customer_id": mandate["customer_id"], "need": need, "option": option, "created_at": isotime(), "options_considered": copy.deepcopy(result["options"])})
        result["need"] = need
        return result

    @staticmethod
    def _category_must_hold(limits, line):
        prefixes = limits.get("must_hold_categories")
        if prefixes is None or category_matches(line.get("category"), prefixes):
            return list(limits.get("must_hold", []))
        return []

    async def _match_basket(self, mandate, need, pools, confirmations):
        await self.ensure_history(mandate["customer_id"])
        usual = self.usual(mandate["customer_id"])
        if need.get("anchor_id") and need["anchor_id"] != usual.get("anchor_id"):
            raise HTTPException(409, "The confirmed usual-order anchor changed. Fetch the usual basket again before ordering.")
        requested = need.get("lines")
        if not isinstance(requested, list) or not requested or len(requested) > 30:
            raise HTTPException(422, "A basket must contain between one and thirty product needs.")
        lines, filters, selected, canonical_needs = [], ["Product type: usual basket, based on the human-confirmed anchor", f"Delivery deadline: {need['deadline']}"], [], []
        for row in requested:
            pid = str(row.get("product_id") or row.get("id") or "")
            product = self.catalog.get(pid)
            if not product:
                raise HTTPException(422, "Every basket line must name a catalog product.")
            try:
                quantity = float(row["quantity"])
                if not math.isfinite(quantity) or quantity <= 0:
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise HTTPException(422, "Each basket line needs a positive finite quantity.") from None
            properties = {normalize_property(str(prop)) for prop in (
                list(row.get("required_properties", row.get("properties", [])))
                + list(need["properties"])
                + self._category_must_hold(mandate["limits"], product)
                + list(mandate["limits"].get("must_hold_by_type", {}).get(product["product_type"], []))
            )}
            line_need = {"product_id": pid, "product_ids": [pid], "product_type": product["product_type"], "quantity": quantity, "unit": row.get("unit", "PACK"), "deadline": need["deadline"], "properties": sorted(properties), "preferences": need.get("preferences", {})}
            result = await self.matcher.match(line_need, mandate["customer_id"], reservations=pools, confirmations=confirmations)
            if not result["options"]:
                raise HTTPException(409, f"The usual product {product['name']} has no usable current quote; review an alternative with the buyer.")
            option = result["options"][0]
            for line in option["lines"]:
                line["required_properties"] = sorted(properties)
            lines.extend(option["lines"])
            filters.extend(result["filters_applied"])
            selected.append(option)
            canonical_needs.append(line_need)
        need["lines"] = canonical_needs
        option = {"label": "best", "lines": lines, "total": round(sum(line["total"] for line in lines), 2), "delivery_date": max(line["delivery_date"] for line in lines), "on_time": all(option["on_time"] for option in selected), "overage": 0, "within_preferences": all(option.get("within_preferences", True) for option in selected), "soft_trade": any(option.get("soft_trade") for option in selected), "partial": any(option.get("partial") for option in selected), "description": "The confirmed usual basket, repriced with the requested changes in whole sales packs.", "filters_applied": list(dict.fromkeys(filters))}
        return {"options": [option], "filters_applied": option["filters_applied"], "need": need, "data_source": "synthetic fixture" if self.fixture else "Valio mock API"}

    def _required_properties(self, mandate, payload, line):
        limits = (mandate or {}).get("limits", {})
        need = payload.get("need", {})
        required = {normalize_property(str(prop)) for prop in (
            self._category_must_hold(limits, line)
            + list(limits.get("must_hold_by_type", {}).get(line.get("product_type"), []))
            + list(need.get("properties", []))
        )}
        if need.get("product_type") == "usual basket":
            for row in need.get("lines", []):
                if str(row.get("product_id") or row.get("id")) == str(line["id"]):
                    required.update(row.get("required_properties", row.get("properties", [])))
        return required

    def offer(self, body: dict):
        mandate = self.mandate(body.get("mandate"), body.get("bot_id"))
        supplied = body.get("option") or {}
        option_id = body.get("option_id") or (supplied.get("option_id") if isinstance(supplied, dict) else supplied)
        with self.store.transaction() as db:
            option_doc = self.store.get(db, "option", option_id or "")
            if not option_doc or option_doc["customer_id"] != mandate["customer_id"]:
                raise HTTPException(400, "Choose an option returned by this buyer's need matching request.")
            if parse_time(option_doc["created_at"]) + timedelta(minutes=5) < utcnow():
                raise HTTPException(409, "This option is old. Match the need again for current prices and stock.")
            option = copy.deepcopy(option_doc["option"])
            for line in option.get("lines", []):
                if not line.get("cutoff"):
                    line["cutoff"] = (utcnow() + timedelta(hours=4)).isoformat()
                    line["cutoff_source"] = "Simulated four-hour deadline: upstream supplied no cutoff"
            payload = {**option, "kind": "offer", "option_kind": option.get("kind", "single_product"), "jti": str(uuid.uuid4()), "customer_id": mandate["customer_id"],
                       "bot_id": body["bot_id"], "need": option_doc["need"], "currency": "EUR", "prices_include_vat": False,
                       "iat": int(utcnow().timestamp()), "exp": int((utcnow() + timedelta(minutes=5)).timestamp()),
                       "expires_at": (utcnow() + timedelta(minutes=5)).isoformat(), "reserves_stock": False,
                       "options_considered": option_doc.get("options_considered", [])}
            token = self.keys.sign_door(payload)
            self.store.put(db, "offer", payload["jti"], {"offer": token, "payload": payload})
        return {"offer": token, "payload": payload}

    def _offer_check(self, token: str | None):
        try:
            payload = self.keys.verify_door(token or "", "offer")
            with self.store.connect() as db:
                registered = self.store.get(db, "offer", payload["jti"])
            if not registered or registered["offer"] != token:
                raise jwt.InvalidTokenError("Unregistered offer")
            return payload, True
        except (jwt.InvalidTokenError, ValueError, TypeError):
            # Only retrieve the original signed server copy. Never use tampered prices.
            try:
                hint = jwt.decode(token or "", options={"verify_signature": False})
                with self.store.connect() as db:
                    original = self.store.get(db, "offer", str(hint.get("jti", "")))
                return (original["payload"] if original else {}), False
            except (jwt.InvalidTokenError, ValueError, TypeError):
                return {}, False

    @staticmethod
    def _matches(value, requested):
        return value in requested if isinstance(requested, list) else value == requested

    def _preference_failures(self, mandate, payload):
        limits = (mandate or {}).get("limits", {})
        need = payload.get("need", {})
        required = {**need.get("preferences", {}).get("required", {}), **limits.get("soft_preferences", {}).get("required", {})}
        entries = (mandate or {}).get("learned_preferences", [])
        failures = []
        for line in payload.get("lines", []):
            kind = line.get("product_type")
            for key, wanted in required.items():
                value = line.get(key)
                matching = [entry for entry in entries if entry.get("product_type") == kind == need.get("product_type") and entry.get("preference") == key and entry.get("accepted_value") == value]
                always_ask = any(entry.get("scope") == "always_ask" or entry.get("answer") == "reject" for entry in matching)
                accepted = any(entry.get("scope") == "always" and entry.get("answer") == "approve" for entry in matching)
                if always_ask or (not self._matches(value, wanted) and not accepted):
                    failures.append({"product_id": str(line.get("id")), "product_type": kind, "name": key, "requested": wanted, "offered": value, "always_ask": always_ask})
        # An inconsistent type cannot inherit a permission from a different type.
        if need.get("product_type") != "usual basket" and any(line.get("product_type") != need.get("product_type") for line in payload.get("lines", [])):
            failures.append({"name": "product_type", "requested": need.get("product_type"), "offered": "a different product type", "product_type": need.get("product_type")})
        return failures

    def _soft_trade(self, mandate, payload):
        requested = payload.get("need", {}).get("preferences", {})
        signed = (mandate or {}).get("limits", {}).get("soft_preferences", {})
        soft = {**{key: value for key, value in requested.items() if key in ("brand", "pack_size")}, **requested.get("soft", {}), **{key: value for key, value in signed.items() if key in ("brand", "pack_size")}, **signed.get("soft", {})}
        return bool(payload.get("partial") or payload.get("soft_trade") or any(not self._matches(line.get(key), value) for line in payload.get("lines", []) for key, value in soft.items()))

    @staticmethod
    def _no_later_delivery_message(payload) -> str:
        deadline = payload.get("need", {}).get("deadline", "the requested delivery date")
        return f"This order cannot be delivered on the requested delivery date {deadline} because the order cutoff has passed, and no later delivery date is known. It can only be rejected or replaced by a new request."

    @staticmethod
    def _reject_only_delivery(payload, checks) -> bool:
        """A spent requested-date cutoff with no later date is a terminal hold."""
        return bool(
            not checks[4]["passed"]
            and not payload.get("earliest_orderable_date")
            and (
                payload.get("requested_cutoff_passed")
                or any(line.get("cutoff") and parse_time(line["cutoff"]) <= utcnow() for line in payload.get("lines", []))
            )
        )

    def _apply_question(self, order, mandate, payload, checks, *, reject_only=None):
        if reject_only is None:
            reject_only = order.get("approvable") is False or self._reject_only_delivery(payload, checks)
        order["question_spec"] = self._question(mandate, payload, checks, reject_only=reject_only)
        if reject_only:
            order["approvable"] = False
        else:
            order.pop("approvable", None)

    def _question(self, mandate, payload, checks, *, reject_only=None):
        failures = self._preference_failures(mandate, payload)
        if reject_only is None:
            reject_only = self._reject_only_delivery(payload, checks)
        if reject_only:
            kind = "delivery"
            needed = self._no_later_delivery_message(payload)
        elif not checks[3]["passed"]:
            kind = "property_confirmation"
            needed = "The buyer must explicitly confirm each missing property for the named product, or reject it."
        elif failures:
            kind = "preference"
            needed = "The buyer must decide whether to accept the offered preference change for " + str(payload.get("need", {}).get("product_type", "this product type")) + "."
        elif not checks[4]["passed"]:
            kind = "delivery"
            needed = "The buyer must decide whether the displayed delivery date and available stock meet the kitchen's needs."
        elif not checks[8]["passed"]:
            kind = "departure"
            needed = checks[8]["reason"] + " The buyer must sign off on this order."
        else:
            return None
        missing = []
        if kind == "property_confirmation":
            for line in payload.get("lines", []):
                product = self.catalog.get(str(line["id"])) or {}
                for prop in self._required_properties(mandate, payload, line):
                    if product.get("properties", {}).get(prop) != "manufacturer" or prop in product.get("contradicted_properties", {}):
                        missing.append({"product_id": str(line["id"]), "product_name": line.get("name"), "property": prop, "contradicted": prop in product.get("contradicted_properties", {})})
        reject_answer = {"value": "reject", "label": "Reject this order", "cost": {"price": 0, "date": "No delivery"}}
        answers = [reject_answer] if reject_only else [
            {"value": "approve", "label": "Accept the displayed order", "cost": {"price": payload.get("total", 0), "date": payload.get("delivery_date")}},
            reject_answer,
        ]
        spec = {"kind": kind, "needed": needed, "product_type": payload.get("need", {}).get("product_type"), "preferences": failures, "missing_properties": missing,
                "answers": answers,
                "scope_options": ["just_this_once", "always", "always_ask"],
                "scope_note": "Standing preferences apply only to this product type and the displayed preference values. Property confirmations apply only to the exact product and property."}
        spec["hash"] = digest(spec)
        return spec

    def _monthly_committed_spend(self, db, customer_id: str, exclude_order_id=None) -> float:
        month = utcnow().strftime("%Y-%m")
        spent = 0.0
        for row in db.execute("SELECT payload FROM orders WHERE customer_id=? AND id != ?", (customer_id, exclude_order_id or "")):
            order = json.loads(row[0])
            if order.get("status") == "committed" and order.get("created_at", "").startswith(month):
                spent += float(order.get("total", 0))
        return round(spent, 2)

    def _delivery_check(self, payload: dict, reservation_shortage=None) -> tuple[bool, str]:
        """Explain stock shortage before any concrete later delivery date."""
        lines = payload.get("lines", [])
        deadline = payload.get("need", {}).get("deadline", "")[:10]
        failures = []
        for line in lines:
            day = line.get("delivery_date", "")[:10]
            if (not day or (deadline and day > deadline) or line.get("on_time") is False
                    or line.get("stock_shortfall", 0) > 0
                    or (line.get("cutoff") and parse_time(line["cutoff"]) <= utcnow())):
                failures.append(line)
        cutoff_driven = bool(payload.get("requested_cutoff_passed")) or any(
            line.get("cutoff") and parse_time(line["cutoff"]) <= utcnow() for line in failures
        )
        if cutoff_driven:
            earliest = payload.get("earliest_orderable_date")
            reason = "The order cutoff for the requested delivery date has passed." if not deadline else f"The order cutoff for the requested delivery date {deadline} has passed."
            if earliest:
                reason += f" The earliest date that can still be ordered is {earliest}."
            else:
                reason += " No confirmed later delivery date is currently available."
            return False, reason
        short_at_source = any(line.get("stock_shortfall", 0) > 0 for line in lines) or payload.get("stock_shortfall", 0) > 0
        # Split, substitute and replenishment options are only constructed after
        # the requested delivery window could not be filled from current stock.
        replenished = payload.get("option_kind", payload.get("kind")) in {"split_delivery", "all_later", "substitute_mix", "shortage_unresolved"}
        is_short = bool(reservation_shortage) or short_at_source or (replenished and bool(failures))
        if not failures and not reservation_shortage:
            return True, "Every line meets the requested delivery deadline and its order cutoff is open."
        reasons = []
        if is_short:
            reasons.append("Stock is short for the requested quantity by the requested deadline")
        seen = set()
        for line in failures:
            name = line.get("name", line.get("id", "Unknown product"))
            day = line.get("delivery_date", "")[:10]
            if deadline and day > deadline:
                reason = f"a later delivery on {day} for {name} cannot meet the requested deadline {deadline}"
            elif not day:
                reason = f"no delivery date is confirmed for {name}"
            elif line.get("cutoff") and parse_time(line["cutoff"]) <= utcnow():
                reason = f"the order cutoff has passed for {name}"
            elif line.get("stock_shortfall", 0) > 0:
                continue
            else:
                reason = f"the available delivery for {name} cannot meet the requested deadline {deadline}"
            if reason not in seen:
                reasons.append(reason)
                seen.add(reason)
        if not reasons:
            reasons.append("the requested quantity is not fully available for the displayed delivery")
        reason = "; ".join(reasons)
        return False, reason[:1].upper() + reason[1:] + "."

    def _checks(self, db, mandate: dict | None, payload: dict, valid_offer: bool, new: bool = True, exclude_order_id=None):
        limits = (mandate or {}).get("limits", {})
        lines = payload.get("lines", [])
        total = float(payload.get("total", 0))
        customer = (mandate or {}).get("customer_id", "unknown")
        scope_fail = [line.get("name", line.get("id", "Unknown product")) for line in lines if not category_matches(line.get("category"), limits.get("allowed_categories", []))]
        property_fail = []
        for line in lines:
            product = self.catalog.get(str(line.get("id", line.get("product_id", "")))) or {}
            properties = product.get("properties", {})
            for prop in self._required_properties(mandate, payload, line):
                confirmation = self.store.get(db, "confirmation", f"{customer}:{product.get('id')}:{prop}")
                contrary = prop in product.get("contradicted_properties", {})
                if contrary:
                    property_fail.append(f"The manufacturer states contrary information for {prop} on {line.get('name', 'this product')}; buyer confirmation cannot override it")
                elif properties.get(prop) != "manufacturer" and not confirmation:
                    property_fail.append(f"{line.get('name', 'This product')} has no manufacturer statement or buyer confirmation for {prop}")
        delivery_pass, delivery_reason = self._delivery_check(payload)
        preferences_pass = not self._preference_failures(mandate, payload)
        spent = self._monthly_committed_spend(db, customer, exclude_order_id)
        bound = bool(mandate and payload.get("customer_id") == customer and payload.get("bot_id") == mandate.get("bot_id"))
        departure = self._departure(db, mandate, payload)
        checks = [
            ("Buyer authority", bool(mandate), "The buyer's signature, expiry and named bot are valid." if mandate else "The buyer mandate is invalid, expired or names another bot."),
            ("Signed offer", valid_offer and bound, "The door's offer signature, buyer, bot and expiry are valid." if valid_offer and bound else "The offer is invalid, expired, or belongs to a different buyer or bot."),
            ("Product scope", bool(lines) and not scope_fail, "Every product is in the buyer's allowed category families." if not scope_fail else "These products are outside the buyer's allowed categories: " + ", ".join(scope_fail) + "."),
            ("Must-hold properties", bool(lines) and not property_fail, "Every must-hold property is stated by its manufacturer or explicitly confirmed by this buyer for this product." if not property_fail else "; ".join(property_fail) + "."),
            ("Delivery deadline", bool(lines) and delivery_pass, delivery_reason),
            ("Per-order cap", bool(mandate) and total <= limits.get("per_order_cap", -1), f"The order costs EUR {total:.2f}; the signed per-order cap is EUR {limits.get('per_order_cap', 0):.2f}."),
            ("Monthly budget", bool(mandate) and total + spent <= limits.get("monthly_budget", -1), f"This order brings the monthly spend to EUR {total + spent:.2f}; the signed budget is EUR {limits.get('monthly_budget', 0):.2f}."),
            ("Substitution preferences", preferences_pass, "Any substitutes stay inside the stated preferences." if preferences_pass else "The proposed substitute falls outside the buyer's stated preferences."),
            ("Departure from usual", departure["passed"], departure["reason"]),
            ("New request", new, "This idempotency key has not been used before." if new else "This is a retry; the first order and receipt are returned."),
        ]
        return [{"id": i + 1, "name": name, "passed": passed, "reason": reason} for i, (name, passed, reason) in enumerate(checks)]

    def _record(self, db, order: dict, approved_by: str, extra: list[str] | None = None):
        previous = db.execute("SELECT payload FROM records WHERE customer_id=? ORDER BY seq DESC LIMIT 1", (order["customer_id"],)).fetchone()
        payload = order.get("offer_payload", {})
        need = payload.get("need", order.get("need", {}))
        filters = payload.get("filters_applied", [])
        request = need.get("request") or f"The buyer requested {need.get('quantity', '?')} {need.get('unit', '')} of {need.get('product_type', 'products')} by {need.get('deadline', 'the stated deadline')}."
        record = {"id": str(uuid.uuid4()), "customer_id": order["customer_id"], "order_id": order["id"], "created_at": isotime(),
                  "request": request, "filters_applied": filters, "options_considered": payload.get("options_considered", []),
                  "checks": order["checks"], "outcome": order["status"], "approved_by": approved_by,
                  "previous_hash": json.loads(previous[0])["hash"] if previous else None,
                  "sentences": [request, "The door applied these filters: " + ", ".join(map(str, filters)) + ".",
                                f"The door considered {len(payload.get('options_considered', []))} matching options.",
                                *[check["reason"] for check in order["checks"]],
                                f"The outcome is {order['status']}. Approval came from {approved_by}.", *(extra or [])],
                  "buyer_copy": True}
        record["hash"] = digest(record)
        db.execute("INSERT INTO records(id,customer_id,payload) VALUES(?,?,?)", (record["id"], record["customer_id"], json.dumps(record)))
        order["record_id"], order["record_hash"] = record["id"], record["hash"]
        if order["status"] == "committed":
            receipt = {"kind": "receipt", "jti": str(uuid.uuid4()), "order_id": order["id"], "customer_id": order["customer_id"],
                       "status": "committed", "tier": order["tier"], "total": order["total"], "currency": "EUR",
                       "lines": payload.get("lines", []), "record_id": record["id"], "record_hash": record["hash"],
                       "iat": int(utcnow().timestamp()), "exp": int((utcnow() + timedelta(days=3650)).timestamp()),
                       "approved_by": approved_by, "simulated_order_book": True}
            order["receipt"] = self.keys.sign_door(receipt)
        return record

    def _decision_record(self, db, *, customer_id: str, action: str, subject_id: str, description: str, approved_by: str, details=None, created_at: str | None = None):
        """Add a buyer-readable non-order decision to the same hash-linked log."""
        previous = db.execute("SELECT payload FROM records WHERE customer_id=? ORDER BY seq DESC LIMIT 1", (customer_id,)).fetchone()
        record = {
            "id": str(uuid.uuid4()), "customer_id": customer_id, "order_id": None,
            "decision_type": action, "subject_id": subject_id, "created_at": created_at or isotime(),
            "request": description, "filters_applied": [], "options_considered": [], "checks": [],
            "outcome": action, "approved_by": approved_by,
            "previous_hash": json.loads(previous[0])["hash"] if previous else None,
            "sentences": [description, f"The outcome is {action}. Approval came from {approved_by}."],
            "buyer_copy": True, "details": details or {},
        }
        record["hash"] = digest(record)
        db.execute("INSERT INTO records(id,customer_id,payload) VALUES(?,?,?)", (record["id"], customer_id, json.dumps(record)))
        return record

    def order(self, body: dict, idempotency_key: str | None):
        if not idempotency_key or len(idempotency_key) > 200:
            raise HTTPException(400, "An Idempotency-Key of at most 200 characters is required.")
        mandate = None
        try:
            mandate = self.mandate(body.get("mandate"), body.get("bot_id"))
        except HTTPException:
            pass
        payload, valid_offer = self._offer_check(body.get("offer"))
        customer = mandate["customer_id"] if mandate else "invalid-authority"
        with self.store.transaction() as db:
            if mandate:
                # A limit change may supersede the token after the first check.
                # Revalidate under the same write lock that guards the order so
                # a stale, higher ceiling cannot auto-commit during that race.
                mandate = self.mandate(body.get("mandate"), body.get("bot_id"))
                customer = mandate["customer_id"]
                previous = db.execute("SELECT payload FROM orders WHERE customer_id=? AND idempotency_key=?", (customer, idempotency_key)).fetchone()
                if previous:
                    return json.loads(previous[0])
            checks = self._checks(db, mandate, payload, valid_offer)
            order = {"id": str(uuid.uuid4()), "customer_id": customer, "idempotency_key": idempotency_key,
                     "created_at": isotime(), "offer": body.get("offer"), "offer_payload": payload, "mandate": body.get("mandate"), "bot_id": body.get("bot_id"),
                     "total": payload.get("total", 0), "checks": checks, "receipt": None, "notice": None, "question_spec": None,
                     "status": "requires_escalation", "tier": "Hold", "continue_url": None}
            order["earliest_orderable_date"] = payload.get("earliest_orderable_date")
            if not mandate:
                order["status"] = "rejected"
                record = self._record(db, order, "nobody; authority was rejected")
                rejection = {"message": "The buyer mandate is invalid, expired, or names a different bot. No stock was reserved.", "checks": checks, "record_id": record["id"]}
            else:
                rejection = None
                if valid_offer and checks[1]["passed"]:
                    shortage = self._replace_reservations(db, order, payload)
                    if shortage:
                        checks[4]["passed"], checks[4]["reason"] = self._delivery_check(payload, shortage)
                if all(check["passed"] for check in checks):
                    notify = self._soft_trade(mandate, payload) or self._departure(db, mandate, payload)["notify"]
                    order["status"], order["tier"] = "committed", "Notify" if notify else "Commit"
                    if notify:
                        order["notice"] = "This order is committed by approval by silence. You may undo it until the order cutoff."
                else:
                    base = (os.getenv("PUBLIC_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
                    order["continue_url"] = f"{base}/?view=signoff&order={order['id']}"
                    self._apply_question(order, mandate, payload, checks)
                order.setdefault("reserved", [])
                self._record(db, order, "the buyer's signed mandate" if order["status"] == "committed" else "nobody; awaiting human sign-off")
                self.store.save_order(db, order)
        if rejection:
            raise HTTPException(403, rejection)
        return order

    def _read_order(self, order_id: str):
        with self.store.connect() as db:
            order = self.store.order(db, order_id)
            review = self.store.get(db, "buyer_review", order_id)
        if not order:
            raise HTTPException(404, "Order not found.")
        if review:
            order.update(acknowledged=True, acknowledgment=review)
        return order

    def _current_mandate_for_order(self, db, order):
        customer = self.store.get(db, "customer", order["customer_id"])
        document = self.store.get(db, "mandate", customer["mandate_id"]) if customer else None
        try:
            if not document:
                raise jwt.InvalidTokenError("No active buyer mandate")
            mandate = self.keys.verify_buyer(document["mandate"], order["bot_id"])
            if mandate["customer_id"] != order["customer_id"]:
                raise jwt.InvalidTokenError("Wrong buyer")
        except (jwt.InvalidTokenError, ValueError, TypeError):
            raise HTTPException(403, "A current buyer-signed mandate naming this bot is required. The held order may still be rejected.") from None
        return document["mandate"], mandate

    def _authentic_bound_offer(self, order):
        try:
            payload = self.keys.verify_door(order.get("offer", ""), "offer", verify_exp=False)
            with self.store.connect() as db:
                stored = self.store.get(db, "offer", payload["jti"])
            return bool(stored and stored["offer"] == order["offer"] and payload.get("customer_id") == order["customer_id"] and payload.get("bot_id") == order.get("bot_id"))
        except (jwt.InvalidTokenError, TypeError, ValueError):
            return False

    async def _fresh_after_cutoff(self, order, session=None, *, approval_attempt=False):
        """Replace a spent-cutoff hold with a later offer, or mark it reject-only."""
        from door.matching import cutoff_passed

        with self.store.connect() as db:
            self._current_mandate_for_order(db, order)
        need = copy.deepcopy(order["offer_payload"]["need"])
        deadline = need.get("deadline", "")
        pools = self.reservation_pools(exclude_order=order["id"])
        result = await self.matcher.match(need, order["customer_id"], reservations=pools)
        candidates = [
            option for option in result["options"]
            if option.get("lines")
            and option.get("delivery_date", "") > deadline
            and not option.get("stock_shortfall")
            and all(not cutoff_passed(line.get("cutoff")) for line in option["lines"])
        ]
        if not candidates:
            message = self._no_later_delivery_message(order["offer_payload"])
            with self.store.transaction() as db:
                latest = self.store.order(db, order["id"])
                if latest["offer"] != order["offer"] or latest["status"] != "requires_escalation":
                    return latest
                order = latest
                order["mandate"], mandate = self._current_mandate_for_order(db, order)
                order["earliest_orderable_date"] = None
                order["checks"][4].update(passed=False, reason=message)
                previous_question = order.get("question_spec")
                self._apply_question(order, mandate, order["offer_payload"], order["checks"], reject_only=True)
                if previous_question != order["question_spec"]:
                    order["pending_answer"] = None
                # Keep the original hold and its reservations intact for rejection.
                self.store.save_order(db, order)
            if approval_attempt:
                raise HTTPException(409, {"message": message, "earliest_orderable_date": None})
            return order
        option = copy.deepcopy(min(candidates, key=lambda row: (row["delivery_date"], row["total"])))
        for line in option["lines"]:
            if not line.get("cutoff"):
                line["cutoff"] = (utcnow() + timedelta(hours=4)).isoformat()
                line["cutoff_source"] = "Simulated four-hour deadline: upstream supplied no cutoff"
        option["requested_cutoff_passed"] = True
        option["earliest_orderable_date"] = result.get("earliest_orderable_date") or option["delivery_date"]
        payload = {
            **option, "kind": "offer", "option_kind": option.get("kind", "single_product"),
            "jti": str(uuid.uuid4()), "customer_id": order["customer_id"], "bot_id": order["bot_id"],
            "need": result["need"], "currency": "EUR", "prices_include_vat": False,
            "iat": int(utcnow().timestamp()), "exp": int((utcnow() + timedelta(minutes=5)).timestamp()),
            "expires_at": (utcnow() + timedelta(minutes=5)).isoformat(), "reserves_stock": False,
            "options_considered": copy.deepcopy(result["options"]),
        }
        token = self.keys.sign_door(payload)
        with self.store.transaction() as db:
            latest = self.store.order(db, order["id"])
            if latest["offer"] != order["offer"] or latest["status"] != "requires_escalation":
                return latest
            order = latest
            order["mandate"], mandate = self._current_mandate_for_order(db, order)
            self.store.put(db, "offer", payload["jti"], {"offer": token, "payload": payload})
            order.update(
                offer=token, offer_payload=payload, total=payload["total"],
                earliest_orderable_date=payload["earliest_orderable_date"],
                requires_price_confirmation=True, requires_delivery_confirmation=True,
                pending_answer=None,
            )
            order["checks"] = self._checks(db, mandate, payload, True)
            shortage = self._replace_reservations(db, order, payload)
            if shortage:
                order["checks"][4]["passed"], order["checks"][4]["reason"] = self._delivery_check(payload, shortage)
            self._apply_question(order, mandate, payload, order["checks"], reject_only=False)
            requested_by = f" at the request of simulated buyer session {session['id']}" if session else ""
            self._record(db, order, "nobody; the later fresh offer awaits explicit buyer confirmation", [
                "The original order cutoff had passed." + requested_by,
                f"A fresh signed offer for {payload['delivery_date']} with current prices and stock is now displayed before approval.",
            ])
            self.store.save_order(db, order)
        return order

    async def _current_offer_payload(self, order, quantity=None):
        from door.matching import whole_packs, cutoff_passed
        with self.store.connect() as db:
            self._current_mandate_for_order(db, order)
        payload = copy.deepcopy(order["offer_payload"])
        lines = payload.get("lines", [])
        basket = payload.get("need", {}).get("product_type") == "usual basket"
        prior_count = float(payload["need"].get("quantity", 1))
        prior_quantity = None if basket else sum(float(line["quantity"]) for line in lines)
        if quantity is not None:
            try:
                quantity = float(quantity)
                if not math.isfinite(quantity) or quantity <= 0:
                    raise ValueError()
            except (ValueError, TypeError):
                raise HTTPException(422, "Enter a positive finite quantity.") from None
            payload["need"]["quantity"] = quantity
        groups = {}
        for line in lines:
            groups.setdefault(line["delivery_date"][:10], []).append(str(line["id"]))
        prices, stocks = {}, {}
        for day, ids in groups.items():
            prices[day], stocks[day] = await asyncio.gather(self.valio.prices(order["customer_id"], day, ids), self.valio.availability(order["customer_id"], day, ids))
        pools = self.reservation_pools(exclude_order=order["id"])
        for line in lines:
            pid, day = str(line["id"]), line["delivery_date"][:10]
            price, stock = prices[day].get(pid, {}), stocks[day].get(pid, {})
            if price.get("error") or not isinstance(price.get("unit_price"), (float, int)):
                raise HTTPException(409, "A fresh price is unavailable. The held order cannot be approved yet.")
            if quantity is not None:
                target = float(line["quantity"]) * quantity / prior_count if basket else quantity * float(line["quantity"]) / prior_quantity
                line["packs"], _ = whole_packs(target, line["pack_size"], line.get("lot_size", 1))
                line["quantity"] = round(line["packs"] * line["pack_size"], 6)
            line["unit_price"] = float(price["unit_price"])
            line["total"] = round(line["packs"] * line["unit_price"], 2)
            line["price_source"] = price.get("source", "Valio mock API")
            if line.get("simulated_stock"):
                # Keep the explicitly chosen simulation scenario while refreshing real prices.
                stock = {"delivery_date": line["delivery_date"], "available": line.get("raw_available", line.get("available", 0)), "cutoff": line.get("cutoff"), "deliverable": True, "sales_block": False}
            line["delivery_date"] = (stock.get("delivery_date") or line["delivery_date"])[:10]
            line["inventory_date"] = line["delivery_date"]
            pool = f"{pid}|{line['inventory_date']}"
            line["raw_available"] = float(stock.get("available", 0))
            line["reserved_at_match"] = pools.get(pool, 0)
            line["available"] = max(0, line["raw_available"] - line["reserved_at_match"])
            line["stock_shortfall"] = max(0, line["packs"] - line["available"])
            line["cutoff"] = stock.get("cutoff") or line.get("cutoff") or (utcnow() + timedelta(hours=4)).isoformat()
            line["on_time"] = bool(stock.get("deliverable") and not stock.get("sales_block") and line["delivery_date"] <= payload["need"]["deadline"] and not cutoff_passed(line["cutoff"]))
            pools[pool] = pools.get(pool, 0) + min(line["packs"], line["available"])
        payload.update(jti=str(uuid.uuid4()), iat=int(utcnow().timestamp()), exp=int((utcnow() + timedelta(minutes=5)).timestamp()), expires_at=(utcnow() + timedelta(minutes=5)).isoformat())
        payload["total"] = round(sum(line["total"] for line in lines), 2)
        payload["delivery_date"] = max(line["delivery_date"] for line in lines)
        payload["on_time"] = all(line["on_time"] and not line["stock_shortfall"] for line in lines)
        payload["overage"] = 0 if basket else round(sum(line["quantity"] for line in lines) - payload["need"]["quantity"], 6)
        if basket and quantity is not None:
            for row in payload["need"].get("lines", []):
                row["quantity"] = float(row["quantity"]) * quantity / prior_count
        return payload

    async def get_order(self, order_id: str, *, buyer_token=None, authorization=None, bot_id=None):
        order = self._read_order(order_id)
        self.authorize_read(
            order["customer_id"], buyer_token=buyer_token,
            authorization=authorization, bot_id=bot_id, expected_bot_id=order.get("bot_id"),
        )
        if order["status"] != "requires_escalation":
            return order
        if any(parse_time(line.get("cutoff")) <= utcnow() for line in order["offer_payload"].get("lines", [])):
            if order.get("approvable") is False:
                return order
            return await self._fresh_after_cutoff(order)
        if order["offer_payload"].get("exp", 0) > utcnow().timestamp() or not self._authentic_bound_offer(order):
            return order
        payload = await self._current_offer_payload(order)
        token = self.keys.sign_door(payload)
        with self.store.transaction() as db:
            latest = self.store.order(db, order_id)
            if latest["offer"] != order["offer"] or latest["status"] != "requires_escalation":
                return latest
            order["mandate"], mandate = self._current_mandate_for_order(db, order)
            self.store.put(db, "offer", payload["jti"], {"offer": token, "payload": payload})
            order.update(offer=token, offer_payload=payload, total=payload["total"], requires_price_confirmation=True, pending_answer=None)
            order["checks"] = self._checks(db, mandate, payload, True)
            shortage = self._replace_reservations(db, order, payload)
            if shortage:
                order["checks"][4]["passed"], order["checks"][4]["reason"] = self._delivery_check(payload, shortage)
            self._apply_question(order, mandate, payload, order["checks"])
            self._record(db, order, "nobody; the fresh offer awaits explicit price confirmation", ["The held offer expired. A fresh signed offer and its current price are now shown to the buyer."])
            self.store.save_order(db, order)
        return order

    def record(self, record_id: str):
        with self.store.connect() as db:
            row = db.execute("SELECT payload FROM records WHERE id=?", (record_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Record not found.")
        return json.loads(row[0])

    def _prepare_answer(self, order, body):
        spec = order.get("question_spec")
        if not spec:
            raise HTTPException(409, "This order has no preference question to answer.")
        answer, scope = body.get("answer", "approve"), body.get("scope", "just_this_once")
        if answer not in {row["value"] for row in spec["answers"]} or scope not in spec["scope_options"]:
            raise HTTPException(422, "Choose an accepted answer and one of the displayed scopes.")
        if scope in ("always", "always_ask") and not spec.get("preferences"):
            raise HTTPException(422, "Standing preference answers require an exact displayed preference; product-property confirmation has its own exact scope.")
        details = "; ".join(f"{row['name']}={row.get('offered')}" for row in spec.get("preferences", [])) or "the displayed order"
        scope_text = {"always": "remember only for", "always_ask": "ask me each time for", "just_this_once": "apply only to this order for"}[scope]
        summary = f"{answer.capitalize()}: {scope_text} {spec.get('product_type')}: {details}."
        pending = {"answer": answer, "scope": scope, "confirmation_summary": summary, "question_hash": spec["hash"]}
        if body.get("question_hash") and body["question_hash"] != spec["hash"]:
            raise HTTPException(409, "The question changed. Review the current question before answering.")
        if body.get("confirmation_summary") and body["confirmation_summary"] != summary:
            raise HTTPException(409, "The answer summary changed. Review and confirm the displayed summary.")
        return pending

    def _new_buyer_version(self, db, previous, learned):
        """Called only by explicit simulated-human confirmation/removal routes."""
        payload = copy.deepcopy(previous)
        payload.update(jti=str(uuid.uuid4()), version=previous["version"] + 1, iat=int(utcnow().timestamp()), learned_preferences=learned)
        token = self.keys.sign_buyer(payload["customer_id"], payload)
        self.store.put(db, "mandate", payload["jti"], {"mandate": token, "payload": payload})
        customer = self.store.get(db, "customer", payload["customer_id"])
        customer["mandate_id"] = payload["jti"]
        self.store.put(db, "customer", customer["id"], customer)
        return token, payload

    def _confirm_properties(self, db, order, body):
        entries = body.get("confirmations", [])
        if entries and body.get("confirmed") is not True:
            raise HTTPException(409, {"confirmation_summary": "Confirm each named property only for its named product."})
        lines = {str(line["id"]): line for line in order["offer_payload"].get("lines", [])}
        mandate = self.keys.verify_buyer(order["mandate"], order["bot_id"])
        for entry in entries:
            pid = str(entry.get("product_id", ""))
            prop = normalize_property(str(entry.get("property", "")))
            required = self._required_properties(mandate, order["offer_payload"], lines[pid]) if pid in lines else set()
            if entry.get("confirmed") is not True or pid not in lines or prop not in required:
                raise HTTPException(422, "A confirmation must name one displayed product and one required property.")
            product = self.catalog.get(pid) or {}
            if prop in product.get("contradicted_properties", {}):
                raise HTTPException(409, "The manufacturer states contrary information. A buyer confirmation cannot override it.")
            confirmation = {"kind": "property-confirmation", "jti": str(uuid.uuid4()), "customer_id": order["customer_id"], "product_id": pid, "property": prop, "confirmed": True, "source": "buyer_confirmation", "order_id": order["id"], "iat": int(utcnow().timestamp()), "exp": mandate["exp"]}
            confirmation["signature"] = self.keys.sign_buyer(order["customer_id"], confirmation)
            self.store.put(db, "confirmation", f"{order['customer_id']}:{pid}:{prop}", confirmation)

    async def signoff(self, order_id: str, body: dict, buyer_token: str | None = None):
        action = body.get("action")
        if action == "acknowledge":
            return self.acknowledge(order_id, body, buyer_token)
        if action not in ("approve", "reject", "edit", "prepare_answer"):
            raise HTTPException(422, "Choose approve, reject, edit, or prepare_answer.")
        snapshot = self._read_order(order_id)
        session = None if action == "prepare_answer" else self.buyer_session(buyer_token, snapshot["customer_id"])
        if body.get("customer_id") and body["customer_id"] != snapshot["customer_id"]:
            raise HTTPException(403, "This order belongs to a different buyer.")
        if snapshot["status"] != "requires_escalation":
            raise HTTPException(409, "Only a held order can be signed off.")
        if action == "approve" and any(parse_time(line.get("cutoff")) <= utcnow() for line in snapshot["offer_payload"].get("lines", [])):
            if snapshot.get("approvable") is False:
                raise HTTPException(409, {
                    "message": self._no_later_delivery_message(snapshot["offer_payload"]),
                    "earliest_orderable_date": None,
                })
            return await self._fresh_after_cutoff(snapshot, session, approval_attempt=True)
        edited_payload = None
        if action == "edit":
            if not self._authentic_bound_offer(snapshot):
                raise HTTPException(409, "An invalid or differently bound offer cannot be edited into an authorized order.")
            edited_payload = await self._current_offer_payload(snapshot, body.get("quantity"))
        with self.store.transaction() as db:
            order = self.store.order(db, order_id)
            if order["offer"] != snapshot["offer"] or order["status"] != "requires_escalation":
                raise HTTPException(409, "The held order changed. Review its latest state before signing off.")
            # Human actions use the current signed limits, never the held order's old ceilings.
            mandate = None
            if action != "reject":
                prior_spec = order.get("question_spec")
                order["mandate"], mandate = self._current_mandate_for_order(db, order)
                _, valid_offer = self._offer_check(order["offer"])
                order["checks"] = self._checks(db, mandate, order["offer_payload"], valid_offer)
                self._apply_question(order, mandate, order["offer_payload"], order["checks"])
                if prior_spec != order["question_spec"]:
                    order["pending_answer"] = None
            notes = []
            if action == "prepare_answer" or (action in ("approve", "reject") and body.get("answer") and body.get("scope") in ("always", "always_ask") and (body.get("confirmed") is not True or not order.get("pending_answer"))):
                pending = self._prepare_answer(order, body)
                order["pending_answer"] = pending
                order["confirmation_summary"] = pending["confirmation_summary"]
                self.store.save_order(db, order)
                return order
            if action == "edit":
                token = self.keys.sign_door(edited_payload)
                self.store.put(db, "offer", edited_payload["jti"], {"offer": token, "payload": edited_payload})
                order.update(offer=token, offer_payload=edited_payload, total=edited_payload["total"], requires_price_confirmation=False, pending_answer=None, edited_by_human=True)
                order["checks"] = self._checks(db, mandate, edited_payload, True)
                shortage = self._replace_reservations(db, order, edited_payload)
                if shortage:
                    order["checks"][4]["passed"], order["checks"][4]["reason"] = self._delivery_check(edited_payload, shortage)
                order["status"] = "committed" if all(check["passed"] for check in order["checks"]) else "requires_escalation"
                order["tier"] = "Commit" if order["status"] == "committed" else "Hold"
                self._apply_question(order, mandate, edited_payload, order["checks"])
                notes.append("The human edited the quantity. All ten checks ran again and the stock reservation was updated atomically.")
                self._tighten(db, order["customer_id"], order["offer_payload"]["need"]["product_type"], "The buyer edited an order, so the agent must ask next time for this kind of order.")
                notes.append("Current freedom was tightened to zero after the edit; the signed ceiling is unchanged.")
            elif action == "approve":
                _, valid = self._offer_check(order.get("offer"))
                if not valid or not self._authentic_bound_offer(order):
                    raise HTTPException(409, "The offer is invalid or expired. Open the sign-off page to see a fresh offer before approving its price.")
                if order.get("requires_price_confirmation") and body.get("offer") != order["offer"]:
                    raise HTTPException(409, "Confirm the exact fresh offer shown on the sign-off page before approval.")
                if any(parse_time(line.get("cutoff")) <= utcnow() for line in order["offer_payload"].get("lines", [])):
                    raise HTTPException(409, "The order cutoff has passed; obtain a new offer.")
                if not self._reservation_covers_order(db, order):
                    raise HTTPException(409, "The requested quantity is not fully reserved. Choose an available alternative or edit the quantity before approving; sign-off cannot create stock.")
                pending = self._prepare_answer(order, body) if body.get("answer") or body.get("scope") else None
                if pending and pending["answer"] != "approve":
                    raise HTTPException(422, "A reject answer must use the reject action.")
                if pending and order.get("pending_answer") and pending != order["pending_answer"]:
                    raise HTTPException(409, "The confirmed answer differs from the displayed summary. Prepare the new answer first.")
                self._confirm_properties(db, order, body)
                checks = self._checks(db, mandate, order["offer_payload"], True)
                if not checks[3]["passed"]:
                    raise HTTPException(409, "Explicit product-property confirmation is required for missing manufacturer data; contrary manufacturer data cannot be overridden.")
                if pending:
                    notes.append(f"The buyer answered {pending['answer']} with scope {pending['scope']}: {pending['confirmation_summary']}")
                    if pending["scope"] in ("always", "always_ask"):
                        if body.get("confirmed") is not True:
                            raise HTTPException(409, {"confirmation_summary": pending["confirmation_summary"]})
                        learned = copy.deepcopy(mandate.get("learned_preferences", []))
                        for preference in order["question_spec"]["preferences"]:
                            if preference["name"] == "product_type":
                                raise HTTPException(422, "Product-type mismatch cannot be granted as a standing preference.")
                            entry = {"id": str(uuid.uuid4()), "product_type": preference["product_type"], "preference": preference["name"], "accepted_value": preference["offered"], "scope": pending["scope"], "question": order["question_spec"]["needed"], "answer": pending["answer"], "confirmation_summary": pending["confirmation_summary"], "created_from_order": order_id}
                            if not any(all(existing.get(k) == entry.get(k) for k in ("product_type", "preference", "accepted_value", "scope", "answer")) for existing in learned):
                                learned.append(entry)
                        token, mandate = self._new_buyer_version(db, mandate, learned)
                        order["mandate"] = token
                        notes.append("A new version was signed with the simulated buyer key. Every signed ceiling is unchanged.")
                order["checks"] = self._checks(db, mandate, order["offer_payload"], True)
                order["status"], order["tier"] = "committed", "Commit"
                order["requires_price_confirmation"] = False
                notes.append("The human approved this displayed order; no signed ceiling was widened.")
                self._clean_approval(db, order, mandate)
            else:
                if body.get("answer") and body.get("scope") in ("always", "always_ask"):
                    pending = self._prepare_answer(order, body)
                    if pending["answer"] != "reject" or order.get("pending_answer") != pending or body.get("confirmed") is not True:
                        raise HTTPException(409, "Prepare and confirm the exact reject summary before remembering it.")
                    order["mandate"], mandate = self._current_mandate_for_order(db, order)
                    learned = copy.deepcopy(mandate.get("learned_preferences", []))
                    for preference in order["question_spec"]["preferences"]:
                        if preference["name"] == "product_type":
                            raise HTTPException(422, "Product-type mismatch cannot be granted as a standing preference.")
                        learned.append({"id": str(uuid.uuid4()), "product_type": preference["product_type"], "preference": preference["name"], "accepted_value": preference["offered"], "scope": pending["scope"], "question": order["question_spec"]["needed"], "answer": "reject", "confirmation_summary": pending["confirmation_summary"], "created_from_order": order_id})
                    order["mandate"], _ = self._new_buyer_version(db, mandate, learned)
                    notes.append("The buyer confirmed this exact standing rejection in a new buyer-signed mandate version.")
                order["status"], order["tier"] = "rejected", "Hold"
                db.execute("UPDATE reservations SET active=0 WHERE order_id=?", (order_id,))
                order["reserved"] = []
                notes.append("The human rejected this order and its reservation was released.")
            approver = f"simulated buyer session {session['id']} on the sign-off page" if session else "an unconfirmed answer preparation"
            self._record(db, order, approver, notes)
            self.store.save_order(db, order)
        return order

    def acknowledge(self, order_id, body, buyer_token: str | None = None):
        snapshot = self._read_order(order_id)
        session = self.buyer_session(buyer_token, snapshot["customer_id"])
        if body.get("confirmed") is not True:
            raise HTTPException(409, "Confirm that this committed order was right before counting a clean human review.")
        with self.store.transaction() as db:
            order = self.store.order(db, order_id)
            if not order:
                raise HTTPException(404, "Order not found.")
            if body.get("customer_id") != order["customer_id"]:
                raise HTTPException(403, "This order belongs to a different buyer.")
            if order["status"] != "committed":
                raise HTTPException(409, "Only a committed order can receive a clean-order acknowledgment.")
            prior = self.store.get(db, "buyer_review", order_id)
            if prior:
                return {**order, "acknowledged": True, "acknowledgment": prior}
            _, mandate = self._current_mandate_for_order(db, order)
            review = copy.deepcopy(order)
            review["checks"] = self._checks(db, mandate, order["offer_payload"], self._authentic_bound_offer(order), exclude_order_id=order_id)
            self._clean_approval(db, review, mandate)
            review["status"] = "reviewed"
            record = self._record(db, review, f"simulated buyer session {session['id']} explicitly confirming the committed order was right", ["This acknowledgment leaves the original receipt, order and stock reservation unchanged. It contributes evidence only; no freedom or usual anchor changes automatically."])
            acknowledgment = {"kind": "buyer-review", "jti": str(uuid.uuid4()), "order_id": order_id, "customer_id": order["customer_id"], "review_record_id": record["id"], "record_hash": record["hash"], "confirmed": True, "exp": mandate["exp"]}
            acknowledgment["buyer_signature"] = self.keys.sign_buyer(order["customer_id"], acknowledgment)
            self.store.put(db, "buyer_review", order_id, acknowledgment)
        return {**order, "acknowledged": True, "acknowledgment": acknowledgment}

    def undo(self, order_id: str, buyer_token: str | None = None):
        snapshot = self._read_order(order_id)
        session = self.buyer_session(buyer_token, snapshot["customer_id"])
        with self.store.transaction() as db:
            order = self.store.order(db, order_id)
            if not order:
                raise HTTPException(404, "Order not found.")
            if order["status"] != "committed":
                raise HTTPException(409, "Only a committed order can be undone.")
            if any(parse_time(line.get("cutoff")) <= utcnow() for line in order["offer_payload"].get("lines", [])):
                raise HTTPException(409, "The order cutoff has passed.")
            order["status"], order["receipt"] = "withdrawn", None
            db.execute("UPDATE reservations SET active=0 WHERE order_id=?", (order_id,))
            order["reserved"] = []
            self._tighten(db, order["customer_id"], order["offer_payload"]["need"]["product_type"], "The buyer used undo to withdraw this order. Current freedom is now zero and the agent must ask next time.")
            self._record(db, order, f"simulated buyer session {session['id']} using undo", ["The order was withdrawn and its stock reservation released.", "Undo tightened current freedom to zero for this kind of order. The signed ceiling is unchanged."])
            self.store.save_order(db, order)
        return order

    def change_limit(self, limit_id: str, body: dict, buyer_token: str | None = None, proof_token: str | None = None):
        """Change a spend ceiling only with a fresh proof bound to this human session."""
        customer_id = body.get("customer_id", "")
        session = self.buyer_session(buyer_token, customer_id)
        names = {"per_order_cap": "Spend per order", "monthly_budget": "Monthly budget"}
        if limit_id == "departure":
            raise HTTPException(422, "Departure from usual changes by buyer-approved proposal, not by this operation.")
        if limit_id not in names:
            raise HTTPException(404, "Only per_order_cap and monthly_budget can be changed here.")
        with self.store.transaction() as db:
            customer = self.store.get(db, "customer", customer_id)
            if not customer:
                raise HTTPException(404, "Customer not found.")
            mandate = self.store.get(db, "mandate", customer["mandate_id"])["payload"]
            proof = self.store.get(db, "identification_proof", digest(proof_token or ""))
            if not proof_token:
                raise HTTPException(403, "Identify yourself before changing a signed limit.")
            if not proof:
                raise HTTPException(403, "The identification proof is invalid.")
            if proof.get("used_at"):
                raise HTTPException(403, "The identification proof has already been used; identify yourself again.")
            if proof.get("customer_id") != customer_id or proof.get("buyer_session_key") != digest(buyer_token):
                raise HTTPException(403, "The identification proof belongs to a different buyer session or customer.")
            changed_at = utcnow()
            if parse_time(proof.get("expires_at")) <= changed_at:
                raise HTTPException(403, "The identification proof has expired; identify yourself again.")
            value = body.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HTTPException(422, "The new limit must be a positive finite amount.")
            if isinstance(value, int):
                try:
                    float(value)
                except OverflowError:
                    raise HTTPException(422, "The new limit must be a positive finite amount.") from None
                if value <= 0:
                    raise HTTPException(422, "The new limit must be a positive finite amount.")
                new_value = value
            else:
                if not math.isfinite(value) or value <= 0:
                    raise HTTPException(422, "The new limit must be a positive finite amount.")
                new_value = int(value) if value.is_integer() else value
            old_value = mandate["limits"][limit_id]
            if new_value == old_value:
                raise HTTPException(422, "The new limit must differ from its current value.")
            updated = copy.deepcopy(mandate)
            updated["limits"][limit_id] = new_value
            token, payload = self._new_buyer_version(db, updated, updated.get("learned_preferences", []))
            name = names[limit_id]
            at = changed_at.isoformat()
            change = {
                "limit_id": limit_id, "name": name,
                "old_value": old_value, "new_value": new_value,
                "signed_by": proof["person"]["name"],
                "identified_by": proof["method"], "at": at,
            }
            old_text = str(old_value) if isinstance(old_value, int) else f"{old_value:g}"
            new_text = str(new_value) if isinstance(new_value, int) else f"{new_value:g}"
            description = (
                f"{name} changed from EUR {old_text} to EUR {new_text}. "
                f"{change['signed_by']} signed this change using {change['identified_by']} at {at}. "
                "The identification and company authorisation are simulated. "
                f"Buyer-signed mandate version {payload['version']} supersedes version {mandate['version']}."
            )
            record = self._decision_record(
                db, customer_id=customer_id, action="limit_changed", subject_id=limit_id,
                description=description,
                approved_by=f"{change['signed_by']} using {change['identified_by']} for simulated buyer session {session['id']}",
                details={**change, "mandate_id": payload["jti"], "mandate_version": payload["version"]},
                created_at=at,
            )
            proof["used_at"] = at
            self.store.put(db, "identification_proof", digest(proof_token), proof)
            response_limits = self._limits(db, customer_id)
        return {"limits": response_limits, "change": change, "mandate_version": payload["version"], "record_id": record["id"]}

    def _limits(self, db, customer_id: str):
        customer = self.store.get(db, "customer", customer_id)
        if not customer:
            raise HTTPException(404, "Customer not found.")
        payload = self.store.get(db, "mandate", customer["mandate_id"])["payload"]
        kind = "usual basket" if self.store.get(db, "usual", customer_id) else "drinking milk"
        state = self.store.get(db, "freedom", f"{customer_id}:{kind}") or {}
        proposals = [proposal for proposal in self.store.list(db, "proposal") if proposal["customer_id"] == customer_id]
        used = self._monthly_committed_spend(db, customer_id)
        limits = payload["limits"]
        remaining = round(max(0.0, float(limits["monthly_budget"]) - used), 2)
        return {"customer_id": customer_id, "mandate_version": payload["version"],
                "product_scope_label": limits.get("allowed_categories_label", "Buyer-signed product categories"),
                "must_hold_label": limits.get("must_hold_label", "Buyer-required properties"), "rows": [
            {"id": "per_order_cap", "name": "Spend per order", "signed_ceiling": limits["per_order_cap"], "current_freedom": limits["per_order_cap"], "status": "Buyer signed", "reason": "The buyer signed this limit; the door cannot raise it."},
            {"id": "monthly_budget", "name": "Monthly budget", "signed_ceiling": limits["monthly_budget"], "current_freedom": limits["monthly_budget"], "used": used, "remaining": remaining, "status": "Buyer signed", "reason": "The budget counts orders committed through this door."},
            {"id": "departure", "name": "Departure from usual", "signed_ceiling": limits["departure_ceiling"], "current_freedom": min(limits["departure_ceiling"], state.get("current_freedom", limits["current_freedom"])), "status": state.get("status", "No history"), "reason": state.get("reason", "No confirmed usual-order history has been seeded yet."), "kind": kind}],
            "learned_preferences": payload.get("learned_preferences", []), "proposals": proposals}

    def limits(self, customer_id: str):
        with self.store.connect() as db:
            return self._limits(db, customer_id)

    def reset_limit(self, limit_id: str, body: dict, buyer_token: str | None = None):
        customer_id = body.get("customer_id", "hotel")
        session = self.buyer_session(buyer_token, customer_id)
        if any(key in body for key in ("signed_ceiling", "limits", "current_freedom")):
            raise HTTPException(403, "This action cannot raise or replace a signed ceiling or current freedom.")
        with self.store.transaction() as db:
            customer = self.store.get(db, "customer", customer_id)
            if not customer:
                raise HTTPException(404, "Customer not found.")
            mandate = self.store.get(db, "mandate", customer["mandate_id"])["payload"]
            if limit_id == "departure":
                kind = "usual basket" if self.store.get(db, "usual", customer_id) else "drinking milk"
                self._tighten(db, customer_id, kind, "The buyer reset current freedom to zero. The agent must ask next time; the signed ceiling is unchanged.")
                record = self._decision_record(
                    db, customer_id=customer_id, action="freedom_reset",
                    subject_id=f"{customer_id}:{kind}",
                    description=f"The buyer reset current freedom for {kind} to zero; the signed ceiling remains {mandate['limits']['departure_ceiling']}.",
                    approved_by=f"simulated buyer session {session['id']}",
                    details={"kind": kind, "resulting_freedom": 0, "signed_ceiling": mandate["limits"]["departure_ceiling"]},
                )
                return {"status": "reset", "limit_id": "departure", "current_freedom": 0, "signed_ceiling": mandate["limits"]["departure_ceiling"], "record_id": record["id"], "record_hash": record["hash"]}
            learned = mandate.get("learned_preferences", [])
            if not any(entry["id"] == limit_id for entry in learned):
                raise HTTPException(409, "There is no learned preference with this identifier to remove.")
            retained = [entry for entry in learned if entry["id"] != limit_id]
            token, payload = self._new_buyer_version(db, mandate, retained)
            record = self._decision_record(
                db, customer_id=customer_id, action="learned_preference_removed", subject_id=limit_id,
                description=f"The buyer removed learned preference {limit_id} and registered mandate version {payload['version']}; no signed ceiling changed.",
                approved_by=f"simulated buyer session {session['id']}",
                details={"removed_id": limit_id, "mandate_id": payload["jti"], "version": payload["version"]},
            )
        return {"status": "removed", "removed_id": limit_id, "mandate": token, "payload": payload, "record_id": record["id"], "record_hash": record["hash"], **self.limits(customer_id)}
