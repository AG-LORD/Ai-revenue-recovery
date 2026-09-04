from app.repositories import database
from app.services.merchant_service import (
    get_merchant_by_key,
    get_merchant_by_order_id,
    register_order,
    resolve_event_merchant,
)


def _failure_payment(payment_id: str, order_id: str) -> dict:
    return {
        "id": payment_id,
        "order_id": order_id,
        "amount": 179900,
        "currency": "INR",
        "status": "failed",
        "method": "card",
        "error_code": "BAD_REQUEST_ERROR",
        "error_source": "issuer",
        "error_step": "payment_authorization",
        "error_reason": "payment_failed",
        "error_description": "Temporary bank issue",
    }


def _diagnosis() -> dict:
    return {
        "category": "temporary_issuer_failure",
        "diagnosis": "Temporary issuer failure",
        "recoverable": True,
        "recommended_action": "retry_payment",
        "max_retries": 1,
    }


def test_order_registry_keeps_merchants_separate():
    urban = get_merchant_by_key("urban_cart")
    fit = get_merchant_by_key("fit_gear")

    register_order("order_urban_1", urban["id"])
    register_order("order_fit_1", fit["id"])

    assert get_merchant_by_order_id("order_urban_1")["id"] == urban["id"]
    assert get_merchant_by_order_id("order_fit_1")["id"] == fit["id"]
    assert get_merchant_by_order_id("order_urban_1")["id"] != fit["id"]


def test_payment_case_and_audit_inherit_order_merchant():
    fit = get_merchant_by_key("fit_gear")
    register_order("order_fit_case", fit["id"])

    payload = {
        "event": "payment.failed",
        "id": "evt_fit_case",
        "payload": {"payment": {"entity": _failure_payment("pay_fit_case", "order_fit_case")}},
    }

    assert database.record_payment_event(
        event_id="evt_fit_case",
        event_type="payment.failed",
        payload_dict=payload,
        source="razorpay_webhook",
        payment_id="pay_fit_case",
        order_id="order_fit_case",
        amount_paise=179900,
        currency="INR",
        payment_status="failed",
    )

    case, created = database.create_or_get_recovery_case(
        _failure_payment("pay_fit_case", "order_fit_case"),
        _diagnosis(),
    )
    assert created is True

    conn = database.get_connection()
    try:
        payment_row = conn.execute(
            "SELECT merchant_account_id FROM payment_events WHERE event_id = ?",
            ("evt_fit_case",),
        ).fetchone()
        case_row = conn.execute(
            "SELECT merchant_account_id FROM recovery_cases WHERE id = ?",
            (case["id"],),
        ).fetchone()
        audit_rows = conn.execute(
            "SELECT merchant_account_id FROM audit_trail WHERE case_id = ?",
            (case["id"],),
        ).fetchall()
    finally:
        conn.close()

    assert payment_row["merchant_account_id"] == fit["id"]
    assert case_row["merchant_account_id"] == fit["id"]
    assert audit_rows
    assert all(row["merchant_account_id"] == fit["id"] for row in audit_rows)


def test_webhook_event_inherits_merchant_from_order_payload():
    learn = get_merchant_by_key("learn_pro")
    register_order("order_learn_webhook", learn["id"])

    payload = {
        "event": "payment.failed",
        "id": "evt_learn_webhook",
        "payload": {
            "payment": {
                "entity": _failure_payment("pay_learn_webhook", "order_learn_webhook")
            }
        },
    }

    assert database.record_webhook_event(
        event_id="evt_learn_webhook",
        event_type="payment.failed",
        payload_dict=payload,
    )

    conn = database.get_connection()
    try:
        row = conn.execute(
            "SELECT merchant_account_id FROM webhook_events WHERE event_id = ?",
            ("evt_learn_webhook",),
        ).fetchone()
    finally:
        conn.close()

    assert row["merchant_account_id"] == learn["id"]


def test_event_resolver_uses_order_ownership_when_demo_accounts_are_shared():
    urban = get_merchant_by_key("urban_cart")
    fit = get_merchant_by_key("fit_gear")
    register_order("order_fit_resolve", fit["id"])

    event = {
        "account_id": "demo_account",
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": _failure_payment("pay_fit_resolve", "order_fit_resolve")
            }
        },
    }

    resolved = resolve_event_merchant(event)
    assert resolved is not None
    assert resolved["id"] == fit["id"]
    assert resolved["id"] != urban["id"]
