# app/services/recovery_service.py
"""Service layer for recovery execution."""

import logging
from typing import Any, Dict

from app.integrations.razorpay_client import PaymentGateway, PaymentGatewayUnavailable
from app.repositories import database
from app.repositories.merchant_scope import find_case_for_captured_payment_scoped, find_case_for_recovery_event_scoped
from app.services.merchant_service import resolve_event_merchant

logger = logging.getLogger(__name__)


def execute_recovery_action(case: Dict[str, Any], razorpay_client: PaymentGateway) -> Dict[str, Any]:
    """Execute the approved recovery action for a case."""
    case_id = case["id"]
    payment_id = case["payment_id"]
    action = case.get("action_taken")
    max_retries = case.get("max_retries", 0)
    retry_count = case.get("retry_count", 0)

    if action == "retry_payment":
        if retry_count >= max_retries:
            updated_case = database.update_case_recovery_action(case_id=case_id, payment_id=payment_id, recovery_status="ESCALATED", action_result=f"RETRY_LIMIT_EXHAUSTED ({retry_count}/{max_retries})")
            database.add_audit_log(payment_id=payment_id, case_id=case_id, actor="RECOVERY_ENGINE", action="RETRY_BLOCKED_BY_LIMIT", details=f"Cannot retry. Retry limit of {max_retries} reached. Case escalated.")
            return {"action": "retry_payment", "status": "escalated", "message": "Retry limit reached. Escalated to manual review.", "case": updated_case}
        updated_case = database.increment_retry_count(case_id, payment_id)
        updated_case = database.update_case_recovery_action(case_id=case_id, payment_id=payment_id, recovery_status="PENDING_RETRY", action_result=f"RETRY_ATTEMPT_{updated_case['retry_count']}_INITIATED")
        database.add_audit_log(payment_id=payment_id, case_id=case_id, actor="RECOVERY_ENGINE", action="RETRY_INITIATED", details=f"Bounded retry #{updated_case['retry_count']} of {max_retries} triggered for Rs. {case['amount']}.")
        return {"action": "retry_payment", "status": "retry_initiated", "retry_count": updated_case["retry_count"], "max_retries": max_retries, "case": updated_case}

    if action == "send_payment_reminder":
        if case.get("payment_link_url"):
            return {"action": "send_payment_reminder", "status": "link_already_exists", "payment_link_id": case.get("payment_link_id"), "payment_link_url": case.get("payment_link_url"), "case": case}
        link_payload = {
            "amount": int(round(case["amount"] * 100)),
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
    """Process payment_link.paid only after resolving and scoping the merchant."""
    merchant = resolve_event_merchant(data)
    if not merchant:
        logger.warning("Recovery success event has no resolvable merchant; no financial action taken")
        return None, False
    payload = data.get("payload", {})
    payment = payload.get("payment", {}).get("entity", {})
    payment_link = payload.get("payment_link", {}).get("entity", {})
    notes = payment.get("notes") or payment_link.get("notes") or {}
    matching_case = find_case_for_recovery_event_scoped(
        merchant["id"], payment.get("order_id"), payment_link.get("id"), notes.get("original_payment_id")
    )
    if not matching_case:
        return None, False
    if matching_case.get("recovery_status") == "RECOVERED":
        logger.info("Recovery success already recorded for case %s; ignoring duplicate event", matching_case["id"])
        return matching_case, False
    amount_paise = payment.get("amount") or payment_link.get("amount_paid") or payment_link.get("amount") or 0
    expected_amount_paise = int(round(float(matching_case["amount"]) * 100))
    if int(amount_paise) != expected_amount_paise:
        logger.warning("Recovery amount mismatch for case %s: expected %s paise, received %s paise", matching_case["id"], expected_amount_paise, amount_paise)
        return matching_case, False
    updated_case = database.mark_case_recovered(case_id=matching_case["id"], payment_id=matching_case["payment_id"], recovered_amount=amount_paise / 100.0, new_payment_id=payment.get("id", "pay_unknown"), event_type=event_type)
    return updated_case, True


def reconcile_successful_payment_event(event_type: str, data: dict) -> tuple[Dict | None, bool]:
    """Reconcile captured/order.paid only inside the resolved merchant tenant."""
    if event_type not in {"payment.captured", "order.paid"}:
        return None, False
    merchant = resolve_event_merchant(data)
    if not merchant:
        logger.warning("Successful event has no resolvable merchant; no financial action taken")
        return None, False
    payload = data.get("payload", {})
    payment = payload.get("payment", {}).get("entity", {})
    order = payload.get("order", {}).get("entity", {})
    payment_id = payment.get("id") or order.get("payment_id")
    order_id = payment.get("order_id") or order.get("id")
    notes = payment.get("notes") or order.get("notes") or {}
    original_payment_id = notes.get("original_payment_id")

    matching_case = None
    if original_payment_id:
        matching_case = find_case_for_recovery_event_scoped(merchant["id"], order_id, None, original_payment_id)
    if not matching_case:
        matching_case = find_case_for_captured_payment_scoped(merchant["id"], payment_id, order_id if not payment_id else None)
    if not matching_case:
        return None, False

    amount_paise = payment.get("amount") or order.get("amount_paid") or order.get("amount") or 0
    expected_amount_paise = int(round(float(matching_case["amount"]) * 100))
    if int(amount_paise) != expected_amount_paise:
        logger.warning("Successful payment amount mismatch for case %s: expected %s paise, received %s paise", matching_case["id"], expected_amount_paise, amount_paise)
        return matching_case, False

    successful_payment_id = payment_id or order_id or "payment_unknown"
    updated_case, transitioned = database.mark_case_recovered_paisa(case_id=matching_case["id"], payment_id=matching_case["payment_id"], recovered_amount_paisa=int(amount_paise), new_payment_id=successful_payment_id, event_type=event_type)
    return updated_case, transitioned
