from app.services import retry_service


def test_authorized_retry_response_contains_original_order_and_amount(monkeypatch):
    monkeypatch.setattr(
        retry_service.database,
        "get_case_by_payment_id",
        lambda _: {
            "id": 201,
            "payment_id": "pay_failed_201",
            "order_id": "order_201",
            "amount": 1799.0,
            "currency": "INR",
            "action_taken": "retry_payment",
            "recovery_status": "PENDING_RETRY",
            "retry_count": 1,
            "max_retries": 1,
        },
    )

    result = retry_service.authorize_retry_checkout("pay_failed_201")

    assert result["order_id"] == "order_201"
    assert result["amount"] == 179900
    assert result["retry_count"] == 1
    assert result["max_retries"] == 1
