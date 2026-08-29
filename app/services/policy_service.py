# app/services/policy_service.py
"""Authoritative deterministic policy gate for financial recovery actions."""

def apply_policy(case: dict) -> dict:
    """Evaluate policy and expose `policy_decision`.

    Args:
        case: The recovery case dictionary.
    Returns:
        A dict containing the original policy evaluation fields plus
        a top‑level ``policy_decision`` entry mirroring ``decision``.
    """
    diagnosis_category = case.get("diagnosis_category", "unknown_failure")
    recoverable = bool(case.get("is_recoverable", 0))
    retry_count = case.get("retry_count", 0)
    max_retries = case.get("max_retries", 0)
    if max_retries > 0 and retry_count >= max_retries:
        result = {"decision": "BLOCKED_BY_GUARDRAIL", "action_allowed": "escalate_manual_review", "next_status": "ESCALATED", "is_safe": True, "reason": f"Retry limit reached ({retry_count}/{max_retries}).", "guardrail_triggered": "MAX_RETRY_LIMIT_EXCEEDED"}
    elif not recoverable or diagnosis_category == "unknown_failure":
        result = {"decision": "ESCALATE_MANUAL_REVIEW", "action_allowed": "escalate_manual_review", "next_status": "ESCALATED", "is_safe": True, "reason": "Payment failure is unclassified or non-recoverable.", "guardrail_triggered": "NON_RECOVERABLE_POLICY"}
    elif diagnosis_category == "temporary_issuer_failure":
        result = {"decision": "ALLOW_RETRY", "action_allowed": "retry_payment", "next_status": "PENDING_RETRY", "is_safe": True, "reason": f"Temporary issuer issue permits one controlled retry ({retry_count + 1}/{max_retries}).", "guardrail_triggered": None}
    elif diagnosis_category == "customer_cancelled":
        result = {"decision": "ALLOW_REMINDER", "action_allowed": "send_payment_reminder", "next_status": "PENDING_REMINDER", "is_safe": True, "reason": "Cancellation permits only a payment link/reminder.", "guardrail_triggered": "AUTOMATIC_RETRY_FORBIDDEN_FOR_CANCELLATIONS"}
    else:
        result = {"decision": "ESCALATE_MANUAL_REVIEW", "action_allowed": "escalate_manual_review", "next_status": "ESCALATED", "is_safe": True, "reason": "Unrecognized category.", "guardrail_triggered": "DEFAULT_SAFETY_FALLBACK"}
    # Add compatibility key expected by the router/tests
    result["policy_decision"] = result.get("decision")
    return result
