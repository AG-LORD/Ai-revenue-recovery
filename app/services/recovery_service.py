# app/services/recovery_service.py
"""Service layer for recovery execution.

This module adapts the original `recovery.py` logic to the new service-oriented
architecture. It removes direct imports of `razorpay` and instead depends on the
`RazorpayClient` wrapper defined in `app.integrations.razorpay_client`.
All database interactions are performed via functions from
`app.repositories.database`.
"""

import logging
from typing import Any, Dict

# Local imports
from app.integrations.razorpay_client import PaymentGateway, PaymentGatewayUnavailable
from app.repositories import database


logger = logging.getLogger(__name__)


def execute_recovery_action(case: Dict[str, Any], razorpay_client: PaymentGateway) -> Dict[str, Any]:
    """Execute the approved recovery action for a case.

    The gateway is injected, keeping the SDK outside business logic.
    """
    case_id = case["id"]
    payment_id = case["payment_id"]
    # Recovery is intentionally unable to derive an action from diagnosis.
    # ``action_taken`` is written only by the deterministic policy gate.
    action = case.get("action_taken")
    max_retries = case.get("max_retries", 0)
    retry_count = case.get("retry_count", 0)

    # ACTION 1: Controlled Retry (Temporary Issuer Failure)
    if action == "retry_payment":
        if retry_count >= max_retries:
            updated_case = database.update_case_recovery_action(
                case_id=case_id,
                payment_id=payment_id,
                recovery_status="ESCALATED",
                action_result=f"RETRY_LIMIT_EXHAUSTED ({retry_count}/{max_retries})",
            )
            database.add_audit_log(
                payment_id=payment_id,
                case_id=case_id,
                actor="RECOVERY_ENGINE",
                action="RETRY_BLOCKED_BY_LIMIT",
                details=f"Cannot retry. Retry limit of {max_retries} reached. Case escalated.",
            )
            return {
                "action": "retry_payment",
                "status": "escalated",
                "message": "Retry limit reached. Escalated to manual review.",
                "case": updated_case,
            }

        # Increment retry count
        updated_case = database.increment_retry_count(case_id, payment_id)

        # Record retry execution in DB
        updated_case = database.update_case_recovery_action(
            case_id=case_id,
            payment_id=payment_id,
            recovery_status="PENDING_RETRY",
            action_result=f"RETRY_ATTEMPT_{updated_case['retry_count']}_INITIATED",
        )

        database.add_audit_log(
            payment_id=payment_id,
            case_id=case_id,
            actor="RECOVERY_ENGINE",
            action="RETRY_INITIATED",
            details=f"Bounded retry #{updated_case['retry_count']} of {max_retries} triggered for Rs. {case['amount']}.",
        )

        return {
            "action": "retry_payment",
            "status": "retry_initiated",
            "retry_count": updated_case["retry_count"],
            "max_retries": max_retries,
            "case": updated_case,
        }

    # ACTION 2: Send Payment Reminder / Link (Customer Cancellation)
    elif action == "send_payment_reminder":
        if case.get("payment_link_url"):
            return {
                "action": "send_payment_reminder",
                "status": "link_already_exists",
                "payment_link_id": case.get("payment_link_id"),
                "payment_link_url": case.get("payment_link_url"),
                "case": case,
            }

        # Generate payment link via the Razorpay client wrapper
        link_payload = {
            "amount": int(case["amount"] * 100),  # paise
            "currency": case.get("currency", "INR"),
            "accept_partial": False,
            "description": f"Recovery for Order {case.get('order_id', '')}",
            "customer": {
                "name": "Test Customer",
                "email": "customer@example.com",
                "contact": "+919876543210",
            },
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "notes": {
                "case_id": str(case_id),
                "original_payment_id": payment_id,
                "order_id": case.get("order_id", ""),
                "recovery_source": "ai_revenue_recovery",
            },
        }
        try:
            plink = razorpay_client.create_payment_link(link_payload)
            plink_id = plink.get("id")
            plink_url = plink.get("short_url")
        except PaymentGatewayUnavailable:
            # Simulation remains deterministic and never retries a gateway call.
            logger.warning("Payment gateway unavailable for case %s; using demo link", case_id)
            plink_id = f"plink_simulated_{case_id}"
            plink_url = f"https://rzp.io/i/simulated_{case_id}"
        except Exception as exc:
            logger.exception("Payment-link creation failed for case %s", case_id)
            updated_case = database.update_case_recovery_action(
                case_id=case_id,
                payment_id=payment_id,
                recovery_status="ESCALATED",
                action_result=f"PAYMENT_LINK_CREATION_FAILED: {type(exc).__name__}",
            )
            database.add_audit_log(
                payment_id=payment_id,
                case_id=case_id,
                actor="RECOVERY_ENGINE",
                action="PAYMENT_LINK_CREATION_FAILED",
                details="Payment link could not be created; case escalated for manual review.",
            )
            return {
                "action": "send_payment_reminder",
                "status": "escalated",
                "message": "Payment link creation failed. Escalated to manual review.",
                "case": updated_case,
            }

        # Update DB with link details
        updated_case = database.update_case_recovery_action(
            case_id=case_id,
            payment_id=payment_id,
            recovery_status="LINK_CREATED",
            action_result=f"PAYMENT_LINK_CREATED: {plink_url}",
            payment_link_id=plink_id,
            payment_link_url=plink_url,
        )

        database.add_audit_log(
            payment_id=payment_id,
            case_id=case_id,
            actor="RECOVERY_ENGINE",
            action="PAYMENT_LINK_CREATED",
            details=f"Generated Razorpay recovery link {plink_id} ({plink_url}) for Rs. {case['amount']}. Sent reminder.",
        )

        return {
            "action": "send_payment_reminder",
            "status": "link_created",
            "payment_link_id": plink_id,
            "payment_link_url": plink_url,
            "case": updated_case,
        }

    # ACTION 3: Escalate to Manual Review (Unknown / Non‑recoverable)
    else:
        updated_case = database.update_case_recovery_action(
            case_id=case_id,
            payment_id=payment_id,
            recovery_status="ESCALATED",
            action_result="FROZEN_FOR_MANUAL_OPERATOR_REVIEW",
        )
        database.add_audit_log(
            payment_id=payment_id,
            case_id=case_id,
            actor="RECOVERY_ENGINE",
            action="CASE_ESCALATED",
            details="Automated financial recovery halted. Flagged for human support review.",
        )
        return {
            "action": "escalate_manual_review",
            "status": "escalated",
            "case": updated_case,
        }


