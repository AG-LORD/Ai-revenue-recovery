import json

import pytest
from fastapi.testclient import TestClient


class VerifiedGateway:
    def verify_webhook_signature(self, body: bytes, signature: str, webhook_secret: str) -> bool:
        return True

    def create_payment_link(self, payload: dict) -> dict:
        return {"id": "plink_test", "short_url": "https://rzp.io/i/test"}


def _event(event_type: str, event_id: str) -> dict:
    payment = {
        "id": f"pay_{event_id}",
        "order_id": f"order_{event_id}",
        "amount": 12345,
        "currency": "INR",
        "status": event_type.rsplit(".", 1)[-1],
    }
    if event_type == "payment.failed":
        payment.update(
            {
                "error_source": "gateway",
                "error_step": "payment_capture",
                "error_reason": "unhandled_code_99",
                "error_description": "Unclassified gateway error.",
            }
        )
    if event_type == "order.paid":
        return {
            "id": event_id,
            "event": event_type,
            "payload": {
                "order": {
                    "entity": {
                        "id": f"order_{event_id}",
                        "amount_paid": 12345,
                        "currency": "INR",
                        "status": "paid",
                    }
                }
            },
        }
    if event_type == "payment_link.paid":
        return {
            "id": event_id,
            "event": event_type,
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": f"plink_{event_id}",
                        "amount_paid": 12345,
                        "status": "paid",
                    }
                },
                "payment": {"entity": payment},
            },
        }
    return {
        "id": event_id,
        "event": event_type,
        "payload": {"payment": {"entity": payment}},
    }


@pytest.mark.parametrize(
    "event_type",
    [
        "payment.failed",
        "payment.authorized",
        "payment.captured",
        "order.paid",
        "payment_link.paid",
    ],
)
def test_supported_webhook_lifecycle_events_are_stored(event_type: str) -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    event = _event(event_type, f"evt_{event_type.replace('.', '_')}")
    response = TestClient(app).post(
        "/webhook/razorpay",
        json=event,
        headers={"X-Razorpay-Signature": "valid"},
    )

    assert response.status_code == 200
    stored = database.get_payment_events(
        payment_id=event["payload"].get("payment", {}).get("entity", {}).get("id"),
        order_id=event["payload"].get("order", {}).get("entity", {}).get("id"),
    )
    assert len(stored) == 1
    assert stored[0]["event_id"] == event["id"]
    assert stored[0]["event_type"] == event_type
    assert stored[0]["source"] == "razorpay_webhook"
    assert stored[0]["amount_paise"] == 12345
    assert stored[0]["processing_status"] == "PROCESSED"
    assert stored[0]["payload"] == json.dumps(event)


def test_duplicate_lifecycle_webhook_is_idempotent() -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    event = _event("payment.captured", "evt_duplicate_capture")
    client = TestClient(app)
    first = client.post("/webhook/razorpay", json=event, headers={"X-Razorpay-Signature": "valid"})
    second = client.post("/webhook/razorpay", json=event, headers={"X-Razorpay-Signature": "valid"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "ignored"
    assert database.has_payment_event(event["id"])
    assert len(database.get_payment_events(payment_id="pay_evt_duplicate_capture")) == 1


def test_successful_checkout_activity_is_visible_without_a_recovery_case() -> None:
    from main import app
    from app.repositories import database
    from app.services.merchant_service import register_order

    order_id = "order_visible_checkout"
    register_order(order_id, 1)
    app.state.razorpay_client = VerifiedGateway()
    event = _event("payment.captured", "evt_visible_checkout")
    event["payload"]["payment"]["entity"]["order_id"] = order_id

    response = TestClient(app).post(
        "/webhook/razorpay",
        json=event,
        headers={"X-Razorpay-Signature": "valid"},
    )

    assert response.status_code == 200
    assert database.get_case_by_payment_id("pay_evt_visible_checkout", merchant_account_id=1) is None
    activity = TestClient(app).get("/api/payment-activity")
    assert activity.status_code == 200
    payment = next(
        item
        for item in activity.json()["payments"]
        if item["payment_id"] == "pay_evt_visible_checkout"
    )
    assert payment["order_id"] == order_id
    assert payment["amount_paise"] == 12345
    assert payment["status"] == "captured"
    assert payment["source"] == "razorpay_webhook"


def test_payment_activity_orders_newest_captures_first_and_keeps_newest_with_many_records() -> None:
    from app.repositories import database

    for index in range(21):
        event_id = f"evt_ordered_capture_{index:02d}"
        assert _store_lifecycle_event(
            event_id,
            "payment.captured",
            f"pay_ordered_{index:02d}",
            f"order_ordered_{index:02d}",
            10000 + index,
        )

    conn = database.get_connection()
    try:
        for index in range(21):
            conn.execute(
                "UPDATE payment_events SET received_at = ? WHERE event_id = ?",
                (f"2026-09-05T12:{index:02d}:00+00:00", f"evt_ordered_capture_{index:02d}"),
            )
        conn.commit()
    finally:
        conn.close()

    activity = database.get_successful_payments()
    assert len(activity) == 21
    assert activity[0]["payment_id"] == "pay_ordered_20"
    assert activity[0]["timestamp"] == "2026-09-05T12:20:00+00:00"
    assert activity[19]["payment_id"] == "pay_ordered_01"
    assert activity[20]["payment_id"] == "pay_ordered_00"


def test_captured_payment_activity_stays_outside_recovery_cases() -> None:
    from app.repositories import database

    assert _store_lifecycle_event(
        "evt_activity_only_capture",
        "payment.captured",
        "pay_activity_only",
        "order_activity_only",
        309800,
    )

    activity = database.get_successful_payments()
    assert any(item["payment_id"] == "pay_activity_only" for item in activity)
    assert database.get_case_by_payment_id("pay_activity_only") is None


def test_simulation_events_are_tagged_as_simulation() -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    response = TestClient(app).post("/api/simulate", json={"scenario": "unknown_failure"})

    assert response.status_code == 200
    events = database.get_payment_events()
    assert events
    assert events[-1]["source"] == "simulation"


def _store_lifecycle_event(
    event_id: str,
    event_type: str,
    payment_id: str | None,
    order_id: str | None,
    amount_paise: int,
) -> bool:
    from app.repositories import database

    payload = {
        "id": event_id,
        "event": event_type,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount_paise,
                    "currency": "INR",
                    "status": event_type.rsplit(".", 1)[-1],
                }
            }
        },
    }
    return database.record_payment_event(
        event_id=event_id,
        event_type=event_type,
        payload_dict=payload,
        source="system",
        payment_id=payment_id,
        order_id=order_id,
        amount_paise=amount_paise,
        currency="INR",
        payment_status=event_type.rsplit(".", 1)[-1],
    )


