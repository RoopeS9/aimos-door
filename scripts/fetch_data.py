#!/usr/bin/env python3
"""Download event-licensed data without forwarding API credentials across hosts."""
import argparse
import asyncio
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

FILES = {
    "products": "valio_aimo_junction26_product_data.json",
    "delivery-reliability": "valio_aimo_junction26_delivery_reliability.csv",
    "carbon-footprint": "valio_aimo_junction26_carbon_footprint.csv",
}
BASE = "https://junction26.valioaimo.fi"


async def fetch(data_dir: Path):
    load_dotenv()
    key = os.getenv("VALIO_API_KEY")
    if not key:
        raise RuntimeError("VALIO_API_KEY is required to fetch the licensed data.")
    data_dir.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=120, headers={"User-Agent": "AimosDoor/0.1 Junction2026"}) as client:
        for endpoint, filename in FILES.items():
            url = httpx.URL(f"{BASE}/api/{endpoint}/download")
            for _ in range(8):
                headers = {"X-API-Key": key} if url.host == "junction26.valioaimo.fi" and url.scheme == "https" else {}
                async with client.stream("GET", url, headers=headers) as response:
                    if response.is_redirect:
                        url = response.url.join(response.headers["location"])
                        if url.scheme != "https":
                            raise RuntimeError("Refusing an insecure data redirect.")
                        continue
                    if response.status_code == 429:
                        raise RuntimeError("Valio usage limit reached; stop development and update STATUS.md.")
                    if response.status_code != 200:
                        raise RuntimeError(f"Valio data download returned HTTP {response.status_code}.")
                    temporary = data_dir / (filename + ".partial")
                    with temporary.open("wb") as stream:
                        async for chunk in response.aiter_bytes():
                            stream.write(chunk)
                    temporary.replace(data_dir / filename)
                    break
            else:
                raise RuntimeError("Too many redirects while downloading event data.")
    return data_dir


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("DATA_DIR", "data")))
    args = parser.parse_args()
    asyncio.run(fetch(args.data_dir))
    print("Downloaded three licensed data files. Do not commit them.")


if __name__ == "__main__":
    main()
