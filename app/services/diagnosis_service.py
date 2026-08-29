# app/services/diagnosis_service.py
"""Deterministic classification of failed payment facts."""

from typing import Any


def diagnose_payment_failure(payment: dict[str, Any]) -> dict[str, Any]:
    """Classify a failed payment without making a policy or recovery decision."""
    description = payment.get("error_description", "").lower()
    if (
        payment.get("error_source") == "issuer"
        and payment.get("error_step") == "payment_authorization"
        and "temporary" in description
    ):
        return {"category": "temporary_issuer_failure", "diagnosis": "Temporary issuer-side payment authorization failure", "recoverable": True, "recommended_action": "retry_once", "max_retries": 1}
    if payment.get("error_reason") == "payment_cancelled":
        return {"category": "customer_cancelled", "diagnosis": "Customer cancelled the payment during checkout", "recoverable": True, "recommended_action": "send_payment_reminder", "max_retries": 0}
    return {"category": "unknown_failure", "diagnosis": "Payment failure could not be confidently classified", "recoverable": False, "recommended_action": "manual_review", "max_retries": 0}