def test_canonical_payment_state_handles_authorized_failed_and_captured() -> None:
    from app.repositories import database

    _store_lifecycle_event("evt_authorized", "payment.authorized", "pay_1", "order_1", 10000)
    _store_lifecycle_event("evt_failed", "payment.failed", "pay_1", "order_1", 10000)
    _store_lifecycle_event("evt_captured", "payment.captured", "pay_1", "order_1", 10000)

    assert database.get_payment_status("pay_1") == "captured"
    assert database.get_payment_lifecycle("pay_1")["is_successful"] is True
    assert database.get_captured_revenue() == 10000


def test_authorized_without_capture_is_not_successful() -> None:
    from app.repositories import database

    _store_lifecycle_event("evt_authorized_only", "payment.authorized", "pay_2", "order_2", 20000)

    assert database.get_payment_status("pay_2") == "authorized"
    assert database.get_payment_lifecycle("pay_2")["is_successful"] is False
    assert database.get_captured_revenue() == 0


def test_captured_and_order_paid_are_one_canonical_success() -> None:
    from app.repositories import database

    _store_lifecycle_event("evt_capture", "payment.captured", "pay_3", "order_3", 30000)
    _store_lifecycle_event("evt_order_paid", "order.paid", None, "order_3", 30000)

    successful = database.get_successful_payments()
    assert len(successful) == 1
    assert successful[0]["amount_paise"] == 30000
    assert database.get_captured_revenue() == 30000


def test_distinct_payment_ids_sharing_order_are_not_merged() -> None:
    from app.repositories import database

    _store_lifecycle_event("evt_capture_a", "payment.captured", "pay_a", "order_shared", 30000)
    _store_lifecycle_event("evt_capture_b", "payment.captured", "pay_b", "order_shared", 40000)

    successful = database.get_successful_payments()
    assert len(successful) == 2
    assert database.get_captured_revenue() == 70000


def test_duplicate_captured_event_does_not_double_count() -> None:
    from app.repositories import database

    assert _store_lifecycle_event("evt_capture_once", "payment.captured", "pay_4", "order_4", 40000)
    assert not _store_lifecycle_event("evt_capture_once", "payment.captured", "pay_4", "order_4", 40000)

    assert database.get_captured_revenue() == 40000
    assert len(database.get_payment_events(payment_id="pay_4")) == 1


def test_successful_payment_does_not_require_recovery_case() -> None:
    from app.repositories import database

    _store_lifecycle_event("evt_standalone_capture", "payment.captured", "pay_5", "order_5", 50000)

    assert database.get_case_by_payment_id("pay_5") is None
    assert database.get_successful_payments()[0]["payment_id"] == "pay_5"
