import json

import pytest
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


def _register_order(order_id: str, merchant_key: str = "urban_cart") -> dict:
    from app.services.merchant_service import get_merchant_by_key, register_order

    merchant = get_merchant_by_key(merchant_key)
    register_order(order_id, merchant["id"])
    return merchant


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


def test_customer_native_retry_recovers_escalated_case_without_rewriting_policy() -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    _register_order("order_native_retry")
    assert _webhook(client, _failure("evt_native_fail", "pay_native_p1", "order_native_retry")).status_code == 200

    response = _webhook(client, _capture("evt_native_capture", "pay_native_p2", "order_native_retry"))
    assert response.json()["recovered"] is True
    case = database.get_case_by_payment_id("pay_native_p1")
    assert case["diagnosis_category"] == "unknown_failure"
    assert case["action_taken"] == "escalate_manual_review"
    assert case["recovery_status"] == "RECOVERED"
    assert case["recovery_source"] == "CUSTOMER_RETRY"
    assert case["recovered_payment_id"] == "pay_native_p2"
    assert case["recovered_amount"] == 500.0


def test_second_failed_attempt_then_customer_capture_uses_one_opportunity() -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    _register_order("order_multiple_native")
    _webhook(client, _failure("evt_multiple_p1", "pay_multiple_p1", "order_multiple_native"))
    _webhook(client, _failure("evt_multiple_p2", "pay_multiple_p2", "order_multiple_native"))

    assert database.get_case_by_payment_id("pay_multiple_p2") is None
    response = _webhook(client, _capture("evt_multiple_p3", "pay_multiple_p3", "order_multiple_native"))
    assert response.json()["recovered"] is True
    case = database.get_case_by_payment_id("pay_multiple_p1")
    assert case["recovery_source"] == "CUSTOMER_RETRY"
    assert case["recovered_payment_id"] == "pay_multiple_p3"
    assert len(database.get_payment_events(order_id="order_multiple_native")) == 3


def test_same_order_ambiguous_candidates_are_lifecycle_only() -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    merchant = _register_order("order_ambiguous")
    first = _create_case(database, "pay_ambiguous_1", "order_ambiguous")
    second = _create_case(database, "pay_ambiguous_2", "order_ambiguous")
    # Direct setup mirrors historical malformed data; scope is explicit so the
    # reconciler must decline rather than choosing an arbitrary case.
    conn = database.get_connection()
    conn.execute("UPDATE recovery_cases SET merchant_account_id = ? WHERE id IN (?, ?)", (merchant["id"], first["id"], second["id"]))
    conn.commit()
    conn.close()

    response = _webhook(client, _capture("evt_ambiguous_capture", "pay_ambiguous_new", "order_ambiguous"))
    assert response.json()["recovered"] is False
    assert database.get_case_by_payment_id("pay_ambiguous_1")["recovery_status"] != "RECOVERED"
    assert database.get_case_by_payment_id("pay_ambiguous_2")["recovery_status"] != "RECOVERED"
    assert database.get_payment_events(payment_id="pay_ambiguous_new")


def test_same_order_amount_or_currency_mismatch_is_not_recovered() -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    _register_order("order_mismatch")
    _webhook(client, _failure("evt_mismatch_fail", "pay_mismatch_p1", "order_mismatch"))
    mismatch = _capture("evt_mismatch_capture", "pay_mismatch_p2", "order_mismatch", 49999)
    mismatch["payload"]["payment"]["entity"]["currency"] = "USD"
    assert _webhook(client, mismatch).json()["recovered"] is False
    assert database.get_case_by_payment_id("pay_mismatch_p1")["recovery_status"] == "ESCALATED"


def test_customer_retry_merchant_scope_never_cross_recovers() -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    _register_order("order_owner_fit", "fit_gear")
    urban = _register_order("order_unrelated_urban", "urban_cart")
    case = _create_case(database, "pay_urban_case", "order_owner_fit")
    conn = database.get_connection()
    conn.execute("UPDATE recovery_cases SET merchant_account_id = ? WHERE id = ?", (urban["id"], case["id"]))
    conn.commit()
    conn.close()

    assert _webhook(client, _capture("evt_fit_capture", "pay_fit_new", "order_owner_fit")).json()["recovered"] is False
    assert database.get_case_by_payment_id("pay_urban_case")["recovery_status"] != "RECOVERED"


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


def _link_case(database, payment_id: str, order_id: str, status: str = "LINK_CREATED") -> dict:
    case, created = database.create_or_get_recovery_case(
        {"id": payment_id, "order_id": order_id, "amount": 50000, "currency": "INR"},
        {
            "category": "customer_cancelled",
            "diagnosis": "Customer cancelled checkout.",
            "recoverable": True,
            "recommended_action": "send_payment_reminder",
            "max_retries": 0,
        },
    )
    assert created is True
    database.update_case_policy(case["id"], payment_id, {
        "decision": "ALLOW_REMINDER",
        "action_allowed": "send_payment_reminder",
        "next_status": "PENDING_REMINDER",
    })
    database.update_case_recovery_action(
        case["id"], payment_id, status, "PAYMENT_LINK_CREATED",
        payment_link_id="plink_exact",
        payment_link_url="https://rzp.io/i/exact",
    )
    return database.get_case_by_payment_id(payment_id)


