from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi import HTTPException
from fastapi.testclient import TestClient

from door.main import create_app


def test_identification_proof_is_single_use_under_concurrent_changes(tmp_path):
    """The proof check, use marker, mandate and record share one transaction."""
    with TestClient(create_app(tmp_path, fixture=True)) as client:
        service = client.app.state.service
        session = client.post("/demo/buyer-session", json={"customer_id": "hotel"}).json()
        identified = client.post(
            "/demo/identify", json={"customer_id": "hotel", "method": "bank"},
            headers={"X-Buyer-Session": session["token"]},
        ).json()
        barrier = threading.Barrier(2)

        def attempt():
            barrier.wait()
            try:
                return service.change_limit(
                    "per_order_cap", {"customer_id": "hotel", "value": 3000},
                    session["token"], identified["proof"],
                )
            except HTTPException as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))

        successes = [result for result in results if isinstance(result, dict)]
        refusals = [result for result in results if isinstance(result, HTTPException)]
        assert len(successes) == 1 and len(refusals) == 1
        assert refusals[0].status_code == 403 and "used" in refusals[0].detail.lower()
        assert successes[0]["mandate_version"] == 2
        customer = next(row for row in client.get("/demo/customers").json()["customers"] if row["id"] == "hotel")
        assert customer["mandate_payload"]["version"] == 2
        assert customer["mandate_payload"]["limits"]["per_order_cap"] == 3000
        with service.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM records WHERE customer_id='hotel'").fetchone()[0] == 1


def test_limit_change_preserves_large_integer_input(tmp_path):
    requested = 2 ** 53 + 1
    with TestClient(create_app(tmp_path, fixture=True)) as client:
        session = client.post("/demo/buyer-session", json={"customer_id": "hotel"}).json()
        headers = {"X-Buyer-Session": session["token"]}
        identified = client.post(
            "/demo/identify", json={"customer_id": "hotel", "method": "bank"}, headers=headers,
        ).json()
        response = client.post(
            "/limits/per_order_cap/change", json={"customer_id": "hotel", "value": requested},
            headers={**headers, "X-Identification-Proof": identified["proof"]},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["change"]["new_value"] == requested
        assert result["limits"]["mandate_version"] == result["mandate_version"]
        customer = next(row for row in client.get("/demo/customers").json()["customers"] if row["id"] == "hotel")
        assert customer["mandate_payload"]["limits"]["per_order_cap"] == requested
