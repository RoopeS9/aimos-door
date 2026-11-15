"""Deterministic, auditable need matching and whole-sales-pack arithmetic."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING

from door.catalog import normalize_property
from door.mandates import category_matches


def whole_packs(quantity: float, pack_size: float, lot_size: float = 1) -> tuple[int, float]:
    """Round upward using decimal inputs. Never promise a fractional sales pack."""
    q, size = Decimal(str(quantity)), Decimal(str(pack_size))
    if q <= 0 or size <= 0:
        raise ValueError("Quantity and pack size must be positive.")
    lot = max(1, math.ceil(float(lot_size)))
    packs = int((q / size / lot).to_integral_value(rounding=ROUND_CEILING)) * lot
    return packs, float(Decimal(packs) * size - q)


def pack_content(product: dict, price: dict, unit: str) -> tuple[float, str]:
    """Reconcile rounded comparison prices with the documented pack hierarchy."""
    if unit in {"PACK", "PACKS", "ST", "EA"}:
        return 1, "sales unit"
    if str(price.get("comparison_unit", "")).upper() != unit:
        raise ValueError("No reliable conversion for requested unit.")
    sale, comparison = price.get("unit_price"), price.get("comparison_price")
    if sale is None or not comparison or sale <= 0 or comparison <= 0:
        raise ValueError("No usable comparison price.")
    ratio = float(sale) / float(comparison)
    # Comparison prices are rounded to cents; don't turn a true 20 L case into
    # 19.94 L. Manufacturer name plus explicit sales-unit hierarchy corroborate.
    name = product.get("names", {}).get("en") or product.get("name", "")
    match = re.search(r"(?<![\d.,])([\d]+(?:[.,]\d+)?)\s*(kg|ml|cl|dl|l)\b", name, re.I)
    if match and product.get("sales_unit_multiplier"):
        amount = float(match.group(1).replace(",", "."))
        named_unit = match.group(2).lower()
        factors = {"l": 1, "dl": .1, "cl": .01, "ml": .001, "kg": 1}
        same_dimension = (unit == "L" and named_unit != "kg") or (unit == "KG" and named_unit == "kg")
        candidate = amount * factors[named_unit] * product["sales_unit_multiplier"]
        if same_dimension and candidate > 0 and abs(candidate - ratio) / candidate <= .02:
            return round(candidate, 6), "manufacturer name × sales-unit hierarchy, checked against comparison price"
    return round(ratio, 6), "catalog price / comparison price"


def cutoff_passed(value: str | None, now: datetime | None = None) -> bool:
    if not value:
        return False
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            # Upstream local timestamps without offset are Finnish time.
            from zoneinfo import ZoneInfo
            timestamp = timestamp.replace(tzinfo=ZoneInfo("Europe/Helsinki"))
        return timestamp <= (now or datetime.now(timezone.utc))
    except (ValueError, TypeError):
        return True


def option_rank(option: dict) -> tuple:
    quantity = sum(float(x.get("quantity", 0)) for x in option.get("lines", []))
    return (
        not bool(option.get("on_time")), not bool(option.get("within_preferences", True)),
        float(option.get("overage", 0)), float(option.get("total", 0)) / max(quantity, .000001),
        -sum(float(line.get("quantity", 0)) for line in option.get("lines", []) if line.get("on_time")),
        tuple((str(line.get("id", "")), str(line.get("delivery_date", "")), float(line.get("packs", 0))) for line in option.get("lines", [])),
        str(option.get("kind", "")),
    )


def rank_options(options: list[dict]) -> list[dict]:
    return sorted(options, key=option_rank)


def _preferences(product: dict, pack_size: float, need: dict) -> tuple[bool, bool]:
    pref = need.get("preferences") or {}
    hard = pref.get("required", {})
    soft = pref.get("soft", {})
    # Simple clients can send {brand: ..., pack_size: ...}; those are soft.
    soft = {**{k: v for k, v in pref.items() if k in {"brand", "pack_size"}}, **soft}
    values = {**product, "pack_size": pack_size}
    inside = all(values.get(k) in v if isinstance(v, list) else values.get(k) == v for k, v in hard.items())
    traded = any(values.get(k) not in v if isinstance(v, list) else values.get(k) != v for k, v in soft.items())
    return inside, traded


class Matcher:
    def __init__(self, catalog, valio, decision_labeler=None):
        self.catalog, self.valio = catalog, valio
        self.decision_labeler = decision_labeler

    @staticmethod
    def _required_properties(need, product):
        required = [normalize_property(prop) for prop in need.get("properties", [])]
        prefixes = need.get("_must_hold_categories")
        if category_matches(product.get("category"), prefixes):
            required.extend(need.get("_must_hold", []))
        return list(dict.fromkeys(required))

    @staticmethod
    def _line(product, price, inventory, need, quantity, reservations):
        unit, deadline = need["unit"], need["deadline"]
        if price.get("error") or not inventory.get("deliverable") or inventory.get("sales_block"):
            return None
        try:
            size, conversion_source = pack_content(product, price, unit)
            packs, _ = whole_packs(quantity, size, product.get("lot_size", 1))
        except (ValueError, TypeError):
            return None
        pid = product["id"]
        delivery = inventory.get("delivery_date")
        if not delivery:
            return None
        pool = f"{pid}|{delivery}"
        reserved = float(reservations.get(pool, reservations.get(pid, 0)))
        raw_available = float(inventory.get("available", 0))
        available = max(0, raw_available - reserved)
        cutoff = inventory.get("cutoff")
        within, soft_trade = _preferences(product, size, need)
        missing = [prop for prop in Matcher._required_properties(need, product) if product.get("properties", {}).get(prop) != "manufacturer" and product.get("property_sources", {}).get(prop) != "buyer_confirmation"]
        line = {**product, "product_id": pid, "packs": packs, "pack_size": size,
                "quantity": round(packs * size, 6), "unit": unit, "conversion_source": conversion_source,
                "unit_price": float(price["unit_price"]), "total": round(packs * float(price["unit_price"]), 2),
                "available": available, "raw_available": raw_available, "reserved_at_match": reserved,
                "inventory_date": delivery, "delivery_date": delivery, "cutoff": cutoff,
                "on_time": delivery <= deadline and not cutoff_passed(cutoff),
                "subject_to_supplier": inventory.get("subject_to_supplier", False),
                "stock_source": inventory.get("source", "Valio mock API"), "price_source": price.get("source", "Valio mock API"),
                "tax": price.get("tax"), "within_preferences": within, "soft_trade": soft_trade,
                "missing_properties": missing, "stock_shortfall": max(0, packs - available)}
        if inventory.get("simulated_stock"):
            line["simulated_stock"] = True
            line["simulation_explanation"] = inventory["simulation_explanation"]
        return line

    @staticmethod
    def _resize(line, packs):
        updated = dict(line)
        updated.update(packs=packs, quantity=round(packs * line["pack_size"], 6),
                       total=round(packs * line["unit_price"], 2),
                       stock_shortfall=max(0, packs - line["available"]))
        return updated

    @staticmethod
    def _option(lines, need, filters, *, kind="single_product", description="", partial=False, soft_trade=False):
        total_quantity = sum(line["quantity"] for line in lines)
        result = {
            "lines": lines, "total": round(sum(line["total"] for line in lines), 2),
            "delivery_date": max(line["delivery_date"] for line in lines),
            "on_time": all(line["on_time"] and not line["stock_shortfall"] for line in lines),
            "overage": round(max(0, total_quantity - need["quantity"]), 6),
            "within_preferences": all(line["within_preferences"] for line in lines),
            "soft_trade": soft_trade or any(line["soft_trade"] for line in lines),
            "partial": partial, "stock_shortfall": sum(line["stock_shortfall"] for line in lines),
            "filters_applied": filters, "currency": "EUR", "tax_included": False,
            "subject_to_supplier": any(line["subject_to_supplier"] for line in lines),
            "kind": kind, "description": description,
        }
        if any(line.get("simulated_stock") for line in lines):
            result["simulation"] = "Simulated limited stock: part of the preferred milk is available now; substitutes and replenishment arrive after the deadline. Prices and manufacturer data remain real."
        result["option_id"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()[:24]
        return result

    async def _future_line(self, product, need, customer_id, reservations, current, simulation):
        """Ask only for the short product on bounded future dates; never invent real supply."""
        if simulation:
            delivery = (date.fromisoformat(need["deadline"]) + timedelta(days=1)).isoformat()
            line = dict(current)
            pool = f"{product['id']}|{delivery}"
            reserved = float(reservations.get(pool, reservations.get(product["id"], 0)))
            raw_available = max(current["packs"] * 2, current["packs"] + reserved)
            line.update(delivery_date=delivery, inventory_date=delivery, on_time=False,
                        raw_available=raw_available, available=max(0, raw_available - reserved),
                        reserved_at_match=reserved, stock_shortfall=0, simulated_stock=True,
                        stock_source="simulated limited-stock/restock scenario",
                        simulation_explanation="Simulated replenishment one day after the requested deadline.")
            return line
        for offset in range(1, 8):
            day = (date.fromisoformat(need["deadline"]) + timedelta(days=offset)).isoformat()
            prices, stock = await asyncio.gather(
                self.valio.prices(customer_id, day, [product["id"]]),
                self.valio.availability(customer_id, day, [product["id"]]))
            line = self._line(product, prices.get(product["id"], {}), stock.get(product["id"], {}), need, need["quantity"], reservations)
            if line and line["delivery_date"] > need["deadline"] and not cutoff_passed(line["cutoff"]) and not line["stock_shortfall"]:
                return line
        return None

    async def _shortage_options(self, preferred, products, lines, need, customer_id, reservations, filters, simulation):
        current = lines.get(preferred)
        if not current:
            return []
        lot = max(1, math.ceil(current.get("lot_size", 1)))
        # Past-cutoff inventory cannot form the on-time part of a split.
        now_packs = math.floor(current["available"] / lot) * lot if current["on_time"] else 0
        now_packs = min(now_packs, current["packs"])
        now_line = self._resize(current, now_packs) if now_packs > 0 else None
        remaining = max(0, need["quantity"] - (now_line["quantity"] if now_line else 0))
        if remaining <= 0:
            return []
        options = []
        alternatives = [line for pid, line in lines.items() if pid != preferred and line["within_preferences"] and not line["missing_properties"] and not cutoff_passed(line["cutoff"]) and (line["on_time"] or simulation)]
        alternatives.sort(key=lambda line: (
            line.get("brand") != current.get("brand"),
            abs(line["pack_size"] - current["pack_size"]),
            line["unit_price"] / line["pack_size"], line["id"]))
        mixed = [now_line] if now_line else []
        still_needed = remaining
        for candidate in alternatives:
            wanted, _ = whole_packs(still_needed, candidate["pack_size"], candidate.get("lot_size", 1))
            candidate_lot = max(1, math.ceil(candidate.get("lot_size", 1)))
            available_packs = math.floor(candidate["available"] / candidate_lot) * candidate_lot
            packs = min(wanted, available_packs)
            if packs <= 0:
                continue
            line = self._resize(candidate, packs)
            line["substitute_for"] = preferred
            mixed.append(line)
            still_needed -= line["quantity"]
            if still_needed <= 0:
                options.append(self._option(mixed, need, filters, kind="substitute_mix", soft_trade=True,
                    description="Fill the shortage with the nearest products inside the stated preferences."))
                break
        future = await self._future_line(products[preferred], need, customer_id, reservations, current, simulation)
        if future:
            if now_line:
                later_packs, _ = whole_packs(remaining, future["pack_size"], future.get("lot_size", 1))
                later = self._resize(future, later_packs)
                options.append(self._option([now_line, later], need, filters, kind="split_delivery", partial=True,
                    description="Deliver the available part on time and the remaining whole packs on the later date."))
            options.append(self._option([future], need, filters, kind="all_later",
                description="Deliver the full quantity on the earliest later date with available stock."))
        if not options:
            options.append(self._option([current], need, filters, kind="shortage_unresolved",
                description="The stock cannot cover this request; no confirmed later supply is available."))
        if cutoff_passed(current.get("cutoff")):
            later_days = sorted({
                line["delivery_date"] for option in options for line in option["lines"]
                if line["delivery_date"] > need["deadline"]
                and not cutoff_passed(line.get("cutoff"))
                and not line.get("stock_shortfall")
            })
            for option in options:
                option["requested_cutoff_passed"] = True
                if later_days:
                    option["earliest_orderable_date"] = later_days[0]
        return options

    async def match(self, need: dict, customer_id: str, reservations=None, confirmations=None, must_hold=None, must_hold_categories=None) -> dict:
        need = dict(need)
        required = [normalize_property(p) for p in need.get("properties", [])]
        if must_hold_categories is not None:
            need["_must_hold"] = [normalize_property(prop) for prop in (must_hold or [])]
            need["_must_hold_categories"] = must_hold_categories
        selected_id = str(need.get("product_id") or "") or None
        selected_product = self.catalog.get(selected_id) if selected_id else None
        product_type = selected_product.get("product_type") if selected_product else need.get("product_type", "drinking milk")
        quantity = float(need.get("quantity", 0))
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("Requested quantity must be a positive finite number.")
        unit = str(need.get("unit", "L")).upper()
        unit = {"LITRES": "L", "LITERS": "L", "LITRE": "L", "KILOGRAMS": "KG"}.get(unit, unit)
        deadline = str(need["deadline"])
        date.fromisoformat(deadline)
        need.update(product_type=product_type, properties=required, quantity=quantity, unit=unit, deadline=deadline)
        filters = [f"Product type: {product_type} (inferred)", *[f"Required property: {p}; manufacturer statement or exact buyer confirmation required" for p in required],
                   f"Quantity: {quantity:g} {unit}, rounded up to whole sales packs", f"Delivery deadline: {deadline}", "Order cutoff has not passed for an on-time option"]
        all_products = self.catalog.candidates(product_type, [])
        if selected_product and all(product["id"] != selected_id for product in all_products):
            all_products.append(selected_product)
        product_by_id = {product["id"]: product for product in all_products}
        exact_ids = {str(pid) for pid in need.get("product_ids", [])}
        if selected_id:
            exact_ids.add(selected_id)
        preferred = selected_id or (str(need["preferred_product_id"]) if need.get("preferred_product_id") else None)
        confirmations = confirmations or {}
        selection = None
        if selected_id:
            missing = []
            if selected_product:
                accepted = confirmations.get(selected_id, [])
                contradicted = selected_product.get("contradicted_properties", {})
                stated = selected_product.get("properties", {})
                selected_required = self._required_properties(need, selected_product)
                missing = [prop for prop in selected_required if stated.get(prop) != "manufacturer" and (prop not in accepted or prop in contradicted)]
            if not selected_product:
                message = f"The selected product {selected_id} is not present in the catalogue."
            elif missing:
                message = f"The selected product {selected_product.get('name', selected_id)} does not satisfy the required properties: {', '.join(missing)}."
            else:
                message = f"The buyer selected this exact product: {selected_product.get('name', selected_id)}; all required properties are satisfied."
            selection = {"product_id": selected_id, "name": selected_product.get("name") if selected_product else None, "satisfies_properties": bool(selected_product and not missing), "missing_properties": missing, "message": message}
            filters.append(message)
        products = []
        for product in all_products:
            pid = product["id"]
            stated = product.get("properties", {})
            accepted = confirmations.get(pid, [])
            contradicted = product.get("contradicted_properties", {})
            product_required = self._required_properties(need, product)
            satisfies = all(stated.get(prop) == "manufacturer" or (prop in accepted and prop not in contradicted) for prop in product_required)
            if satisfies or pid in exact_ids or pid == preferred:
                product = dict(product)
                product["property_sources"] = {prop: "manufacturer" if stated.get(prop) == "manufacturer" else "buyer_confirmation" for prop in product_required if stated.get(prop) == "manufacturer" or (prop in accepted and prop not in contradicted)}
                products.append(product)
        if exact_ids:
            products = [p for p in products if p["id"] in exact_ids]
            filters.append("Exact product IDs requested: " + ", ".join(sorted(exact_ids)) + "; missing claims require sign-off")
        if preferred:
            filters.append("Preferred product: " + preferred)
        if need.get("category"):
            products = [p for p in products if p["category"] == str(need["category"])]
            filters.append("Category: " + str(need["category"]))
        ids = [p["id"] for p in products]
        prices, stock = await asyncio.gather(self.valio.prices(customer_id, deadline, ids), self.valio.availability(customer_id, deadline, ids))
        reserved = reservations or {}
        lines = {product["id"]: self._line(product, prices.get(product["id"], {}), stock.get(product["id"], {}), need, quantity, reserved) for product in products}
        unavailable = [pid for pid, line in lines.items() if line is None]
        lines = {pid: line for pid, line in lines.items() if line is not None}
        simulation = bool(need.get("simulate_limited_stock"))
        if simulation and lines:
            on_time = [line for line in lines.values() if line["on_time"] and line["available"] > 0]
            primary = min(on_time or list(lines.values()), key=lambda line: line["unit_price"] / line["pack_size"])
            preferred = preferred if preferred in lines else primary["id"]
            primary = lines[preferred]
            later_day = (date.fromisoformat(deadline) + timedelta(days=1)).isoformat()
            for pid, line in list(lines.items()):
                inventory = dict(stock[pid])
                inventory.update(simulated_stock=True, source="simulated limited-stock/restock scenario",
                    simulation_explanation="Simulated finite stock now; substitutes and replenishment arrive one day after the deadline.")
                if pid == preferred:
                    remaining_cap = max(1, math.floor(primary["packs"] * .6))
                    inventory["available"] = min(float(inventory.get("available", 0)), primary["reserved_at_match"] + remaining_cap)
                else:
                    inventory["delivery_date"] = later_day
                lines[pid] = self._line(product_by_id[pid], prices[pid], inventory, need, quantity, reserved)
            filters.append("SIMULATED limited stock and later replenishment; manufacturer data and upstream prices unchanged")
        options = [self._option([line], need, filters) for line in lines.values()]
        if preferred in lines and (lines[preferred]["stock_shortfall"] or not lines[preferred]["on_time"]):
            options = await self._shortage_options(preferred, product_by_id, lines, need, customer_id, reserved, filters, simulation)
        elif options and not any(option["on_time"] for option in options):
            shortage = min(lines.values(), key=lambda line: line["stock_shortfall"])
            alternatives = await self._shortage_options(shortage["id"], product_by_id, lines, need, customer_id, reserved, filters, simulation)
            if alternatives:
                options = alternatives
        elif preferred in lines:
            preferred_option = next(option for option in options if option["lines"][0]["id"] == preferred)
            options = [preferred_option]
        ranked = rank_options(options)[:3]
        if self.decision_labeler is not None:
            accepted_options, inferred_by_id = [], {}
            for option in ranked:
                accepted = True
                for line in option["lines"]:
                    if line["id"] not in inferred_by_id:
                        # The bounded decision task distinguishes milk types; it
                        # does not classify arbitrary restaurant staples.
                        if line["product_type"] in {"drinking milk", "sour milk", "coffee milk", "milk powder", "cream"} and len(inferred_by_id) < 3:
                            inferred_by_id[line["id"]] = await self.decision_labeler.classify(line)
                        else:
                            inferred_by_id[line["id"]] = {"label": line["product_type"], "method": "rules", "probabilities": None}
                    inferred = inferred_by_id[line["id"]]
                    if inferred["label"] != line["product_type"]:
                        accepted = False
                    line["inference_method"] = inferred.get("method", "rules")
                    line["probabilities"] = inferred.get("probabilities")
                if accepted:
                    option["option_id"] = hashlib.sha256(json.dumps(option, sort_keys=True).encode()).hexdigest()[:24]
                    accepted_options.append(option)
            ranked = accepted_options
        for label, option in zip(("best", "better", "good"), ranked):
            option["label"] = label
        later_days = sorted({
            line["delivery_date"] for option in ranked for line in option["lines"]
            if line["delivery_date"] > deadline
            and not cutoff_passed(line.get("cutoff"))
            and not line.get("stock_shortfall")
        })
        earliest = later_days[0] if later_days else None
        for option in ranked:
            if option.get("requested_cutoff_passed") or option["delivery_date"] > deadline:
                option["earliest_orderable_date"] = earliest
        public_need = {key: value for key, value in need.items() if not key.startswith("_must_hold")}
        return {"options": ranked, "filters_applied": filters, "need": public_need, "product_selection": selection,
                "unavailable_product_ids": unavailable,
                "earliest_orderable_date": earliest,
                "data_source": "synthetic fixture" if self.valio.fixture else "Valio mock API"}
