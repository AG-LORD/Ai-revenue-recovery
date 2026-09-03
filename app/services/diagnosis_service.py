"""Deterministic classification of payment failure facts.

Diagnosis explains the failure only. It does not authorize a retry or any
other financial action; the policy service remains the authority for that.
"""

from collections.abc import Mapping
from typing import Any


def _text(value: Any) -> str:
    """Safely normalize an optional payload value for deterministic matching."""
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value).strip().lower()
    return ""


def _failure_signals(payment: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """Extract normalized failure fields from flat and nested Razorpay shapes."""
    nested_error = payment.get("error")
    error = nested_error if isinstance(nested_error, Mapping) else {}
    metadata = error.get("metadata")
    metadata_text = " ".join(
        _text(value)
        for value in metadata.values()
    ) if isinstance(metadata, Mapping) else _text(metadata)

    code = _text(error.get("code") or payment.get("error_code"))
    description = _text(
        error.get("description")
        or payment.get("error_description")
    )
    reason = _text(error.get("reason") or payment.get("error_reason"))
    source = _text(error.get("source") or payment.get("error_source"))
    step = _text(error.get("step") or payment.get("error_step"))
    combined = " ".join((code, description, reason, source, step, metadata_text))
    return combined, reason, source, step


def _is_customer_cancellation(payment: Mapping[str, Any], reason: str, combined: str) -> bool:
    """Recognize explicit customer cancellation, never browser/UI dismissal."""
    cancellation_values = {
        "payment_cancelled",
        "payment_canceled",
        "customer_cancelled",
        "customer_canceled",
        "payment_cancelled_by_customer",
        "payment_canceled_by_customer",
    }
    explicit_reason = reason in cancellation_values
    explicit_code = _text(payment.get("error_code")) in cancellation_values
    return explicit_reason or explicit_code


def _is_strong_temporary_failure(combined: str, source: str, step: str) -> bool:
    """Require both a technical/issuer context and a transient signal."""
    context_terms = ("issuer", "bank", "gateway", "technical")
    transient_terms = (
        "temporary",
        "temporarily unavailable",
        "transient",
        "timeout",
        "timed out",
        "service unavailable",
        "technical failure",
        "technical issue",
    )
    has_context = source in {"issuer", "bank", "gateway"} or any(
        term in combined for term in context_terms
    )
    has_transient_signal = any(term in combined for term in transient_terms)
    authorization_or_technical_step = (
        not step
        or step in {"payment_authorization", "payment_authentication", "payment_capture"}
        or "timeout" in combined
        or "technical" in combined
    )
    return has_context and has_transient_signal and authorization_or_technical_step


def diagnose_payment_failure(payment: dict[str, Any]) -> dict[str, Any]:
    """Classify a failed payment using explicit, conservative precedence.

    Customer cancellation wins first because it must never be converted into an
    automatic retry. Strong transient issuer/technical evidence is considered
    second. Ambiguous or malformed failures remain manual-review candidates.
    """
    if not isinstance(payment, Mapping):
        payment = {}

    combined, reason, source, step = _failure_signals(payment)

    # 1. Explicit cancellation: do not infer this from timeout, dismissal, or
    # generic failure text.
    if _is_customer_cancellation(payment, reason, combined):
        return {
            "category": "customer_cancelled",
            "diagnosis": "Customer explicitly cancelled the payment",
            "reason": "An explicit customer-cancellation signal was present.",
            "confidence": "high",
            "recoverable": True,
            "recommended_action": "send_payment_reminder",
            "max_retries": 0,
        }

    # 2. Conservative transient classification: retry eligibility requires
    # issuer/bank/gateway context plus a clear temporary technical signal.
    if _is_strong_temporary_failure(combined, source, step):
        return {
            "category": "temporary_issuer_failure",
            "diagnosis": "Temporary issuer or payment-rail technical failure",
            "reason": "A transient issuer, bank, or gateway signal was detected.",
            "confidence": "high",
            "recoverable": True,
            "recommended_action": "retry_once",
            "max_retries": 1,
        }

    # 3. Everything uncertain is escalated by policy for manual review.
    return {
        "category": "unknown_failure",
        "diagnosis": "Payment failure could not be confidently classified",
        "reason": "No explicit cancellation or strong transient issuer signal was found.",
        "confidence": "not applicable",
        "recoverable": False,
        "recommended_action": "manual_review",
        "max_retries": 0,
    }
