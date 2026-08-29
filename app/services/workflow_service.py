"""Application workflow that coordinates the safe failure-recovery pipeline."""

from typing import Any

from app.integrations.razorpay_client import PaymentGateway
from app.repositories.database import create_or_get_recovery_case, update_case_ai_insights, update_case_policy
from app.services.ai_service import generate_ai_recovery_insights
from app.services.diagnosis_service import diagnose_payment_failure
from app.services.policy_service import apply_policy
from app.services.recovery_service import execute_recovery_action


def process_failed_payment(payment: dict[str, Any], gateway: PaymentGateway) -> dict[str, Any]:
    """Run detect, diagnose, policy, explain, and permitted recovery in order."""
    diagnosis = diagnose_payment_failure(payment)
    case, _ = create_or_get_recovery_case(payment, diagnosis)
    policy = apply_policy(case)
    case = update_case_policy(case["id"], payment["id"], policy)
    insights = generate_ai_recovery_insights(case, diagnosis, policy)
    case = update_case_ai_insights(
        case["id"], payment["id"], insights["explanation"], insights["customer_message"], insights["provider"]
    )
    recovery = execute_recovery_action(case, gateway)
    case = recovery.get("case", case)
    if recovery.get("payment_link_url"):
        message = insights["customer_message"].replace("https://rzp.io/l/recovery", recovery["payment_link_url"])
        if recovery["payment_link_url"] not in message:
            message = f"{message} Link: {recovery['payment_link_url']}"
        case = update_case_ai_insights(case["id"], payment["id"], insights["explanation"], message, insights["provider"])
    return {"case": case, "policy": policy, "recovery": recovery}
