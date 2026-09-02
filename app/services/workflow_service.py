"""Application workflow that coordinates the safe failure-recovery pipeline."""

from typing import Any

from app.integrations.razorpay_client import PaymentGateway
from app.repositories.database import (
    create_or_get_recovery_case,
    update_case_ai_insights,
    update_case_policy,
)
from app.services.ai_service import generate_ai_recovery_insights
from app.services.diagnosis_service import diagnose_payment_failure
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


def process_failed_payment(
    payment: dict[str, Any],
    gateway: PaymentGateway,
    use_ai: bool = True,
) -> dict[str, Any]:
    """Run detect, diagnose, policy, explain, and permitted recovery in order.

    AI is enabled for normal/live processing. The deterministic batch demo can
    set use_ai=False so that 50 synthetic cases can be processed quickly
    without making external AI calls.
    """
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
        )

        if recovery["payment_link_url"] not in message:
            message = (
                f"{message} Link: {recovery['payment_link_url']}"
            )

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