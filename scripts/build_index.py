#!/usr/bin/env python3
"""Stream the Junction-licensed input into a slim local SQLite catalog."""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from door.catalog import Catalog


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("../valio_aimo_junction26_product_data.json"))
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("DATA_DIR", "data")))
    args = parser.parse_args()
    parent = args.source.parent
    count = Catalog(args.data_dir / "catalog.sqlite3").build(
        args.source, parent / "valio_aimo_junction26_delivery_reliability.csv", parent / "valio_aimo_junction26_carbon_footprint.csv")
    print(f"Indexed {count:,} products in {args.data_dir / 'catalog.sqlite3'}.")


if __name__ == "__main__":
    main()
