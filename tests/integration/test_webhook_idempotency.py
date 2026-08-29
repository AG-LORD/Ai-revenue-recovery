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


def test_payment_link_paid_webhook_is_processed_once(monkeypatch) -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    payment = {"id": "pay_original_link_paid", "order_id": "order_link_paid", "amount": 5000, "currency": "INR"}
    diagnosis = {
        "category": "customer_cancelled",
        "diagnosis": "Customer cancelled checkout.",
        "recoverable": True,
        "recommended_action": "send_payment_reminder",
        "max_retries": 0,
    }
    case, created = database.create_or_get_recovery_case(payment, diagnosis)
    assert created is True
    database.update_case_recovery_action(
        case_id=case["id"],
        payment_id=payment["id"],
        recovery_status="LINK_CREATED",
        action_result="PAYMENT_LINK_CREATED",
        payment_link_id="plink_paid_once",
        payment_link_url="https://rzp.io/i/paid-once",
    )
    event = {
        "id": "evt_link_paid_once",
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {"entity": {"id": "plink_paid_once", "amount_paid": 5000}},
            "payment": {"entity": {"id": "pay_recovered_once", "order_id": "order_link_paid", "amount": 5000}},
        },
    }

    first_response = client.post("/webhook/razorpay", json=event, headers={"X-Razorpay-Signature": "valid"})
    second_response = client.post("/webhook/razorpay", json=event, headers={"X-Razorpay-Signature": "valid"})

    assert first_response.status_code == 200
    assert first_response.json()["recovered"] is True
    assert second_response.status_code == 200
    assert second_response.json()["status"] == "ignored"

    conn = database.get_connection()
    recovered_events = conn.execute(
        "SELECT COUNT(*) AS count FROM audit_trail WHERE payment_id = ? AND action = 'REVENUE_RECOVERED'",
        (payment["id"],),
    ).fetchone()
    conn.close()
    assert recovered_events["count"] == 1
