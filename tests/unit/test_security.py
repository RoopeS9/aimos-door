from datetime import timedelta

import jwt
import pytest

from door.security import SigningKeys, digest
from door.service import utcnow


def test_distinct_keys_and_es256(tmp_path):
    keys = SigningKeys(tmp_path)
    payload = {"kind": "mandate", "jti": "one", "customer_id": "hotel", "bot_id": "reference-bot", "limits": {}, "exp": int((utcnow() + timedelta(hours=1)).timestamp())}
    token = keys.sign_buyer("hotel", payload)
    assert keys.verify_buyer(token, "reference-bot") == payload
    with pytest.raises(jwt.InvalidTokenError):
        keys.verify_buyer(keys.sign_door(payload), "reference-bot")
    with pytest.raises(jwt.InvalidTokenError):
        keys.verify_buyer(token, "different-bot")
    assert keys.jwks()["keys"][0]["crv"] == "P-256"
    assert "d" not in keys.jwks()["keys"][0]
    assert (tmp_path / "keys" / "door.pem").stat().st_mode & 0o777 == 0o600
    assert SigningKeys(tmp_path).jwks() == keys.jwks()


def test_record_hash_is_canonical_and_content_bound():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert digest({"a": 1}) != digest({"a": 2})
