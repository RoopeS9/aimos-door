"""Slim, streamed catalog. Manufacturer claims and inferred types stay separate."""
from __future__ import annotations

import csv
import json
import re
from collections import Counter
import sqlite3
from pathlib import Path
from typing import Any

import ijson

from door.staples import RESTAURANT_STAPLES

CATALOG_INDEX_VERSION = "2"

CLAIM_ALIASES = {
    "FREE_FROM_LACTOSE": "lactose-free",
    "FREE_FROM_GLUTEN": "gluten-free",
    # Actual manufacturer allergen codes: milk/lactose and cereals with gluten.
    "ML": "lactose-free",
    "AW": "gluten-free",
    "LACTOSE": "lactose-free",
    "GLUTEN": "gluten-free",
}


def normalize_property(value: str) -> str:
    return CLAIM_ALIASES.get(value.upper(), value.lower().replace("_", "-").replace(" ", "-"))


def infer_product_type(names: dict[str, str], category: str) -> str:
    """Conservative fallback: never infer an allergen or nutritional claim."""
    name = " ".join(names.values()).casefold()
    if category in {"10400", "10405"}:
        if any(x in name for x in ("powder", "jauhe", "pulver")):
            return "milk powder"
        if any(x in name for x in ("sour", "piimä", "buttermilk", "surmjölk")):
            return "sour milk"
        if any(x in name for x in ("coffee", "kahvi", "latte", "café", "cafè", "cafe", "barista", "annosmaito", "portion milk")):
            return "coffee milk"
        if any(x in name for x in ("cream", "kerma", "grädde")):
            return "cream"
        if any(x in name for x in ("soygurt", "yoghurt", "jogurt", "yogurt")):
            return "yogurt"
        if any(x in name for x in ("milk", "maito", "mjölk")):
            return "drinking milk"
    if any(x in name for x in ("rice", "riisi")):
        return "rice"
    if any(x in name for x in ("butter", " voi", "voi ")):
        return "butter"
    if any(x in name for x in ("flour", "jauho")):
        return "flour"
    if any(x in name for x in ("pasta", "spaghetti")):
        return "pasta"
    return "other"


def normalize_product(raw: dict[str, Any], reliability: float | None = None, carbon: float | None = None) -> dict:
    synkka = raw.get("synkkaData") or {}
    names = {n["language"]: n["value"] for n in synkka.get("names", []) if n.get("value")}
    properties: dict[str, str] = {}
    contradicted_properties: dict[str, str] = {}
    labels = []
    for group in synkka.get("classifications", []):
        for value in group.get("values", []):
            claim = str(value.get("id", ""))
            unit = str(value.get("unit", "")).upper()
            if group.get("name") == "allergen" and unit in {"CONTAINS", "MAY_CONTAIN"} and claim.upper() in {"ML", "AW"}:
                contradicted_properties[normalize_property(claim)] = "manufacturer"
            if group.get("name") == "nutritionalClaim" and claim.startswith("FREE_FROM_"):
                properties[normalize_property(claim)] = "manufacturer"
            elif group.get("name") in {"allergen", "nonAllergen"} and unit == "FREE_FROM":
                properties[normalize_property(claim)] = "manufacturer"
            elif group.get("name") == "packagingMarkedLabel":
                labels.append({"id": claim, "source": "manufacturer"})
    # Contrary manufacturer evidence is authoritative even if a conflicting
    # nutritional or free-from claim is present in the same source record.
    for prop in contradicted_properties:
        properties.pop(prop, None)
    category = str(raw.get("category", ""))
    return {
        "id": str(raw["id"]), "gtin": raw.get("salesUnitGtin"), "names": names,
        "name": names.get("en") or names.get("fi") or str(raw["id"]),
        "category": category, "brand": synkka.get("brand", ""),
        "properties": properties, "contradicted_properties": contradicted_properties, "product_type": infer_product_type(names, category),
        "product_type_source": "inferred", "product_type_method": "rules",
        "origin": {"value": raw.get("countryOfOrigin") or synkka.get("countriesOfOrigin", []), "source": "manufacturer"},
        "labels": labels, "sales_unit": raw.get("salesUnit", "ST"),
        "lot_size": float(raw.get("allowedLotSize") or 1),
        "storage_class": raw.get("temperatureCondition"),
        "sales_unit_multiplier": next((float(u["sizeInBaseUnits"]) for u in raw.get("units", []) if u["unitId"] == raw.get("salesUnit")), 1.0),
        "delivery_reliability": reliability, "carbon_kg_per_kg": carbon,
    }