def _link_paid(event_id: str, payment_link_id: str = "plink_exact", amount: int = 50000, currency: str = "INR") -> dict:
    return {
        "id": event_id,
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": payment_link_id,
                    "amount_paid": amount,
                    "currency": currency,
                    "order_id": "order_link_exact",
                    "notes": {"original_payment_id": "pay_link_exact"},
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_link_success",
                    "order_id": "order_link_exact",
                    "amount": amount,
                    "currency": currency,
                    "status": "captured",
                    "notes": {"original_payment_id": "pay_link_exact"},
                }
            },
        },
    }


def test_payment_link_exact_match_recovers_with_amount_and_currency() -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    _register_order("order_link_exact")
    _link_case(database, "pay_link_exact", "order_link_exact")

    response = _webhook(client, _link_paid("evt_link_exact"))

    assert response.json()["recovered"] is True
    case = database.get_case_by_payment_id("pay_link_exact")
    assert case["recovery_status"] == "RECOVERED"
    assert case["recovery_source"] == "PAYMENT_LINK"


@pytest.mark.parametrize(
    ("event_id", "payment_link_id", "amount", "currency", "status"),
    [
        ("evt_link_wrong_currency", "plink_exact", 50000, "USD", "LINK_CREATED"),
        ("evt_link_wrong_amount", "plink_exact", 49999, "INR", "LINK_CREATED"),
        ("evt_link_escalated", "plink_exact", 50000, "INR", "ESCALATED"),
        ("evt_link_pending_retry", "plink_exact", 50000, "INR", "PENDING_RETRY"),
        ("evt_link_wrong_id", "plink_other", 50000, "INR", "LINK_CREATED"),
    ],
)
def test_payment_link_success_requires_exact_authorized_case(
    event_id: str,
    payment_link_id: str,
    amount: int,
    currency: str,
    status: str,
) -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    _register_order("order_link_exact")
    _link_case(database, "pay_link_exact", "order_link_exact", status=status)

    response = _webhook(
        client,
        _link_paid(event_id, payment_link_id=payment_link_id, amount=amount, currency=currency),
    )

    assert response.json()["recovered"] is False
    assert database.get_case_by_payment_id("pay_link_exact")["recovery_status"] == status


def test_payment_link_duplicate_event_ids_and_new_event_ids_recover_once() -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    _register_order("order_link_exact")
    _link_case(database, "pay_link_exact", "order_link_exact")

    first = _webhook(client, _link_paid("evt_link_duplicate"))
    duplicate = _webhook(client, _link_paid("evt_link_duplicate"))
    second_event = _webhook(client, _link_paid("evt_link_duplicate_2"))

    assert first.json()["recovered"] is True
    assert duplicate.json()["status"] == "ignored"
    assert second_event.json()["recovered"] is False
    conn = database.get_connection()
    count = conn.execute(
        "SELECT COUNT(*) AS count FROM audit_trail WHERE payment_id = ? AND action = 'REVENUE_RECOVERED'",
        ("pay_link_exact",),
    ).fetchone()["count"]
    conn.close()
    assert count == 1


def test_recovered_case_is_terminal_for_duplicate_failure_and_capture() -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    _register_order("order_terminal")
    _webhook(client, _failure("evt_terminal_fail", "pay_terminal", "order_terminal"))
    capture = _capture("evt_terminal_capture", "pay_terminal", "order_terminal")
    assert _webhook(client, capture).json()["recovered"] is True

    duplicate_failure = _failure("evt_terminal_failure_again", "pay_terminal", "order_terminal")
    assert _webhook(client, duplicate_failure).status_code == 200
    assert _webhook(client, _capture("evt_terminal_capture_again", "pay_terminal", "order_terminal")).json()["recovered"] is False

    case = database.get_case_by_payment_id("pay_terminal")
    assert case["recovery_status"] == "RECOVERED"
    assert case["recovered_payment_id"] == "pay_terminal"
    assert case["recovery_source"] == "CUSTOMER_RETRY"
    assert case["recovered_event_id"] == "evt_terminal_capture"


def test_duplicate_original_failure_does_not_consume_pending_retry() -> None:
    from main import app
    from app.repositories import database

    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    _register_order("order_duplicate_original")
    failure = _failure("evt_duplicate_original_one", "pay_duplicate_original", "order_duplicate_original")
    failure["payload"]["payment"]["entity"].update({
        "error_source": "issuer",
        "error_step": "payment_authorization",
        "error_description": "Temporary bank issue.",
    })
    assert _webhook(client, failure).status_code == 200
    assert database.get_case_by_payment_id("pay_duplicate_original")["recovery_status"] == "PENDING_RETRY"

    duplicate = _failure("evt_duplicate_original_two", "pay_duplicate_original", "order_duplicate_original")
    duplicate["payload"]["payment"]["entity"].update({
        "error_source": "issuer",
        "error_step": "payment_authorization",
        "error_description": "Temporary bank issue.",
    })
    assert _webhook(client, duplicate).status_code == 200

    case = database.get_case_by_payment_id("pay_duplicate_original")
    assert case["recovery_status"] == "PENDING_RETRY"
    assert case["retry_count"] == 1
