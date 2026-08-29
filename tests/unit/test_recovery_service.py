from app.repositories import database
from app.services.recovery_service import execute_recovery_action


class FailingGateway:
    def create_payment_link(self, payload: dict) -> dict:
        raise RuntimeError("simulated Razorpay API failure")


def test_payment_link_gateway_failure_escalates_without_simulated_link() -> None:
    payment = {"id": "pay_gateway_failure", "amount": 5000, "currency": "INR", "method": "card"}
    diagnosis = {
        "category": "customer_cancelled",
        "diagnosis": "Customer cancelled checkout.",
        "recoverable": True,
        "recommended_action": "send_payment_reminder",
        "max_retries": 0,
    }
    case, created = database.create_or_get_recovery_case(payment, diagnosis)
    assert created is True
    case["action_taken"] = "send_payment_reminder"

    outcome = execute_recovery_action(case, FailingGateway())

    assert outcome["status"] == "escalated"
    assert outcome["case"]["recovery_status"] == "ESCALATED"
    assert outcome["case"]["payment_link_url"] == ""
    assert outcome["case"]["action_result"] == "PAYMENT_LINK_CREATION_FAILED: RuntimeError"
