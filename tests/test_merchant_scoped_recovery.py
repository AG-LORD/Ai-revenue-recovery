from app.repositories import database
from app.repositories.merchant_scope import (
    find_case_for_captured_payment_scoped,
    find_case_for_recovery_event_scoped,
)
from app.services.merchant_service import get_merchant_by_key, register_order


def _payment(payment_id: str, order_id: str, amount: int = 179900) -> dict:
    return {
        "id": payment_id,
        "order_id": order_id,
        "amount": amount,
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


def test_captured_payment_cannot_cross_merchant_boundary():
    urban = get_merchant_by_key("urban_cart")
    fit = get_merchant_by_key("fit_gear")
    register_order("order_fit_scoped", fit["id"])
    payment = _payment("pay_fit_scoped", "order_fit_scoped")
    case, _ = database.create_or_get_recovery_case(payment, _diagnosis())

    assert find_case_for_captured_payment_scoped(urban["id"], "pay_fit_scoped") is None
    match = find_case_for_captured_payment_scoped(fit["id"], "pay_fit_scoped")
    assert match is not None
    assert match["id"] == case["id"]


def test_recovery_link_matching_is_merchant_scoped():
    urban = get_merchant_by_key("urban_cart")
    fit = get_merchant_by_key("fit_gear")
    register_order("order_fit_link_scope", fit["id"])
    case, _ = database.create_or_get_recovery_case(_payment("pay_fit_link_scope", "order_fit_link_scope"), _diagnosis())

    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE recovery_cases SET payment_link_id = ? WHERE id = ?",
            ("plink_fit_scope", case["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    assert find_case_for_recovery_event_scoped(urban["id"], payment_link_id="plink_fit_scope") is None
    match = find_case_for_recovery_event_scoped(fit["id"], payment_link_id="plink_fit_scope")
    assert match is not None
    assert match["id"] == case["id"]
