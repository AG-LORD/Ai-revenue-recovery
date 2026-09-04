"""Deterministic authorization for a bounded Razorpay retry checkout."""

from __future__ import annotations

from typing import Any

from app.core.config import RAZORPAY_KEY_ID
from app.repositories import database


class RetryAuthorizationError(ValueError):
    """Raised when a recovery case is not eligible for another checkout attempt."""


def authorize_retry_checkout(payment_id: str) -> dict[str, Any]:
    """Authorize the already-approved retry using the original Razorpay order.

    This function does not create a new order, increment retry_count, or mark
    recovery as successful. The policy/recovery engine has already consumed the
    bounded retry before this endpoint can be reached. Razorpay webhooks remain
    authoritative for the final payment outcome.
    """
    case = database.get_case_by_payment_id(payment_id)
    if not case:
        raise RetryAuthorizationError("Recovery case not found")

    if case.get("action_taken") != "retry_payment":
        raise RetryAuthorizationError("This case is not approved for retry")

    if case.get("recovery_status") != "PENDING_RETRY":
        raise RetryAuthorizationError("Retry is no longer pending")

    retry_count = int(case.get("retry_count") or 0)
    max_retries = int(case.get("max_retries") or 0)
    if retry_count < 1 or retry_count > max_retries:
        raise RetryAuthorizationError("Retry guardrail does not permit checkout")

    order_id = case.get("order_id")
    if not order_id:
        raise RetryAuthorizationError("Original Razorpay order is missing")

    amount = case.get("amount")
    if amount is None or float(amount) <= 0:
        raise RetryAuthorizationError("Recovery amount is invalid")

    currency = case.get("currency") or "INR"
    amount_paise = int(round(float(amount) * 100))

    return {
        "status": "authorized",
        "payment_id": payment_id,
        "order_id": order_id,
        "amount": amount_paise,
        "currency": currency,
        "key_id": RAZORPAY_KEY_ID,
        "retry_count": retry_count,
        "max_retries": max_retries,
        "case_id": case["id"],
    }
