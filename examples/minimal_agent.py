#!/usr/bin/env python3
"""A purchasing agent in about forty lines: no SDK, no model, plain HTTP.

It finds a product, asks for a signed fixed-price offer, places the order and checks the
door's signature on the receipt. Any agent that can make HTTP calls can do the same.

    uv run python examples/minimal_agent.py --door http://127.0.0.1:8000
    uv run python examples/minimal_agent.py --door https://your-door.example --access-code "$DEMO_ACCESS_CODE"

The buyer's signed mandate is the agent's authority. In the demo it is read from
/demo/customers; a real buyer would hand its agent the mandate it signed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import uuid

import httpx
import jwt

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--door", default="http://127.0.0.1:8000")
parser.add_argument("--access-code", default=None, help="Only if the demo is behind an access code")
parser.add_argument("--customer", default="hotel")
parser.add_argument("--litres", type=float, default=40)
args = parser.parse_args()

headers = {"X-Demo-Access-Code": args.access_code} if args.access_code else {}
door = httpx.Client(base_url=args.door.rstrip("/"), headers=headers, timeout=60)

def call(method: str, path: str, **kwargs) -> dict:
    response = door.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()

# 1. Authority: the mandate the buyer signed, and the bot it names.
customer = next(row for row in call("GET", "/demo/customers")["customers"] if row["id"] == args.customer)
identity = {"mandate": customer["mandate"], "bot_id": customer["bot_id"]}

# 2. Find: state the need; the door answers with ranked options and the filters it applied.
today = dt.date.today()
next_monday = today + dt.timedelta(days=(7 - today.weekday()) % 7 or 7)
need = {"product_type": "drinking milk", "properties": ["lactose-free"], "quantity": args.litres, "unit": "L", "deadline": next_monday.isoformat()}
options = call("POST", "/needs/match", json={**identity, "need": need})["options"]
best = options[0]
print(f"found {len(options)} options; best: {best.get('label')} at €{best.get('total')}")

# 3. Offer: a signed, fixed price. Nothing is reserved yet.
offer = call("POST", "/offers", json={**identity, "option": best, "need": need})["offer"]

# 4. Order: the door runs its ten checks against the mandate and commits, notifies or holds.
order = call("POST", "/orders", json={**identity, "offer": offer, "need": need}, headers={"Idempotency-Key": str(uuid.uuid4())})
print("order status:", order["status"])
for check in order["checks"]:
    print(f"  {'pass' if check['passed'] else 'FAIL'}  {check['name']}")

# 5. Verify: anyone can check the receipt against the door's public keys.
if order.get("receipt"):
    keys = {key["kid"]: key for key in call("GET", "/.well-known/jwks.json")["keys"]}
    key = jwt.PyJWK.from_dict(keys[jwt.get_unverified_header(order["receipt"])["kid"]], algorithm="ES256").key
    receipt = jwt.decode(order["receipt"], key, algorithms=["ES256"], options={"verify_aud": False})
    print("receipt signature valid; record hash", receipt["record_hash"][:16], "…")
else:
    print("no receipt: the order waits for the buyer at", order.get("continue_url"))
