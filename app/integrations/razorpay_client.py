# app/integrations/razorpay_client.py
"""Razorpay integration wrapper.

Provides a thin abstraction over the official ``razorpay`` SDK used by the
application. The wrapper forwards calls to the underlying ``razorpay.Client``
instance but gives us a single place to change the integration if required and
helps keep the FastAPI entry-point free of SDK-specific code.

Only the methods required by the current codebase are exposed:

* ``create_order`` – create a Razorpay order (used by the store checkout).
* ``create_payment_link`` – generate a payment link for recovery.
* ``verify_webhook_signature`` – validate Razorpay webhook signatures.
"""

from typing import Any, Dict, Optional, Protocol

import razorpay
from app.core.config import RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET


class PaymentGateway(Protocol):
    def create_order(
        self,
        amount: int,
        receipt: str,
        currency: str = "INR",
        notes: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]: ...

    def create_payment_link(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...
    def verify_webhook_signature(self, body: bytes, signature: str, webhook_secret: str) -> bool: ...


class PaymentGatewayUnavailable(RuntimeError):
    """Raised when a payment operation is requested without gateway credentials."""


class UnavailableRazorpayClient:
    """Allows non-payment endpoints and local simulations to start without secrets."""

    def _unavailable(self) -> None:
        raise PaymentGatewayUnavailable("Razorpay API keys are not configured")

    def create_order(
        self,
        amount: int,
        receipt: str,
        currency: str = "INR",
        notes: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._unavailable()

    def create_payment_link(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._unavailable()

    def verify_webhook_signature(self, body: bytes, signature: str, webhook_secret: str) -> bool:
        self._unavailable()


class RazorpayClient:
    """Thin wrapper around ``razorpay.Client``."""

    def __init__(self) -> None:
        key_id = RAZORPAY_KEY_ID
        key_secret = RAZORPAY_KEY_SECRET
        if not key_id or not key_secret:
            raise ValueError("Razorpay API keys are missing from environment")
        self._client = razorpay.Client(auth=(key_id, key_secret))

    def create_order(
        self,
        amount: int,
        receipt: str,
        currency: str = "INR",
        notes: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a Razorpay order with optional merchant metadata.

        ``amount`` is always in paise. ``notes`` is forwarded to Razorpay so
        webhook payloads can carry stable merchant-routing metadata.
        """
        order_data: Dict[str, Any] = {
            "amount": amount,
            "currency": currency,
            "receipt": receipt,
        }
        if notes:
            order_data["notes"] = notes
        return self._client.order.create(data=order_data)

    def create_payment_link(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create a payment link.

        ``payload`` is passed directly to ``client.payment_link.create``.
        """
        return self._client.payment_link.create(data=payload)

    def verify_webhook_signature(self, body: bytes, signature: str, webhook_secret: str) -> bool:
        """Verify Razorpay webhook signature."""
        self._client.utility.verify_webhook_signature(body.decode("utf-8"), signature, webhook_secret)
        return True


def create_razorpay_client() -> PaymentGateway:
    """Create the SDK-backed client, or a safe unavailable implementation."""
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        return UnavailableRazorpayClient()
    return RazorpayClient()