def _csv_values(path: str | Path | None) -> dict[str, float]:
    if not path or not Path(path).exists():
        return {}
    values = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            product_id = row.get("productId")
            if not product_id:
                continue
            for key, value in row.items():
                if key != "productId":
                    try:
                        values[product_id] = float(value)
                    except (TypeError, ValueError):
                        pass
    return values


class Catalog:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS catalog (
                    id TEXT PRIMARY KEY, category TEXT NOT NULL, product_type TEXT NOT NULL,
                    search_text TEXT NOT NULL, storage_class TEXT, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS catalog_category ON catalog(category);
                CREATE INDEX IF NOT EXISTS catalog_type ON catalog(product_type);
                CREATE TABLE IF NOT EXISTS type_labels (
                    product_id TEXT PRIMARY KEY, label TEXT NOT NULL,
                    method TEXT NOT NULL, probabilities TEXT, attempted INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS catalog_metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
            """)

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def index_version(self) -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM catalog_metadata WHERE key='index_version'").fetchone()
        return row[0] if row else "legacy"

    def needs_rebuild(self) -> bool:
        return self.index_version() != CATALOG_INDEX_VERSION

    @staticmethod
    def _mark_current(db):
        db.execute(
            "INSERT OR REPLACE INTO catalog_metadata(key,value) VALUES('index_version',?)",
            (CATALOG_INDEX_VERSION,),
        )

    def build(self, source_path: str | Path, reliability_path=None, carbon_path=None) -> int:
        reliability, carbon = _csv_values(reliability_path), _csv_values(carbon_path)
        count = 0
        with self.connect() as db, Path(source_path).open("rb") as stream:
            db.execute("DELETE FROM catalog")
            for raw in ijson.items(stream, "item", use_float=True):
                product = normalize_product(raw, reliability.get(str(raw["id"])), carbon.get(str(raw["id"])))
                previous = db.execute("SELECT label,method,probabilities FROM type_labels WHERE product_id=?", (product["id"],)).fetchone()
                if previous:
                    product["product_type"], product["product_type_method"] = previous["label"], previous["method"]
                    if previous["probabilities"]:
                        product["type_probabilities"] = json.loads(previous["probabilities"])
                self._insert(db, product)
                db.execute("INSERT OR IGNORE INTO type_labels(product_id,label,method) VALUES (?,?,?)", (product["id"], product["product_type"], "rules"))
                count += 1
            self._mark_current(db)
        return count

    @staticmethod
    def _insert(db, product):
        text = " ".join([product["id"], product.get("gtin") or "", *product["names"].values(), product.get("brand", "")]).casefold()
        db.execute("INSERT OR REPLACE INTO catalog VALUES (?,?,?,?,?,?)", (
            product["id"], product["category"], product["product_type"], text,
            str(product.get("storage_class")), json.dumps(product, ensure_ascii=False),
        ))

    def upsert(self, product: dict):
        """Accept a normalized product, useful for isolated test fixtures."""
        with self.connect() as db:
            self._insert(db, product)

    def count(self) -> int:
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM catalog").fetchone()[0]

    def get(self, product_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT data FROM catalog WHERE id=?", (str(product_id),)).fetchone()
        return json.loads(row[0]) if row else None

    def search(self, text="", category=None, properties=None, storage_class=None, limit=50) -> list[dict]:
        clauses, params = [], []
        for word in str(text).casefold().split():
            clauses.append("search_text LIKE ? ESCAPE '\\'")
            params.append("%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if category:
            clauses.append("category=?")
            params.append(str(category))
        if storage_class is not None:
            clauses.append("storage_class=?")
            params.append(str(storage_class))
        query = "SELECT data FROM catalog" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY id"
        result = []
        required = [normalize_property(x) for x in (properties or [])]
        with self.connect() as db:
            for row in db.execute(query, params):
                product = json.loads(row[0])
                if all(product["properties"].get(p) == "manufacturer" for p in required):
                    result.append(product)
                    if len(result) >= min(max(int(limit), 1), 1000):
                        break
        return result

    def category_sizes(self) -> dict[str, int]:
        """Number of products per category, read once."""
        sizes = getattr(self, "_category_sizes", None)
        if sizes is None:
            with sqlite3.connect(self.db_path) as db:
                sizes = {str(category): int(count) for category, count in db.execute("SELECT category, COUNT(*) FROM catalog GROUP BY category")}
            self._category_sizes = sizes
        return sizes

    def search_relevant(self, text: str, properties=None, limit: int = 6) -> list[dict]:
        """Type-first browsing with a product-name fallback and stable relevance."""
        required = [normalize_property(value) for value in (properties or [])]
        folded = str(text).casefold()
        kind = None
        # Plant-based "milks" are sold as drinks: "oat milk" means the oat drinks, not
        # dairy milk and not milk chocolate.
        plant = r"(?:oat|soy|soya|almond|rice|coconut|pea|hazelnut|cashew|kaura|soija|manteli|riisi|kookos)"
        folded = re.sub(rf"\b({plant})[ -]?(?:milks?|maito\w*)\b", r"\1 drink", folded)
        if re.search(r"\b(?:milks?|maito\w*|mjölk)\b", folded):
            kind = "drinking milk"
        elif re.search(r"\b(?:butter|voi)\b", folded):
            kind = "butter"
        elif re.search(r"\b(?:rice|riisi)\b", folded):
            kind = "rice"
        elif re.search(r"\b(?:flour|jauho)\b", folded):
            kind = "flour"
        elif re.search(r"\b(?:pasta|spaghetti)\b", folded):
            kind = "pasta"

        def eligible(products):
            return [product for product in products if all(product.get("properties", {}).get(prop) == "manufacturer" for prop in required)]

        if kind:
            typed = eligible(self.candidates(kind, []))
        else:
            typed = []
        query = folded
        query = re.sub(r"\b(?:lactose[ -]free|gluten[ -]free|laktoositon\w*|gluteeniton\w*)\b", " ", query)
        query = re.sub(r"\b(?:what|which|do|does|you|your|have|sell|price|prices|cost|please|some|the|a|an|of|for|show|me|i|can|get|buy|order)\b", " ", query)
        words = [word for word in re.findall(r"[\wäöåÄÖÅ-]+", query) if word]
        phrase = " ".join(words).strip()

        def ranked(products):
            def score(product):
                name = " ".join(product.get("names", {}).values()).casefold()
                word_hits = sum(bool(re.search(rf"\b{re.escape(word)}\b", name)) for word in words)
                substring_hits = sum(word in name for word in words)
                phrase_hit = bool(phrase and phrase in name)
                return (not phrase_hit, -word_hits, -substring_hits, name.find(phrase) if phrase_hit else 10000, len(name), product["id"])
            # A word such as "butter" also appears in croissants, dishes and spreaders.
            # The category in which the largest share of products carries the words is
            # the one the buyer means, so it leads; close categories take turns.
            if not products:
                return []
            # "Wine" in "red wine vinegar" or "cream" in "ice cream" names another product.
            # Count a product only when the words are its head: followed by a size, a
            # comma or the end of the name, not by another noun.
            # For several words the last one is the noun ("red wine", "chicken breast"): it
            # decides the shelf, and the other words narrow the choice on that shelf.
            noun = words[-1] if words else ""
            # "Cream" must not mean ice cream, unless the buyer asked for ice cream.
            not_compound = r"(?<!\bice )" if noun == "cream" and "ice" not in words else ""
            head = re.compile(rf"{not_compound}{re.escape(noun)}s?(?=\s*(?:$|[\d,(]|-\s*\d))") if noun else None
            def is_head(product):
                return bool(head) and any(head.search(str(name).casefold()) for name in product.get("names", {}).values())
            shelf = products if len(words) < 2 or kind else (eligible(self.search(text=noun, properties=[], limit=2000)) or products)
            hits: Counter = Counter()
            for product in shelf:
                names = " ".join(str(name) for name in product.get("names", {}).values()).casefold()
                if is_head(product):
                    hits[str(product.get("category", ""))] += 1
                elif noun and noun in names:
                    hits[str(product.get("category", ""))] += 0.2
            sizes = self.category_sizes()
            # Share of the category that matches, weighted by how many products match, so a
            # three-product sweets category cannot outrank the milk shelf.
            share = {category: count * count / max(sizes.get(category, count), 1) for category, count in hits.items()}
            ordered = sorted(products, key=lambda product: (-round(share.get(str(product.get("category", "")), 0), 3), not is_head(product), score(product)))
            top = share.get(str(ordered[0].get("category", "")), 0)
            by_category: dict[str, list] = {}
            for product in ordered:
                by_category.setdefault(str(product.get("category", "")), []).append(product)
            lead = [rows for category, rows in by_category.items() if top and share.get(category, 0) >= top * 0.6]
            if not lead:
                return ordered
            mixed = [rows[index] for index in range(max(map(len, lead))) for rows in lead if index < len(rows)]
            chosen = {product["id"] for product in mixed}
            return mixed + [product for product in ordered if product["id"] not in chosen]

        products = ranked(typed) if kind else []
        if not products and phrase:
            products = ranked(eligible(self.search(text=phrase, properties=[], limit=1000)))
            if not products and len(words) > 1:
                # A descriptive adjective should not hide an otherwise matching
                # product name. Try the final noun-like word without weakening claims.
                products = ranked(eligible(self.search(text=words[-1], properties=[], limit=1000)))
        return products[:min(max(int(limit), 1), 100)]

    def candidates(self, product_type: str, properties=None) -> list[dict]:
        kind = product_type.lower().replace("_", "-").replace("-", " ")
        if kind in {"milk", "maito"}:
            kind = "drinking milk"
        required = [normalize_property(x) for x in (properties or [])]
        with self.connect() as db:
            rows = db.execute("SELECT data FROM catalog WHERE product_type=? ORDER BY id", (kind,)).fetchall()
        products = [json.loads(r[0]) for r in rows]
        return [p for p in products if all(p["properties"].get(x) == "manufacturer" for x in required)]

    def seed_fixture(self, conversation: bool = False):
        products = [
            ("milk-175", "Fixture lactose-free milk 1.75 L", "10400", True, "drinking milk"),
            ("milk-10", "Fixture lactose-free milk 10 L", "10400", True, "drinking milk"),
            ("milk-12", "Fixture lactose-free milk 12 x 1 L", "10405", True, "drinking milk"),
            ("milk-unknown", "Fixture milk with no claim", "10400", False, "drinking milk"),
            ("rice", "Fixture rice 1 KG", "30500", False, "rice"),
        ]
        # These fixture staples mirror the real products' lactose-free statements.
        lactose_free_staples = {"464194", "462421", "463400"}
        products.extend((staple["id"], staple["name"], staple["category"], staple["id"] in lactose_free_staples, staple["product_type"]) for staple in RESTAURANT_STAPLES)
        if conversation:
            products.append(("oat-drink", "Fixture oat drink 1 L", "10402", True, "other"))
        products.append(("cleaning-spray", "Fixture non-food cleaning spray 1 L", "63100", False, "other"))
        for product_id, name, category, claim, kind in products:
            product = {
                "id": product_id, "gtin": "0000000000000", "names": {"en": name}, "name": name,
                "category": category, "brand": "Fixture", "properties": {"lactose-free": "manufacturer"} if claim else {},
                "product_type": kind, "product_type_source": "inferred", "product_type_method": "rules",
                "origin": {"value": "fi", "source": "manufacturer"}, "labels": [{"id": "FIXTURE", "source": "manufacturer"}],
                "sales_unit": "ST", "lot_size": 1, "storage_class": 1, "synthetic": True,
            }
            self.upsert(product)
        with self.connect() as db:
            self._mark_current(db)
