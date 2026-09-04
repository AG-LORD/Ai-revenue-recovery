"""Service layer for recovery execution."""

import logging
from typing import Any, Dict

from app.integrations.razorpay_client import PaymentGateway, PaymentGatewayUnavailable
from app.repositories import database
from app.repositories.merchant_scope import (
    find_case_for_captured_payment_scoped,
    find_case_for_recovery_event_scoped,
    find_same_order_recovery_candidates_scoped,
)
from app.services.merchant_service import resolve_event_merchant

logger = logging.getLogger(__name__)


def _get_stored_amount_paise(case_id: int, payment_id: str) -> int | None:
    conn = database.get_connection()
    try:
        row = conn.execute(
            "SELECT amount FROM recovery_cases WHERE id = ? AND payment_id = ? LIMIT 1",
            (case_id, payment_id),
        ).fetchone()
        return int(row["amount"]) if row else None
    finally:
        conn.close()


def _legacy_recovery_case_by_event(
    event_type: str,
    payment_id: str | None,
    order_id: str | None,
    payment_link_id: str | None,
    original_payment_id: str | None,
):
    case = None
    if event_type == "payment_link.paid":
        case = database.find_case_for_recovery_event(
            order_id=order_id,
            payment_link_id=payment_link_id,
            original_payment_id=original_payment_id,
        )
    else:
        if original_payment_id:
            case = database.find_case_for_recovery_event(
                order_id=order_id,
                original_payment_id=original_payment_id,
            )
        if not case and payment_id:
            case = database.find_case_for_captured_payment(payment_id=payment_id, order_id=None)
        if not case and payment_id is None and order_id:
            case = database.find_case_for_captured_payment(payment_id=None, order_id=order_id)
    if not case or case.get("merchant_account_id") is not None:
        return None
    return case


def execute_recovery_action(case: Dict[str, Any], razorpay_client: PaymentGateway) -> Dict[str, Any]:
    case_id = case["id"]
    payment_id = case["payment_id"]
    action = case.get("action_taken")
    max_retries = int(case.get("max_retries", 0) or 0)
    retry_count = int(case.get("retry_count", 0) or 0)

    if action == "retry_payment":
        if retry_count >= max_retries:
            updated_case = database.update_case_recovery_action(
                case_id=case_id, payment_id=payment_id, recovery_status="ESCALATED",
                action_result=f"RETRY_LIMIT_EXHAUSTED ({retry_count}/{max_retries})",
            )
            database.add_audit_log(
                payment_id=payment_id, case_id=case_id, actor="RECOVERY_ENGINE",
                action="RETRY_BLOCKED_BY_LIMIT",
                details=f"Cannot retry. Retry limit of {max_retries} reached. Case escalated.",
            )
            return {"action": "retry_payment", "status": "escalated", "message": "Retry limit reached. Escalated to manual review.", "case": updated_case}
        updated_case = database.increment_retry_count(case_id, payment_id)
        updated_case = database.update_case_recovery_action(
            case_id=case_id, payment_id=payment_id, recovery_status="PENDING_RETRY",
            action_result=f"RETRY_ATTEMPT_{updated_case['retry_count']}_INITIATED",
        )
        database.add_audit_log(
            payment_id=payment_id, case_id=case_id, actor="RECOVERY_ENGINE", action="RETRY_INITIATED",
            details=f"Bounded retry #{updated_case['retry_count']} of {max_retries} triggered for Rs. {case['amount']}.",
        )
        return {"action": "retry_payment", "status": "retry_initiated", "retry_count": updated_case["retry_count"], "max_retries": max_retries, "case": updated_case}

    if action == "send_payment_reminder":
        if case.get("payment_link_url"):
            return {"action": "send_payment_reminder", "status": "link_already_exists", "payment_link_id": case.get("payment_link_id"), "payment_link_url": case.get("payment_link_url"), "case": case}
        amount_paise = _get_stored_amount_paise(case_id, payment_id)
        if amount_paise is None:
            updated_case = database.update_case_recovery_action(case_id=case_id, payment_id=payment_id, recovery_status="ESCALATED", action_result="RECOVERY_AMOUNT_NOT_FOUND")
            database.add_audit_log(payment_id=payment_id, case_id=case_id, actor="RECOVERY_ENGINE", action="PAYMENT_LINK_CREATION_FAILED", details="Canonical recovery amount could not be read from the database. Case escalated.")
            return {"action": "send_payment_reminder", "status": "escalated", "message": "Recovery amount could not be verified. Escalated to manual review.", "case": updated_case}
        link_payload = {
            "amount": amount_paise,
            "currency": case.get("currency", "INR"),
            "accept_partial": False,
            "description": f"Recovery for Order {case.get('order_id', '')}",
            "customer": {"name": "Test Customer", "email": "customer@example.com", "contact": "+919876543210"},
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "notes": {"case_id": str(case_id), "original_payment_id": payment_id, "order_id": case.get("order_id", ""), "recovery_source": "ai_revenue_recovery"},
        }
        try:
            plink = razorpay_client.create_payment_link(link_payload)
            plink_id = plink.get("id")
            plink_url = plink.get("short_url")
        except PaymentGatewayUnavailable:
            logger.warning("Payment gateway unavailable for case %s; using demo link", case_id)
            plink_id = f"plink_simulated_{case_id}"
            plink_url = f"https://rzp.io/i/simulated_{case_id}"
        except Exception as exc:
            logger.exception("Payment-link creation failed for case %s", case_id)
            updated_case = database.update_case_recovery_action(case_id=case_id, payment_id=payment_id, recovery_status="ESCALATED", action_result=f"PAYMENT_LINK_CREATION_FAILED: {type(exc).__name__}")
            database.add_audit_log(payment_id=payment_id, case_id=case_id, actor="RECOVERY_ENGINE", action="PAYMENT_LINK_CREATION_FAILED", details="Payment link could not be created; case escalated for manual review.")
            return {"action": "send_payment_reminder", "status": "escalated", "message": "Payment link creation failed. Escalated to manual review.", "case": updated_case}
        updated_case = database.update_case_recovery_action(case_id=case_id, payment_id=payment_id, recovery_status="LINK_CREATED", action_result=f"PAYMENT_LINK_CREATED: {plink_url}", payment_link_id=plink_id, payment_link_url=plink_url)
        database.add_audit_log(payment_id=payment_id, case_id=case_id, actor="RECOVERY_ENGINE", action="PAYMENT_LINK_CREATED", details=f"Generated Razorpay recovery link {plink_id} ({plink_url}) for Rs. {case['amount']}. Sent reminder.")
        return {"action": "send_payment_reminder", "status": "link_created", "payment_link_id": plink_id, "payment_link_url": plink_url, "case": updated_case}

    updated_case = database.update_case_recovery_action(case_id=case_id, payment_id=payment_id, recovery_status="ESCALATED", action_result="FROZEN_FOR_MANUAL_OPERATOR_REVIEW")
    database.add_audit_log(payment_id=payment_id, case_id=case_id, actor="RECOVERY_ENGINE", action="CASE_ESCALATED", details="Automated financial recovery halted. Flagged for human support review.")
    return {"action": "escalate_manual_review", "status": "escalated", "case": updated_case}


