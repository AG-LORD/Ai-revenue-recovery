import importlib

from fastapi.testclient import TestClient


class VerifiedGateway:
    def verify_webhook_signature(self, body: bytes, signature: str, webhook_secret: str) -> bool:
        return True


def _failed_payment_event() -> dict:
    return {
        "id": "evt_processing_failure",
        "event": "payment.failed",
        "payload": {"payment": {"entity": {"id": "pay_processing_failure", "amount": 100}}},
    }


def test_failed_payment_webhook_is_retryable_after_processing_failure(monkeypatch) -> None:
    from main import app
    from app.repositories import database

    router_module = importlib.import_module("app.api.router")
    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)

    def fail_processing(payment, gateway):
        raise RuntimeError("downstream failure")

    monkeypatch.setattr(router_module, "process_failed_payment", fail_processing)
    response = client.post("/webhook/razorpay", json=_failed_payment_event(), headers={"X-Razorpay-Signature": "valid"})
    assert response.status_code == 500

    conn = database.get_connection()
    remaining_event = conn.execute("SELECT status FROM webhook_events WHERE event_id = ?", ("evt_processing_failure",)).fetchone()
    conn.close()
    assert remaining_event is None

    monkeypatch.setattr(
        router_module,
        "process_failed_payment",
        lambda payment, gateway: {"case": {"recovery_status": "DETECTED"}, "policy": {}, "recovery": {}},
    )
    retry_response = client.post("/webhook/razorpay", json=_failed_payment_event(), headers={"X-Razorpay-Signature": "valid"})
    assert retry_response.status_code == 200
    assert retry_response.json()["status"] == "ok"

    conn = database.get_connection()
    processed_event = conn.execute("SELECT status FROM webhook_events WHERE event_id = ?", ("evt_processing_failure",)).fetchone()
    conn.close()
    assert processed_event["status"] == "PROCESSED"
