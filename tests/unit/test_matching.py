from datetime import datetime, timedelta, timezone
import pytest
from door.catalog import Catalog
from door.matching import Matcher, cutoff_passed, pack_content, rank_options, whole_packs
from door.valio import ValioClient


def test_whole_pack_examples():
    assert whole_packs(1000, 1.75) == (572, 1)
    assert whole_packs(1000, 10) == (100, 0)
    assert whole_packs(1000, 12) == (84, 8)
    assert whole_packs(25, 10, 2) == (4, 15)


def test_price_rounding_reconciled_with_hierarchy():
    p = {"names": {"en": "Milk 10 l novobox"}, "sales_unit_multiplier": 2}
    assert pack_content(p, {"unit_price": 31.31, "comparison_price": 1.57, "comparison_unit": "L"}, "L")[0] == 20


def test_ranking():
    def op(i, on_time, inside, overage, price):
        return {"id": i, "on_time": on_time, "within_preferences": inside, "overage": overage, "total": price, "lines": [{"quantity": 1000}]}
    options = [op("late", False, True, 0, 1), op("outside", True, False, 0, 1), op("overage", True, True, 1, 1), op("costly", True, True, 0, 100), op("best", True, True, 0, 99)]
    assert [o["id"] for o in rank_options(options)] == ["best", "costly", "overage", "outside", "late"]