def process_recovery_success_event(event_type: str, data: dict) -> tuple[Dict, bool]:
    """Process a successful recovery webhook event.

    Match the gateway event to the original case, then record the successful
    recovery. This is intentionally separate from policy: success events do
    not authorize a new financial action.
    """
    payload = data.get("payload", {})
    payment = payload.get("payment", {}).get("entity", {})
    payment_link = payload.get("payment_link", {}).get("entity", {})
    notes = payment.get("notes") or payment_link.get("notes") or {}
    matching_case = database.find_case_for_recovery_event(
        order_id=payment.get("order_id"),
        payment_link_id=payment_link.get("id"),
        original_payment_id=notes.get("original_payment_id"),
    )
    if not matching_case:
        return None, False
    if matching_case.get("recovery_status") == "RECOVERED":
        logger.info(
            "Recovery success already recorded for case %s; ignoring duplicate event",
            matching_case["id"],
        )
        return matching_case, False
    amount_paise = payment.get("amount") or payment_link.get("amount_paid") or payment_link.get("amount") or 0
    updated_case = database.mark_case_recovered(
        case_id=matching_case["id"],
        payment_id=matching_case["payment_id"],
        recovered_amount=amount_paise / 100.0,
        new_payment_id=payment.get("id", "pay_unknown"),
        event_type=event_type,
    )
    return updated_case, True
