"""Separate buyer and door ES256 identities. Private keys never leave this module."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


class SigningKeys:
    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "keys"
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.door = self._load("door", os.getenv("DOOR_SIGNING_KEY"))
        self.buyers = {customer: self._load(f"buyer-{customer}") for customer in ("hotel", "restaurant")}

    def _load(self, name: str, supplied: str | None = None):
        path = self.path / f"{name}.pem"
        if supplied:
            # Environment accepts PEM, never arbitrary file paths.
            raw = supplied.replace("\\n", "\n").encode()
        elif path.exists():
            raw = path.read_bytes()
        else:
            key = ec.generate_private_key(ec.SECP256R1())
            raw = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as out:
                    out.write(raw)
            except FileExistsError:
                raw = path.read_bytes()
        key = serialization.load_pem_private_key(raw, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError("Signing key must be an ES256 P-256 private key")
        return key

    def jwks(self, buyer: bool = False) -> dict:
        keys = self.buyers if buyer else {"door": self.door}
        return {"keys": [dict(json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key())), kid=name, use="sig", alg="ES256") for name, key in keys.items()]}

    def sign_door(self, payload: dict) -> str:
        return jwt.encode(payload, self.door, algorithm="ES256", headers={"kid": "door", "typ": "JWT"})

    def sign_buyer(self, customer: str, payload: dict) -> str:
        return jwt.encode(payload, self.buyers[customer], algorithm="ES256", headers={"kid": customer, "typ": "JWT"})

    def verify_door(self, token: str, kind: str, *, verify_exp: bool = True) -> dict:
        payload = jwt.decode(token, self.door.public_key(), algorithms=["ES256"], options={"verify_exp": verify_exp, "require": ["exp", "kind", "jti"]})
        if payload.get("kind") != kind or jwt.get_unverified_header(token).get("kid") != "door":
            raise jwt.InvalidTokenError("Wrong document type")
        return payload

    def verify_buyer(self, token: str, bot_id: str | None = None) -> dict:
        header = jwt.get_unverified_header(token)
        customer = header.get("kid")
        if customer not in self.buyers:
            raise jwt.InvalidTokenError("Unknown buyer")
        payload = jwt.decode(token, self.buyers[customer].public_key(), algorithms=["ES256"], options={"require": ["exp", "kind", "jti", "customer_id", "bot_id", "limits"]})
        if payload["kind"] != "mandate" or payload["customer_id"] != customer:
            raise jwt.InvalidTokenError("Wrong buyer or document type")
        if bot_id is not None and payload["bot_id"] != bot_id:
            raise jwt.InvalidTokenError("Mandate names a different bot")
        return payload