async def test_match_cutoff_and_reservations(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.seed_fixture()
    valio = ValioClient(tmp_path, fixture=True)
    deadline = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    valio.fixture_availability["milk-10"] = {"available": 1000, "delivery_date": deadline, "cutoff": old, "deliverable": True}
    result = await Matcher(catalog, valio).match({"product_type": "drinking milk", "properties": ["lactose-free"], "quantity": 1000, "unit": "L", "deadline": deadline}, "hotel", {"milk-12": 3})
    assert 1 <= len(result["options"]) <= 3
    lines = [o["lines"][0] for o in result["options"]]
    assert next(x for x in lines if x["id"] == "milk-10")["on_time"] is False
    assert next(x for x in lines if x["id"] == "milk-12")["available"] == 9997
    assert all(x["properties"]["lactose-free"] == "manufacturer" for x in lines)
    assert all(x["product_type_source"] == "inferred" for x in lines)


async def test_shortage_has_mix_split_and_full_later(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.seed_fixture()
    valio = ValioClient(tmp_path, fixture=True)
    deadline = valio._fixture_initial_date()
    result = await Matcher(catalog, valio).match({"product_type": "drinking milk", "properties": ["lactose-free"], "quantity": 1000, "unit": "L", "deadline": deadline, "preferred_product_id": "milk-175"}, "hotel")
    assert len(result["options"]) == 3
    by_kind = {option["kind"]: option for option in result["options"]}
    mixed = by_kind["substitute_mix"]
    assert mixed["on_time"] and mixed["within_preferences"] and mixed["soft_trade"]
    assert len({line["id"] for line in mixed["lines"]}) == 2
    assert mixed["lines"][0]["packs"] == 500
    split = by_kind["split_delivery"]
    assert split["partial"] and len(split["lines"]) == 2 and not split["on_time"]
    assert split["lines"][0]["delivery_date"] <= deadline < split["lines"][1]["delivery_date"]
    assert all(line["delivery_date"] > deadline for line in by_kind["all_later"]["lines"])
    assert all(sum(line["quantity"] for line in option["lines"]) >= 1000 for option in result["options"])
    assert all(line["packs"] <= line["available"] for option in result["options"] for line in option["lines"])


async def test_shortage_does_not_invent_future_stock(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.seed_fixture()
    valio = ValioClient(tmp_path, fixture=True)
    deadline = valio._fixture_initial_date()
    valio.fixture_availability["milk-175"] = {"available": 500, "delivery_date": deadline, "deliverable": True, "cutoff": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}
    result = await Matcher(catalog, valio).match({"product_type": "drinking milk", "properties": ["lactose-free"], "quantity": 1000, "unit": "L", "deadline": deadline, "preferred_product_id": "milk-175"}, "hotel")
    assert len(result["options"]) == 1
    assert result["options"][0]["kind"] == "substitute_mix"
    assert all(line["delivery_date"] <= deadline for line in result["options"][0]["lines"])


async def test_simulated_shortage_is_labelled_and_needs_late_signoff(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.seed_fixture()
    valio = ValioClient(tmp_path, fixture=True)
    result = await Matcher(catalog, valio).match({"product_type": "drinking milk", "properties": ["lactose-free"], "quantity": 1000, "unit": "L", "deadline": valio._fixture_initial_date(), "simulate_limited_stock": True}, "hotel")
    assert len(result["options"]) == 3
    assert all(not option["on_time"] and option["simulation"] for option in result["options"])
    assert all(line["simulated_stock"] for option in result["options"] for line in option["lines"])
    assert any(line["on_time"] for line in result["options"][0]["lines"])


async def test_limited_stock_act_retains_three_options_after_prior_order(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.seed_fixture()
    valio = ValioClient(tmp_path, fixture=True)
    need = {"product_type": "drinking milk", "properties": ["lactose-free"], "quantity": 1000, "unit": "L", "deadline": valio._fixture_initial_date()}
    matcher = Matcher(catalog, valio)
    first = (await matcher.match(need, "hotel"))["options"][0]
    reserved = {f"{line['id']}|{line['delivery_date']}": line["packs"] for line in first["lines"]}
    second = await matcher.match({**need, "simulate_limited_stock": True}, "hotel", reserved)
    assert len(second["options"]) == 3
    assert {option["kind"] for option in second["options"]} == {"substitute_mix", "split_delivery", "all_later"}
    assert any(line["on_time"] for line in second["options"][0]["lines"])
    assert all(not option["on_time"] and option["simulation"] for option in second["options"])


async def test_exact_missing_claim_can_be_held_without_inventing_claim(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.seed_fixture()
    valio = ValioClient(tmp_path, fixture=True)
    need = {"product_type": "drinking milk", "properties": ["lactose-free"], "quantity": 100, "unit": "L", "deadline": valio._fixture_initial_date()}
    ordinary = await Matcher(catalog, valio).match(need, "hotel")
    assert all(line["id"] != "milk-unknown" for option in ordinary["options"] for line in option["lines"])
    exact = await Matcher(catalog, valio).match({**need, "product_ids": ["milk-unknown"]}, "hotel")
    line = exact["options"][0]["lines"][0]
    assert line["missing_properties"] == ["lactose-free"]
    assert "lactose-free" not in line["properties"]


async def test_reservation_pools_are_date_specific(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.seed_fixture()
    valio = ValioClient(tmp_path, fixture=True)
    deadline = valio._fixture_initial_date()
    later = (datetime.fromisoformat(deadline) + timedelta(days=1)).date().isoformat()
    result = await Matcher(catalog, valio).match({"product_type": "drinking milk", "properties": ["lactose-free"], "quantity": 1000, "unit": "L", "deadline": deadline, "preferred_product_id": "milk-175"}, "hotel", {f"milk-175|{deadline}": 100, f"milk-175|{later}": 20})
    split = next(option for option in result["options"] if option["kind"] == "split_delivery")
    assert split["lines"][0]["available"] == 400
    assert split["lines"][0]["reserved_at_match"] == 100
    assert split["lines"][1]["available"] == 9980
    assert split["lines"][1]["reserved_at_match"] == 20


async def test_earliest_replenishment_does_not_skip_days_four_to_six(tmp_path, monkeypatch):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.seed_fixture()
    valio = ValioClient(tmp_path, fixture=True)
    deadline = valio._fixture_initial_date()
    replenishment = (datetime.fromisoformat(deadline) + timedelta(days=4)).date().isoformat()
    original = valio.availability

    async def availability(customer, day, ids):
        result = await original(customer, day, ids)
        if "milk-175" in result:
            result["milk-175"]["available"] = 500 if day < replenishment else 10000
        return result

    monkeypatch.setattr(valio, "availability", availability)
    result = await Matcher(catalog, valio).match({"product_type": "drinking milk", "properties": ["lactose-free"], "quantity": 1000, "unit": "L", "deadline": deadline, "preferred_product_id": "milk-175"}, "hotel")
    later = next(option for option in result["options"] if option["kind"] == "all_later")
    assert later["delivery_date"] == replenishment
