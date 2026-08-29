"""Template-only communication layer; it cannot decide or execute recovery."""

from typing import Any

from app.core.config import AI_ENABLED, AI_PROVIDER


def _validate(content: str, policy: dict[str, Any]) -> bool:
    """Reject language that promises action forbidden by the policy fact."""
    forbidden = ("charging your card", "automatically charging", "retrying automatically")
    if policy.get("decision") in {"ESCALATE_MANUAL_REVIEW", "BLOCKED_BY_GUARDRAIL", "ALLOW_REMINDER"}:
        return not any(phrase in content.lower() for phrase in forbidden)
    return True


def generate_ai_recovery_insights(
    case: dict[str, Any], diagnosis: dict[str, Any], policy: dict[str, Any], payment_link: str | None = None
) -> dict[str, Any]:
    """Produce deterministic explanations from immutable diagnosis and policy facts."""
    amount = case.get("amount", 0)
    category = diagnosis.get("category")
    decision = policy.get("decision")
    if decision == "ALLOW_RETRY" and category == "temporary_issuer_failure":
        explanation = f"A temporary issuer authorization failure was identified. Policy permits one bounded retry ({case.get('retry_count', 0) + 1}/{case.get('max_retries', 0)})."
        message = f"We noticed a temporary bank issue with your payment of Rs. {amount}. We are re-attempting it once; no action is required."
    elif decision == "ALLOW_REMINDER" and category == "customer_cancelled":
        link = payment_link or case.get("payment_link_url") or "https://rzp.io/l/recovery"
        explanation = "Checkout was cancelled. Policy prohibits automatic charging and permits only a customer-initiated payment link."
        message = f"Your order of Rs. {amount} is ready to complete whenever you are: {link}"
    else:
        explanation = "The payment could not be safely recovered automatically and has been routed for manual review."
        message = f"Your payment of Rs. {amount} could not be processed. Our support team will assist if needed."
    return {
        "explanation": explanation,
        "customer_message": message,
        "recommended_action": policy.get("action_allowed", "escalate_manual_review"),
        "policy_decision": decision,
        "ai_generated": False,
        "provider": AI_PROVIDER,
        "safety_validated": _validate(explanation, policy) and _validate(message, policy),
        "ai_enabled": AI_ENABLED,
    }