def process_recovery_success_event(event_type: str, data: dict) -> tuple[Dict | None, bool]:
    payload = data.get("payload", {})
    payment = payload.get("payment", {}).get("entity", {}) or {}
    payment_link = payload.get("payment_link", {}).get("entity", {}) or {}
    notes = payment.get("notes") or payment_link.get("notes") or {}
    order_id = payment.get("order_id") or payment_link.get("order_id")
    original_payment_id = notes.get("original_payment_id")
    merchant = resolve_event_merchant(data)
    if merchant:
        matching_case = find_case_for_recovery_event_scoped(merchant["id"], order_id, payment_link.get("id"), original_payment_id)
    else:
        matching_case = _legacy_recovery_case_by_event(event_type, payment.get("id"), order_id, payment_link.get("id"), original_payment_id)
    if not matching_case:
        logger.warning("Recovery success event has no safely resolvable case; no financial action taken")
        return None, False
    if matching_case.get("recovery_status") == "RECOVERED":
        return matching_case, False
    amount_paise = int(payment.get("amount") or payment_link.get("amount_paid") or payment_link.get("amount") or 0)
    expected_amount_paise = _get_stored_amount_paise(matching_case["id"], matching_case["payment_id"])
    if expected_amount_paise is None or amount_paise != expected_amount_paise:
        logger.warning("Recovery amount mismatch for case %s: expected %s paise, received %s paise", matching_case["id"], expected_amount_paise, amount_paise)
        return matching_case, False
    updated_case, transitioned = database.mark_case_recovered_paisa(
        case_id=matching_case["id"], payment_id=matching_case["payment_id"],
        recovered_amount_paisa=amount_paise, new_payment_id=payment.get("id", "pay_unknown"),
        event_type=event_type, recovery_source="PAYMENT_LINK", event_id=data.get("id"),
    )
    return updated_case, transitioned


