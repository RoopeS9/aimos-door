"""Deterministic restaurant demo history, explicitly synthetic at every layer.

Only the current catalog and current quote are real in a live seed. We do not
claim that the mock wholesaler has an order-history API or historic prices.
The caller persists this seed once and restores that immutable copy on reset.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from door.matching import Matcher, whole_packs
from door.staples import RESTAURANT_SEED_VERSION, RESTAURANT_STAPLES

LABEL = "Synthetic order history: 12 weekly orders; simulated buyer-confirmed usual basket."


class HistorySeedError(RuntimeError):
    """The real catalog cannot currently support the promised demo basket."""


def _calendar(as_of: date | None = None) -> tuple[date, date]:
    today = as_of or datetime.now(ZoneInfo("Europe/Helsinki")).date()
    # "Last week's order" is the Monday strictly before today. The new order
    # uses the next Monday, even when the application starts on a Monday.
    last_monday = today - timedelta(days=today.weekday() or 7)
    next_monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    return last_monday, next_monday


def _seed_line(product, price, stock, kind, deadline, unit, target_packs):
    required = ["lactose-free"] if kind == "drinking milk" else []
    need = {"quantity": 5, "unit": unit, "deadline": deadline, "properties": required, "preferences": {}}
    line = Matcher._line(product, price, stock, need, 5, {})
    if not line or not line["on_time"] or line["subject_to_supplier"] or line["missing_properties"]:
        return None
    lot = max(1, math.ceil(product.get("lot_size", 1)))
    if kind == "drinking milk":
        increment_packs, _ = whole_packs(5, line["pack_size"], lot)
        packs = 5 * increment_packs
        # Five litres more rounds to one fifth of this anchored quantity.
        line["demo_milk_increment_packs"] = increment_packs
    else:
        packs = target_packs * lot
    line = Matcher._resize(line, packs)
    # Leave room for the demo's milk increment and several clean approval
    # examples; a low-stock seed would turn an autonomy demo into a stock hold.
    if line["available"] < packs * 6:
        return None
    line["required_properties"] = required
    line["usual_anchor"] = True
    line["synthetic_order_pattern"] = True
    return line


async def build_history(catalog, valio, *, as_of: date | None = None) -> dict:
    """Make a frozen seed from a narrowed current quote, without model calls.

    Fixture Valio clients stay offline. Live clients retain their normal cache,
    cutoff and error behavior; no synthetic price or stock replaces a failure.
    """
    last_monday, next_monday = _calendar(as_of)
    deadline = next_monday.isoformat()
    groups = [("drinking milk", catalog.candidates("drinking milk", ["lactose-free"])[:40], "L", 0)]
    for staple in RESTAURANT_STAPLES:
        product = catalog.get(staple["id"])
        groups.append((staple["key"], [product] if product else [], staple["unit"], staple["packs"]))
    products = {product["id"]: product for _, group, _, _ in groups for product in group}
    if not groups[0][1] or any(not group for _, group, _, _ in groups[1:]):
        raise HistorySeedError("The real catalog must contain lactose-free drinking milk and every fixed restaurant staple.")
    prices, stock = await asyncio.gather(
        valio.prices("restaurant", deadline, list(products)),
        valio.availability("restaurant", deadline, list(products)),
    )
    lines = []
    for kind, group, unit, target_packs in groups:
        candidates = []
        for product in group:
            line = _seed_line(product, prices.get(product["id"], {}), stock.get(product["id"], {}), kind, deadline, unit, target_packs)
            if line:
                candidates.append(line)
        if not candidates:
            raise HistorySeedError(f"No on-time {kind} with sufficient confirmed stock is available for the restaurant seed.")
        candidates.sort(key=lambda line: (line["pack_size"], line["unit_price"] / line["pack_size"], line["id"]))
        lines.append(candidates[0])
    if not 12 <= len(lines) <= 15 or len({line["id"] for line in lines}) != len(lines) or len({line["category"] for line in lines}) < 5 or sum(line["product_type"] == "drinking milk" for line in lines) != 1:
        raise HistorySeedError("The restaurant seed must contain 12–15 distinct staples in at least five categories and exactly one drinking-milk line.")
    basket = [{key: line.get(key) for key in (
        "id", "product_id", "gtin", "name", "names", "category", "product_type", "properties", "required_properties",
        "packs", "pack_size", "quantity", "unit", "unit_price", "total", "origin", "labels")}
        for line in lines]
    anchor_id = "restaurant-usual-" + hashlib.sha256(json.dumps(basket, sort_keys=True).encode()).hexdigest()[:16]
    history = []
    for week in range(12):
        day = (last_monday - timedelta(weeks=11 - week)).isoformat()
        history.append({
            "id": f"restaurant-synthetic-{day}", "customer_id": "restaurant", "date": day,
            "delivery_date": day, "lines": copy.deepcopy(basket),
            "total": round(sum(line["total"] for line in basket), 2), "currency": "EUR",
            "synthetic": True, "label": "Synthetic weekly order; current quoted prices reused as a demo assumption.",
            "status": "committed", "approved_by": "simulated restaurant buyer", "clean_approval": True,
            "historical_price_source": "simulated from the seed's current quote",
        })
    return {
        "seed_version": RESTAURANT_SEED_VERSION,
        "customer_id": "restaurant", "lines": lines, "history": history,
        "synthetic": True, "label": LABEL, "history_label": LABEL,
        "anchor_confirmed": True, "anchor_id": anchor_id,
        "anchor": {"id": anchor_id, "lines": copy.deepcopy(lines), "confirmed": True,
                   "confirmed_by": "simulated restaurant buyer", "confirmed_date": last_monday.isoformat(), "synthetic": True},
        "deadline": deadline, "total": round(sum(line["total"] for line in lines), 2), "currency": "EUR",
        "allowed_categories": sorted({line["category"] for line in lines}),
        "history_source": "deterministic demo simulation; no real order-history endpoint exists",
        "catalog_source": "synthetic fixture" if valio.fixture else "real event-licensed catalog",
        "current_quote_source": "synthetic fixture" if valio.fixture else "Valio mock API",
        "proposal_evidence": [{"order_id": entry["id"], "date": entry["date"], "outcome": "clean approval", "synthetic": True} for entry in history[-4:]],
    }
