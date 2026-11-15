import json
from door.catalog import CATALOG_INDEX_VERSION, Catalog, infer_product_type, normalize_product


def test_manufacturer_claims_only(tmp_path):
    raw = {"id": "test", "category": "10400", "synkkaData": {"names": [{"language": "en", "value": "Lactose free milk 1 L"}], "classifications": []}}
    product = normalize_product(raw)
    assert "lactose-free" not in product["properties"]
    assert product["product_type"] == "drinking milk"
    raw["synkkaData"]["classifications"] = [{"name": "nutritionalClaim", "values": [{"id": "FREE_FROM_LACTOSE"}]}]
    assert normalize_product(raw)["properties"]["lactose-free"] == "manufacturer"


def test_milk_type_exclusions():
    for text, expected in [("Coffee milk", "coffee milk"), ("Sour milk", "sour milk"), ("Milk powder", "milk powder"), ("Cream powder", "milk powder"), ("Valio kahvimaito", "coffee milk")]:
        assert infer_product_type({"en": text}, "10400") == expected


def test_fixture_search_has_no_price_or_stock(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.seed_fixture()
    assert catalog.count() == 18
    claimed = catalog.search(properties=["lactose-free"])
    assert len(claimed) == 6
    assert sum(product["product_type"] == "drinking milk" for product in claimed) == 3
    assert all("price" not in str(p.keys()) and "stock" not in str(p.keys()) for p in claimed)
    assert catalog.get("milk-10")["names"]["en"]
    cleaning = catalog.get("cleaning-spray")
    assert cleaning["category"] == "63100" and "lactose-free" not in cleaning["properties"]


def test_streamed_build(tmp_path):
    source = tmp_path / "source.json"
    source.write_text(json.dumps([{"id": "1", "category": "10400", "salesUnit": "ST", "synkkaData": {"names": [{"language": "en", "value": "milk 1L"}]}}]))
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    assert catalog.build(source) == 1
    assert catalog.count() == 1
    assert catalog.search(text="milk")[0]["id"] == "1"
    assert catalog.index_version() == CATALOG_INDEX_VERSION and not catalog.needs_rebuild()
    with catalog.connect() as db:
        db.execute("UPDATE catalog_metadata SET value='legacy' WHERE key='index_version'")
    assert catalog.needs_rebuild()
    catalog.build(source)
    assert catalog.index_version() == CATALOG_INDEX_VERSION and not catalog.needs_rebuild()


def test_explicit_contradiction_does_not_confuse_milk_protein_with_lactose():
    raw = {"id": "test", "category": "10400", "synkkaData": {"names": [{"language": "en", "value": "Milk"}], "classifications": [
        {"name": "allergen", "values": [{"id": "MILK", "unit": "CONTAINS"}, {"id": "AW", "unit": "MAY_CONTAIN"}]},
        {"name": "nutritionalClaim", "values": [{"id": "FREE_FROM_LACTOSE"}, {"id": "FREE_FROM_GLUTEN"}]},
    ]}}
    product = normalize_product(raw)
    assert product["properties"]["lactose-free"] == "manufacturer"
    assert product["contradicted_properties"]["gluten-free"] == "manufacturer"
    assert "gluten-free" not in product["properties"]
    raw["synkkaData"]["classifications"][0]["values"].append({"id": "ML", "unit": "CONTAINS"})
    product = normalize_product(raw)
    assert product["contradicted_properties"]["lactose-free"] == "manufacturer"
    assert "lactose-free" not in product["properties"]
