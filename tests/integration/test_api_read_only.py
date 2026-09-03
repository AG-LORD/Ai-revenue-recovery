from fastapi.testclient import TestClient

from app.repositories import database
from main import app


client = TestClient(app)


def test_public_read_only_endpoints() -> None:
    for path in ("/", "/dashboard", "/checkout", "/api/metrics", "/api/cases"):
        response = client.get(path)
        assert response.status_code == 200


def test_webhook_rejects_missing_signature() -> None:
    response = client.post("/webhook/razorpay", content=b"{}")
    assert response.status_code == 400


def test_payment_status_endpoint_returns_safe_authoritative_state() -> None:
    database.record_payment_event(
        event_id="evt_capture_status",
        event_type="payment.captured",
        payload_dict={
            "id": "evt_capture_status",
            "event": "payment.captured",
            "secret": "must-not-be-returned",
        },
        source="razorpay_webhook",
        payment_id="pay_status",
        order_id="order_status",
        amount_paise=50000,
        currency="INR",
        payment_status="captured",
    )

    response = client.get("/api/cases/pay_status")

    assert response.status_code == 200
    body = response.json()
    assert body["payment_id"] == "pay_status"
    assert body["payment_lifecycle_status"] == "captured"
    assert body["is_successful"] is True
    assert body["amount"] == 500.0
    assert "payload" not in body
    assert "secret" not in body


def test_payment_status_endpoint_returns_not_found_for_unknown_payment() -> None:
    response = client.get("/api/cases/pay_missing")

    assert response.status_code == 404
