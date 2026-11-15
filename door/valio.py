"""Narrow, cached calls to Valio's mock API; no prices leak through catalog."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from door.staples import RESTAURANT_STAPLES

BASE_URL = "https://junction26.valioaimo.fi"


class UpstreamError(RuntimeError):
    pass


class UsageLimitError(UpstreamError):
    pass


class ValioClient:
    def __init__(self, data_dir: str | Path, fixture: bool = False):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.fixture = fixture
        self.db_path = self.data_dir / "upstream-cache.sqlite3"
        self.calls = 0
        self.fixture_prices: dict[str, dict] = {}
        self.fixture_availability: dict[str, dict] = {}
        with sqlite3.connect(self.db_path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS responses (kind TEXT, customer TEXT, day TEXT, product TEXT, expires REAL, data TEXT, PRIMARY KEY(kind,customer,day,product))")

    @staticmethod
    def _price(raw: dict) -> dict:
        return {
            "unit_price": (raw.get("catalogPrice") or {}).get("value"),
            "unit_price_with_tax": (raw.get("catalogPrice") or {}).get("valueWithTax"),
            "comparison_price": (raw.get("comparisonPrice") or {}).get("value"),
            "comparison_unit": raw.get("comparisonUnit"),
            "tax": raw.get("tax"), "error": raw.get("error", False),
            "source": "Valio mock API",
        }

    @staticmethod
    def _availability(raw: dict) -> dict:
        schedule = sorted(raw.get("scheduleLines", []), key=lambda x: x.get("availableDate", "9999-12-31"))
        valid = [x for x in schedule if not x.get("salesBlock")]
        first = valid[0] if valid else {}
        return {
            "available": first.get("availableAmount", 0),
            "delivery_date": first.get("availableDate"), "cutoff": raw.get("closingTime"),
            "sales_block": bool(raw.get("salesBlock")) or not valid,
            "logistic_model": raw.get("logisticModel"),
            "subject_to_supplier": raw.get("logisticModel") == "Terminal",
            "deliverable": bool(valid) and not raw.get("salesBlock"),
            "source": "Valio mock API",
        }

    async def _get(self, kind: str, customer_id: str, day: str, ids: list[str]) -> dict:
        if len(ids) > 1000:
            raise ValueError("Only narrowed candidate lists of up to 1,000 products are allowed.")
        if not ids:
            return {}
        if self.fixture:
            return self._fixtures(kind, day, ids)
        now = time.time()
        result, missing = {}, []
        with sqlite3.connect(self.db_path) as db:
            for product_id in ids:
                row = db.execute("SELECT data FROM responses WHERE kind=? AND customer=? AND day=? AND product=? AND expires>?", (kind, customer_id, day, product_id, now)).fetchone()
                if row:
                    result[product_id] = json.loads(row[0])
                else:
                    missing.append(product_id)
        if not missing:
            return result
        load_dotenv()
        key = os.getenv("VALIO_API_KEY")
        if not key:
            raise UpstreamError("VALIO_API_KEY is required for live prices and stock.")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(f"{BASE_URL}/api/{kind}",
                    headers={"X-API-Key": key, "User-Agent": "AimosDoor/0.1 Junction2026"},
                    json={"customer": customer_id, "requestedDate": day, "products": missing})
            self.calls += 1
        except httpx.HTTPError:
            raise UpstreamError("The Valio mock API is unreachable; no synthetic live response was substituted.") from None
        if response.status_code in (402, 429):
            raise UsageLimitError(f"Valio usage or credit limit: HTTP {response.status_code}. Stop and update STATUS.md.")
        if response.status_code != 200:
            raise UpstreamError(f"The Valio mock API returned HTTP {response.status_code}.")
        try:
            body = response.json()
            records = body.get("products", []) if isinstance(body, dict) else body
            normalized = {str(p["productId"]): self._price(p) if kind == "price" else self._availability(p) for p in records}
        except (ValueError, TypeError, KeyError):
            raise UpstreamError("The Valio mock API response had an unexpected shape.") from None
        with sqlite3.connect(self.db_path) as db:
            for product_id, value in normalized.items():
                db.execute("INSERT OR REPLACE INTO responses VALUES (?,?,?,?,?,?)", (kind, customer_id, day, product_id, now + 300, json.dumps(value)))
        result.update(normalized)
        return result

    def _fixtures(self, kind, day, ids):
        result = {}
        sizes = {"milk-175": 1.75, "milk-10": 10, "milk-12": 12, "milk-unknown": 1, "rice": 1, "oat-drink": 1}
        staples = {staple["id"]: staple for staple in RESTAURANT_STAPLES}
        for product_id in ids:
            if kind == "price":
                staple = staples.get(product_id, {})
                size = sizes.get(product_id, staple.get("pack_size", 1))
                unit = "L" if product_id.startswith("milk") or product_id == "oat-drink" or staple.get("unit") == "L" else "KG"
                result[product_id] = self.fixture_prices.get(product_id, {
                    "unit_price": round(size * 1.2, 2), "unit_price_with_tax": round(size * 1.2 * 1.135, 2),
                    "comparison_price": 1.2, "comparison_unit": unit,
                    "tax": {"taxRate": 13.5}, "error": False, "source": "synthetic fixture",
                }).copy()
            else:
                result[product_id] = self.fixture_availability.get((product_id, day), self.fixture_availability.get(product_id, {
                    "available": 500 if product_id == "milk-175" and day <= self._fixture_initial_date() else 10000,
                    "delivery_date": day, "cutoff": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                    "sales_block": False, "logistic_model": "Stock", "subject_to_supplier": False,
                    "deliverable": True, "source": "synthetic fixture; milk-175 replenishes after the initial Monday",
                })).copy()
        return result

    @staticmethod
    def _fixture_initial_date():
        today = datetime.now(timezone.utc).date()
        return (today + timedelta(days=(7 - today.weekday()) % 7 or 7)).isoformat()

    async def prices(self, customer_id: str, date: str, ids: list[str]):
        return await self._get("price", customer_id, date, [str(x) for x in ids])

    async def availability(self, customer_id: str, date: str, ids: list[str]):
        return await self._get("availability", customer_id, date, [str(x) for x in ids])

    def invalidate(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("DELETE FROM responses")
