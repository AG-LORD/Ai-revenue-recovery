import json

from fastapi.testclient import TestClient


class VerifiedGateway:
    def verify_webhook_signature(self, body: bytes, signature: str, webhook_secret: str) -> bool:
        return True

    def create_payment_link(self, payload: dict) -> dict:
        return {"id": "plink_test", "short_url": "https://rzp.io/i/test"}


def _webhook(client: TestClient, event: dict):
    return client.post(
        "/webhook/razorpay",
        json=event,
        headers={"X-Razorpay-Signature": "valid"},
    )


def _failure(event_id: str, payment_id: str, order_id: str, amount: int = 50000) -> dict:
    return {
        "id": event_id,
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount,
                    "currency": "INR",
                    "status": "failed",
                    "error_source": "gateway",
                    "error_step": "payment_capture",
                    "error_reason": "unknown_failure",
                    "error_description": "Unclassified payment failure.",
                }
            }
        },
    }


def _capture(
    event_id: str,
    payment_id: str,
    order_id: str,
    amount: int = 50000,
) -> dict:
    return {
        "id": event_id,
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount,
                    "currency": "INR",
                    "status": "captured",
                }
            }
        },
    }


def _order_paid(event_id: str, order_id: str, amount: int = 50000, payment_id: str | None = None) -> dict:
    order = {
        "id": order_id,
        "amount_paid": amount,
        "currency": "INR",
        "status": "paid",
    }
    if payment_id:
        order["payment_id"] = payment_id
    return {
        "id": event_id,
        "event": "order.paid",
        "payload": {"order": {"entity": order}},
    }


def _create_case(database, payment_id: str, order_id: str, amount: int = 50000) -> dict:
    case, created = database.create_or_get_recovery_case(
        {
            "id": payment_id,
            "order_id": order_id,
            "amount": amount,
            "currency": "INR",
        },
        {
            "category": "unknown_failure",
            "diagnosis": "Unclassified failure.",
            "recoverable": False,
            "recommended_action": "manual_review",
            "max_retries": 0,
        },
    )
    assert created is True
    return case


def test_failed_then_captured_reconciles_existing_case() -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    _webhook(client, _failure("evt_fail_1", "pay_fail_1", "order_fail_1"))
    response = _webhook(client, _capture("evt_capture_1", "pay_fail_1", "order_fail_1"))

    assert response.json()["recovered"] is True
    case = database.get_case_by_payment_id("pay_fail_1")
    assert case["recovery_status"] == "RECOVERED"
    assert case["recovered_amount"] == 500.0


def test_failed_authorized_captured_reconciles_to_captured() -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    _webhook(client, _failure("evt_fail_2", "pay_fail_2", "order_fail_2"))
    authorized = {
        "id": "evt_auth_2",
        "event": "payment.authorized",
        "payload": {"payment": {"entity": {"id": "pay_fail_2", "order_id": "order_fail_2", "amount": 50000}}},
    }
    _webhook(client, authorized)
    _webhook(client, _capture("evt_capture_2", "pay_fail_2", "order_fail_2"))

    assert database.get_payment_status("pay_fail_2") == "captured"
    assert database.get_case_by_payment_id("pay_fail_2")["recovery_status"] == "RECOVERED"


def test_capture_and_order_paid_create_one_recovery_transition() -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    _create_case(database, "pay_pair", "order_pair")
    capture = _capture("evt_pair_capture", "pay_pair", "order_pair")
    assert _webhook(client, capture).json()["recovered"] is True
    assert _webhook(client, _order_paid("evt_pair_order", "order_pair")).json()["recovered"] is False

    conn = database.get_connection()
    count = conn.execute(
        "SELECT COUNT(*) AS count FROM audit_trail WHERE payment_id = ? AND action = 'REVENUE_RECOVERED'",
        ("pay_pair",),
    ).fetchone()["count"]
    conn.close()
    assert count == 1
    assert database.get_captured_revenue() == 50000


def test_order_paid_then_capture_does_not_duplicate_recovery() -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    _create_case(database, "pay_reverse", "order_reverse")
    assert _webhook(client, _order_paid("evt_reverse_order", "order_reverse")).json()["recovered"] is True
    assert _webhook(client, _capture("evt_reverse_capture", "pay_reverse", "order_reverse")).json()["recovered"] is False

    assert database.get_case_by_payment_id("pay_reverse")["recovered_amount"] == 500.0
    assert len(database.get_successful_payments()) == 1


def test_same_order_with_different_payment_id_does_not_match_by_order() -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    _create_case(database, "pay_original", "order_shared")
    response = _webhook(client, _capture("evt_other_payment", "pay_other", "order_shared"))

    assert response.json()["recovered"] is False
    assert database.get_case_by_payment_id("pay_original")["recovery_status"] != "RECOVERED"


def test_same_amount_unrelated_capture_does_not_match_case() -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    _create_case(database, "pay_amount_case", "order_amount_case")
    response = _webhook(client, _capture("evt_unrelated", "pay_unrelated", "order_unrelated"))

    assert response.json()["recovered"] is False
    assert database.get_case_by_payment_id("pay_amount_case")["recovery_status"] != "RECOVERED"


def test_duplicate_capture_event_and_already_recovered_case_are_idempotent() -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    _create_case(database, "pay_duplicate", "order_duplicate")
    event = _capture("evt_duplicate_capture", "pay_duplicate", "order_duplicate")
    assert _webhook(client, event).json()["recovered"] is True
    assert _webhook(client, event).json()["status"] == "ignored"
    assert _webhook(client, _order_paid("evt_duplicate_order", "order_duplicate")).json()["recovered"] is False

    case = database.get_case_by_payment_id("pay_duplicate")
    assert case["recovered_amount"] == 500.0
    conn = database.get_connection()
    count = conn.execute(
        "SELECT COUNT(*) AS count FROM audit_trail WHERE payment_id = ? AND action = 'REVENUE_RECOVERED'",
        ("pay_duplicate",),
    ).fetchone()["count"]
    conn.close()
    assert count == 1


def test_capture_without_safe_case_records_lifecycle_only() -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    response = _webhook(client, _capture("evt_orphan_capture", "pay_orphan", "order_orphan", 12345))

    assert response.json()["recovered"] is False
    assert database.get_case_by_payment_id("pay_orphan") is None
    assert database.get_captured_revenue() == 12345
    assert json.loads(database.get_payment_events(payment_id="pay_orphan")[0]["payload"])["event"] == "payment.captured"
