import pytest

from app.services.diagnosis_service import diagnose_payment_failure


def diagnosis(payment: dict) -> str:
    return diagnose_payment_failure(payment)["category"]


def test_explicit_temporary_issuer_failure() -> None:
    assert diagnosis({
        "error_source": "issuer",
        "error_step": "payment_authorization",
        "error_description": "Temporary issue with the issuing bank.",
    }) == "temporary_issuer_failure"


@pytest.mark.parametrize(
    "description",
    [
        "The issuer is temporarily unavailable.",
        "Bank technical failure; please try again.",
        "Gateway timeout while authorizing payment.",
    ],
)
def test_strong_transient_bank_signals_are_classified_as_temporary(
    description: str,
) -> None:
    assert diagnosis({
        "error_source": "issuer",
        "error_step": "payment_authorization",
        "error_description": description,
    }) == "temporary_issuer_failure"


def test_nested_razorpay_error_fields_are_supported() -> None:
    assert diagnosis({
        "error": {
            "code": "GATEWAY_ERROR",
            "description": "Issuer timeout during authorization",
            "source": "issuer",
            "step": "payment_authorization",
            "reason": "timeout",
            "metadata": {"rail": "bank"},
        }
    }) == "temporary_issuer_failure"


@pytest.mark.parametrize(
    "payment",
    [
        {"error_reason": "payment_cancelled"},
        {"error": {"reason": "payment_canceled_by_customer"}},
    ],
)
def test_explicit_customer_cancellation_is_recognized(payment: dict) -> None:
    assert diagnosis(payment) == "customer_cancelled"


def test_explicit_cancellation_takes_precedence_over_transient_text() -> None:
    assert diagnosis({
        "error": {
            "reason": "payment_cancelled",
            "source": "issuer",
            "description": "Temporary bank issue after customer cancellation.",
        }
    }) == "customer_cancelled"


@pytest.mark.parametrize(
    "payment",
    [
        {"error_description": "Checkout modal was dismissed by the user."},
        {"error_description": "Browser was closed before payment completed."},
        {"error_reason": "payment_failed", "error_description": "Payment timed out."},
        {"error_source": "issuer", "error_description": "Bank rejected the payment."},
        {"error_code": "UNKNOWN_NEW_CODE", "error_source": "gateway"},
        {},
        {"error": None},
        {"error": {"description": None, "metadata": ["unexpected", "shape"]}},
        {"error": {"code": {"unexpected": "object"}}},
        None,
    ],
)
def test_ambiguous_missing_or_malformed_failures_remain_unknown(payment) -> None:
    assert diagnosis(payment) == "unknown_failure"


def test_flat_simulation_scenarios_keep_expected_categories() -> None:
    assert diagnosis({
        "error_source": "issuer",
        "error_step": "payment_authorization",
        "error_description": "Temporary issue with the issuing bank.",
    }) == "temporary_issuer_failure"
    assert diagnosis({
        "error_reason": "payment_cancelled",
        "error_description": "Customer cancelled the payment.",
    }) == "customer_cancelled"
    assert diagnosis({
        "error_source": "gateway",
        "error_reason": "unhandled_code_99",
        "error_description": "Unclassified gateway error.",
    }) == "unknown_failure"
