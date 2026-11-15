#!/usr/bin/env python3
"""Verify an ES256 receipt against a JWKS URL or file, without any private key."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import httpx
import jwt


def verify(token: str, jwks: dict) -> dict:
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "ES256":
        raise ValueError("Only ES256 receipts are accepted")
    keys = [key for key in jwks.get("keys", []) if key.get("kid") == header.get("kid") and key.get("kty") == "EC"]
    if len(keys) != 1:
        raise ValueError("Receipt signing key was not uniquely found")
    key = jwt.PyJWK.from_dict(keys[0], algorithm="ES256").key
    payload = jwt.decode(token, key, algorithms=["ES256"], options={"verify_aud": False})
    if payload.get("kind") != "receipt" or not payload.get("record_hash"):
        raise ValueError("The signed document is not a receipt with a decision record hash")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", help="Receipt JWS, or path to a JWS / JSON response containing receipt")
    parser.add_argument("--jwks", default="http://127.0.0.1:8000/.well-known/jwks.json", help="JWKS URL or JSON file")
    args = parser.parse_args()
    try:
        token = args.receipt
        if len(token) < 512 and Path(token).is_file():
            token = Path(token).read_text().strip()
        if token.startswith("{"):
            token = json.loads(token)["receipt"]
        if args.jwks.startswith(("http://", "https://")):
            response = httpx.get(args.jwks, timeout=10)
            response.raise_for_status()
            jwks = response.json()
        else:
            jwks = json.loads(Path(args.jwks).read_text())
        payload = verify(token, jwks)
        print(json.dumps({"verified": True, "receipt": payload}, indent=2))
        return 0
    except (ValueError, KeyError, OSError, jwt.PyJWTError, httpx.HTTPError):
        print("Receipt verification FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
