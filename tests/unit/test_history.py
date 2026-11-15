from datetime import date, datetime, timedelta, timezone

import pytest

from door.catalog import Catalog
from door.history import HistorySeedError, build_history
from door.matching import whole_packs
from door.valio import ValioClient


def fixture_sources(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.seed_fixture()
    valio = ValioClient(tmp_path, fixture=True)
    # Fixed cutoff makes repeated builds themselves deterministic as well as
    # the application's required frozen-snapshot reset behavior.
    for product in catalog.search(limit=100):
        valio.fixture_availability[product["id"]] = {
            "available": 10000, "delivery_date": "2026-11-16", "cutoff": "2099-11-15T17:00:00+02:00",
            "sales_block": False, "logistic_model": "Stock", "subject_to_supplier": False,
            "deliverable": True, "source": "synthetic fixture",
        }
    return catalog, valio


async def test_history_is_twelve_weekly_synthetic_snapshots(tmp_path):
    catalog, valio = fixture_sources(tmp_path)
    seed = await build_history(catalog, valio, as_of=date(2026, 11, 14))
    assert len(seed["history"]) == 12 and 12 <= len(seed["lines"]) <= 15
    assert len({line["category"] for line in seed["lines"]}) >= 5
    assert sum(line["product_type"] == "drinking milk" for line in seed["lines"]) == 1
    assert seed["synthetic"] and seed["anchor_confirmed"]
    assert "synthetic" in seed["label"].lower()
    days = [date.fromisoformat(entry["date"]) for entry in seed["history"]]
    assert days[-1] == date(2026, 11, 9)
    assert all((right - left).days == 7 for left, right in zip(days, days[1:]))
    assert all(entry["synthetic"] and entry["historical_price_source"].startswith("simulated") for entry in seed["history"])
    assert len(seed["proposal_evidence"]) == 4
    assert valio.calls == 0
    assert seed == await build_history(catalog, valio, as_of=date(2026, 11, 14))


async def test_milk_increment_is_twenty_percent_in_whole_packs(tmp_path):
    catalog, valio = fixture_sources(tmp_path)
    seed = await build_history(catalog, valio, as_of=date(2026, 11, 14))
    milk = next(line for line in seed["lines"] if line["product_type"] == "drinking milk")
    new_packs, _ = whole_packs(milk["quantity"] + 5, milk["pack_size"], milk["lot_size"])
    assert new_packs * milk["pack_size"] >= milk["quantity"] + 5
    assert (new_packs - milk["packs"]) / milk["packs"] == pytest.approx(.2)
    assert milk["properties"]["lactose-free"] == "manufacturer"
    assert milk["required_properties"] == ["lactose-free"]
    rice = next(line for line in seed["lines"] if line["product_type"] == "rice")
    assert rice["required_properties"] == [] and rice["unit"] == "KG"
    assert all(line["inventory_date"] == line["delivery_date"] for line in seed["lines"])


async def test_seed_rejects_past_cutoff_and_unstated_milk(tmp_path):
    catalog, valio = fixture_sources(tmp_path)
    for product in catalog.candidates("drinking milk", ["lactose-free"]):
        valio.fixture_availability[product["id"]]["cutoff"] = "2020-01-01T00:00:00+00:00"
    with pytest.raises(HistorySeedError, match="on-time drinking milk"):
        await build_history(catalog, valio, as_of=date(2026, 11, 14))
    assert valio.calls == 0


async def test_historical_entries_do_not_share_mutable_line_objects(tmp_path):
    catalog, valio = fixture_sources(tmp_path)
    seed = await build_history(catalog, valio, as_of=date(2026, 11, 14))
    original = seed["history"][1]["lines"][0]["packs"]
    seed["history"][0]["lines"][0]["packs"] = 9999
    assert seed["history"][1]["lines"][0]["packs"] == original
    assert seed["lines"][0]["packs"] == original
    assert seed["anchor"]["lines"][0]["packs"] == original
