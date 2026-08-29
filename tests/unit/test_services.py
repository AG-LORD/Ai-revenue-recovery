from unittest.mock import patch

from app.core.config import AI_ENABLED, AI_PROVIDER
from app.services.ai_service import generate_ai_recovery_insights
from app.services.diagnosis_service import diagnose_payment_failure
from app.services.policy_service import apply_policy


def test_issuer_failure_is_diagnosed_and_policy_allows_one_retry() -> None:
    diagnosis = diagnose_payment_failure(
        {
            "error_source": "issuer",
            "error_step": "payment_authorization",
            "error_description": "Temporary issue",
        }
    )

    policy = apply_policy(
        {
            "diagnosis_category": diagnosis["category"],
            "is_recoverable": True,
            "retry_count": 0,
            "max_retries": 1,
        }
    )

    assert diagnosis["category"] == "temporary_issuer_failure"
    assert policy["action_allowed"] == "retry_payment"


def test_policy_blocks_an_exhausted_retry() -> None:
    policy = apply_policy(
        {
            "diagnosis_category": "temporary_issuer_failure",
            "is_recoverable": True,
            "retry_count": 1,
            "max_retries": 1,
        }
    )

    assert policy["decision"] == "BLOCKED_BY_GUARDRAIL"
    assert policy["action_allowed"] == "escalate_manual_review"


def test_cancellation_can_only_create_customer_initiated_link() -> None:
    diagnosis = {"category": "customer_cancelled"}

    policy = apply_policy(
        {
            "diagnosis_category": "customer_cancelled",
            "is_recoverable": True,
            "retry_count": 0,
            "max_retries": 0,
        }
    )

    fake_gemini_output = {
        "explanation": (
            "The customer cancelled checkout, so a payment link "
            "is permitted."
        ),
        "customer_message": (
            "Your order is ready whenever you are: "
            "https://rzp.io/l/recovery"
        ),
    }

    # Mock Gemini so unit tests never make a real API request.
    with patch(
        "app.services.ai_service._generate_with_gemini",
        return_value=fake_gemini_output,
    ):
        insight = generate_ai_recovery_insights(
            {"amount": 500},
            diagnosis,
            policy,
        )

    # Deterministic policy remains authoritative.
    assert policy["action_allowed"] == "send_payment_reminder"
    assert policy["decision"] == "ALLOW_REMINDER"

    # Gemini generated the communication.
    assert insight["ai_generated"] is True
    assert insight["provider"] == "gemini"

    # Gemini cannot change the financial decision.
    assert insight["recommended_action"] == "send_payment_reminder"
    assert insight["policy_decision"] == "ALLOW_REMINDER"

    # Safety validation still applies to AI output.
    assert "automatically charging" not in insight["customer_message"].lower()
    assert insight["safety_validated"] is True

def test_gemini_failure_falls_back_to_deterministic_template() -> None:
    diagnosis = {"category": "customer_cancelled"}

    policy = apply_policy(
        {
            "diagnosis_category": "customer_cancelled",
            "is_recoverable": True,
            "retry_count": 0,
            "max_retries": 0,
        }
    )

    with patch(
        "app.services.ai_service._generate_with_gemini",
        side_effect=RuntimeError("Gemini unavailable"),
    ):
        insight = generate_ai_recovery_insights(
            {"amount": 500},
            diagnosis,
            policy,
        )

    # Recovery policy remains unchanged.
    assert insight["recommended_action"] == "send_payment_reminder"
    assert insight["policy_decision"] == "ALLOW_REMINDER"

    # System safely falls back to deterministic communication.
    assert insight["ai_generated"] is False
    assert insight["provider"] == "template"
    assert insight["ai_enabled"] is True

    # Fallback output still passes the safety guardrail.
    assert insight["safety_validated"] is True
    assert "automatically charging" not in insight["customer_message"].lower()
def test_ai_configuration_uses_gemini() -> None:
    from app.core.config import GEMINI_MODEL

    assert AI_ENABLED is True
    assert AI_PROVIDER == "gemini"
    assert GEMINI_MODEL == "gemini-3.6-flash"


def test_monetary_values_stored_as_paisa_without_floating_point_error():
    # Test that storing and retrieving monetary values does not introduce
    # floating point errors.
    from app.repositories.database import (
        create_or_get_recovery_case,
        get_case_by_payment_id,
        get_connection,
        get_recovery_metrics,
        mark_case_recovered,
    )

    payment = {
        "id": "test_payment_1",
        "amount": 12345,  # paisa = Rs. 123.45
        "currency": "INR",
        "method": "card",
    }

    diagnosis = diagnose_payment_failure(
        {
            "error_source": "issuer",
            "error_step": "payment_authorization",
            "error_description": "Temporary issue",
        }
    )

    policy = apply_policy(
        {
            "diagnosis_category": diagnosis["category"],
            "is_recoverable": True,
            "retry_count": 0,
            "max_retries": 1,
        }
    )

    # Create first recovery case.
    case, created = create_or_get_recovery_case(
        payment,
        diagnosis,
    )

    assert created is True
    assert abs(case["amount"] - 123.45) < 0.001

    # Retrieve the case.
    retrieved_case = get_case_by_payment_id(
        payment["id"]
    )

    assert retrieved_case is not None
    assert abs(retrieved_case["amount"] - 123.45) < 0.001

    # Create second recovery case.
    payment2 = {
        "id": "test_payment_2",
        "amount": 67890,  # paisa = Rs. 678.90
        "currency": "INR",
        "method": "wallet",
    }

    case2, created2 = create_or_get_recovery_case(
        payment2,
        diagnosis,
    )

    assert created2 is True

    # Verify total revenue at risk.
    metrics = get_recovery_metrics()

    assert abs(
        metrics["total_revenue_at_risk"] - 802.35
    ) < 0.001

    # Initially nothing is recovered.
    assert metrics["total_revenue_recovered"] == 0.0

    # Recover the first case.
    recovered_case = mark_case_recovered(
        case["id"],
        payment["id"],
        123.45,
        "pay_new_1",
        "payment_link.paid",
    )

    assert recovered_case["recovered_amount"] == 123.45

    # Verify recovered metrics.
    metrics2 = get_recovery_metrics()

    assert abs(
        metrics2["total_revenue_at_risk"] - 802.35
    ) < 0.001

    assert abs(
        metrics2["total_revenue_recovered"] - 123.45
    ) < 0.001

    # Clean up test cases.
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM recovery_cases WHERE payment_id IN (?, ?)",
        (
            payment["id"],
            payment2["id"],
        ),
    )

    conn.commit()
    conn.close()