"""Application workflow that coordinates the safe failure-recovery pipeline."""

from typing import Any

from app.integrations.razorpay_client import PaymentGateway
from app.repositories import database
from app.repositories.database import (
    create_or_get_recovery_case,
    update_case_ai_insights,
    update_case_policy,
)
from app.services.ai_service import generate_ai_recovery_insights
from app.services.diagnosis_service import diagnose_payment_failure
from app.services.merchant_service import DEFAULT_MERCHANT_KEY, get_merchant_by_key, register_order
from app.services.policy_service import apply_policy
from app.services.recovery_service import execute_recovery_action


def _build_batch_insights(policy: dict[str, Any]) -> dict[str, str]:
    """Build deterministic communication for the offline batch demo."""
    action = policy.get("action_allowed") or policy.get("decision")

    if action == "retry_payment":
        return {
            "explanation": (
                "The failure matches a temporary issuer issue. "
                "The recovery policy allows a bounded retry."
            ),
            "customer_message": (
                "We couldn't complete your payment due to a temporary "
                "bank issue. We'll retry the payment within the allowed limit."
            ),
            "provider": "template",
        }

    if action == "send_payment_reminder":
        return {
            "explanation": (
                "The payment was cancelled by the customer. "
                "The recovery policy allows a payment reminder."
            ),
            "customer_message": (
                "Your payment was not completed. You can return to checkout "
                "and complete your purchase when you're ready."
            ),
            "provider": "template",
        }

    return {
        "explanation": (
            "The failure could not be safely classified. "
            "The policy requires manual review instead of automated recovery."
        ),
        "customer_message": (
            "We couldn't safely complete your payment. "
            "Our support team will review the issue."
        ),
        "provider": "template",
    }


def _materialize_demo_order(payment: dict[str, Any], gateway: PaymentGateway) -> None:
    """Replace the synthetic demo order with a real Razorpay Test Mode order.

    The dashboard's Bank Glitch button intentionally starts with a synthetic
    payment.failed event. Before that event enters the recovery workflow, give
    it a real Razorpay order so the retry page can safely open Razorpay Checkout.
    Normal webhook traffic is never rewritten because only the simulation's
    ``order_sim_*`` identifiers enter this path.
    """
    order_id = str(payment.get("order_id") or "")
    if not order_id.startswith("order_sim_"):
        return

    amount_paise = int(payment.get("amount") or 0)
    if amount_paise <= 0:
        raise ValueError("Demo retry amount must be positive")

    merchant = get_merchant_by_key(DEFAULT_MERCHANT_KEY)
    if not merchant:
        raise RuntimeError("Default demo merchant is unavailable")

    order = gateway.create_order(
        amount=amount_paise,
        currency=payment.get("currency") or "INR",
        receipt=f"recovery_demo_{payment.get('id')}",
        notes={
            "merchant_key": DEFAULT_MERCHANT_KEY,
            "merchant_account_id": str(merchant["id"]),
            "demo_scenario": "bank_failure",
            "original_payment_id": str(payment.get("id") or ""),
        },
    )
    real_order_id = order.get("id")
    if not real_order_id:
        raise RuntimeError("Razorpay did not return a demo order ID")

    register_order(real_order_id, merchant["id"])
    payment["order_id"] = real_order_id


def _handle_failed_retry(payment: dict[str, Any]) -> dict[str, Any] | None:
    """Consume a failed retry without opening a second recovery case."""
    order_id = payment.get("order_id")
    if not order_id:
        return None

    case = database.find_case_for_captured_payment(payment_id=None, order_id=order_id)
    if not case:
        return None

    if case.get("action_taken") != "retry_payment" or case.get("recovery_status") != "PENDING_RETRY":
        return None

    updated_case = database.update_case_recovery_action(
        case_id=case["id"],
        payment_id=case["payment_id"],
        recovery_status="ESCALATED",
        action_result="RETRY_FAILED_ESCALATED",
    )
    database.add_audit_log(
        payment_id=case["payment_id"],
        case_id=case["id"],
        actor="RECOVERY_ENGINE",
        action="RETRY_FAILED_ESCALATED",
        details="The bounded retry failed. No second automated retry was created; case escalated for manual review.",
    )
    return {
        "case": updated_case,
        "policy": {
            "decision": "ESCALATE_MANUAL_REVIEW",
            "action_allowed": "escalate_manual_review",
            "reason": "Bounded retry failed; stopping rule reached.",
        },
        "recovery": {
            "action": "escalate_manual_review",
            "status": "escalated",
            "case": updated_case,
        },
    }


def process_failed_payment(
    payment: dict[str, Any],
    gateway: PaymentGateway,
    use_ai: bool = True,
) -> dict[str, Any]:
    """Run detect, policy, explanation, and permitted recovery in order."""
    _materialize_demo_order(payment, gateway)

    retry_failure = _handle_failed_retry(payment)
    if retry_failure:
        return retry_failure

    diagnosis = diagnose_payment_failure(payment)

    case, _ = create_or_get_recovery_case(payment, diagnosis)

    policy = apply_policy(case)

    case = update_case_policy(
        case["id"],
        payment["id"],
        policy,
    )

    if use_ai:
        insights = generate_ai_recovery_insights(
            case,
            diagnosis,
            policy,
        )
    else:
        insights = _build_batch_insights(policy)

    case = update_case_ai_insights(
        case["id"],
        payment["id"],
        insights["explanation"],
        insights["customer_message"],
        insights["provider"],
    )

    recovery = execute_recovery_action(case, gateway)
    case = recovery.get("case", case)

    if recovery.get("payment_link_url"):
        message = insights["customer_message"].replace(
            "https://rzp.io/l/recovery",
            recovery["payment_link_url"],
        ).replace("{{payment_link}}", recovery["payment_link_url"])

        if recovery["payment_link_url"] not in message:
            message = f"{message} Link: {recovery['payment_link_url']}"

        case = update_case_ai_insights(
            case["id"],
            payment["id"],
            insights["explanation"],
            message,
            insights["provider"],
        )

    return {
        "case": case,
        "policy": policy,
        "recovery": recovery,
    }