def _same_order_candidate_or_audit(
    merchant: dict,
    order_id: str | None,
    amount_paise: int,
    currency: str | None,
    payment_id: str | None,
) -> Dict | None:
    """Select one exact recovery opportunity or record why none was selected."""
    candidates = find_same_order_recovery_candidates_scoped(
        merchant["id"], order_id or "", amount_paise, currency
    )
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        database.add_audit_log(
            payment_id=payment_id or order_id or "payment_unknown",
            merchant_account_id=merchant["id"],
            actor="RECONCILIATION",
            action="SAME_ORDER_RECOVERY_AMBIGUOUS",
            details=(
                "Successful payment was recorded in the lifecycle ledger, but "
                f"{len(candidates)} unresolved recovery opportunities share the "
                "same merchant, order, amount, and currency. No recovery was selected."
            ),
        )
    return None


def reconcile_successful_payment_event(event_type: str, data: dict) -> tuple[Dict | None, bool]:
    if event_type not in {"payment.captured", "order.paid"}:
        return None, False
    payload = data.get("payload", {})
    payment = payload.get("payment", {}).get("entity", {}) or {}
    order = payload.get("order", {}).get("entity", {}) or {}
    payment_id = payment.get("id") or order.get("payment_id")
    order_id = payment.get("order_id") or order.get("id")
    notes = payment.get("notes") or order.get("notes") or {}
    original_payment_id = notes.get("original_payment_id")
    merchant = resolve_event_merchant(data)
    amount_paise = int(payment.get("amount") or order.get("amount_paid") or order.get("amount") or 0)
    currency = payment.get("currency") or order.get("currency")
    recovery_source = None
    if merchant:
        matching_case = None
        if payment_id:
            matching_case = find_case_for_captured_payment_scoped(
                merchant["id"], payment_id, None
            )
        if not matching_case and original_payment_id:
            matching_case = find_case_for_recovery_event_scoped(
                merchant["id"], order_id, None, original_payment_id
            )
        # A new Razorpay payment ID may represent a customer-native retry. It
        # can bridge to an opportunity only after merchant, order, amount and
        # currency resolve to exactly one unresolved candidate.
        if not matching_case and payment_id and order_id:
            matching_case = _same_order_candidate_or_audit(
                merchant, order_id, amount_paise, currency, payment_id
            )
            if matching_case:
                recovery_source = "CUSTOMER_RETRY"
    else:
        matching_case = _legacy_recovery_case_by_event(event_type, payment_id, order_id, None, original_payment_id)
        if not matching_case and payment_id and order_id:
            candidate = database.find_case_for_captured_payment(payment_id=None, order_id=order_id)
            if (
                candidate
                and candidate.get("action_taken") == "retry_payment"
                and candidate.get("recovery_status") == "PENDING_RETRY"
            ):
                matching_case = candidate
    if not matching_case:
        logger.warning("Successful event has no safely resolvable case; no financial action taken")
        return None, False
    expected_amount_paise = _get_stored_amount_paise(matching_case["id"], matching_case["payment_id"])
    expected_currency = matching_case.get("currency")
    if (
        expected_amount_paise is None
        or amount_paise != expected_amount_paise
        or not currency
        or currency != expected_currency
    ):
        logger.warning("Successful payment amount mismatch for case %s: expected %s paise, received %s paise", matching_case["id"], expected_amount_paise, amount_paise)
        return matching_case, False
    if matching_case.get("action_taken") == "retry_payment":
        retry_count = int(matching_case.get("retry_count") or 0)
        max_retries = int(matching_case.get("max_retries") or 0)
        if matching_case.get("recovery_status") != "PENDING_RETRY" or retry_count < 1 or retry_count > max_retries:
            logger.warning("Successful event rejected for invalid retry state on case %s", matching_case["id"])
            return matching_case, False
        recovery_source = recovery_source or "BOUNDED_RETRY"
    elif recovery_source is None:
        # Exact payment-ID reconciliation confirms a capture, but does not
        # rewrite the historical policy decision as an authorized retry.
        recovery_source = "CUSTOMER_RETRY"
    successful_payment_id = payment_id or order_id or "payment_unknown"
    updated_case, transitioned = database.mark_case_recovered_paisa(
        case_id=matching_case["id"], payment_id=matching_case["payment_id"],
        recovered_amount_paisa=amount_paise, new_payment_id=successful_payment_id,
        event_type=event_type, recovery_source=recovery_source,
        event_id=data.get("id"),
    )
    return updated_case, transitioned
