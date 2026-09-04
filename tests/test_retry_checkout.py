from app.services import retry_service


def _case(**overrides):
    case = {
        "id": 101,
        "payment_id": "pay_failed_101",
        "order_id": "order_101",
        "amount": 1799.0,
        "currency": "INR",
        "action_taken": "retry_payment",
        "recovery_status": "PENDING_RETRY",
        "retry_count": 1,
        "max_retries": 1,
    }
    case.update(overrides)
    return case


def test_authorize_retry_uses_existing_order(monkeypatch):
    monkeypatch.setattr(retry_service.database, "get_case_by_payment_id", lambda _: _case())

    result = retry_service.authorize_retry_checkout("pay_failed_101")

    assert result["status"] == "authorized"
    assert result["order_id"] == "order_101"
    assert result["amount"] == 179900
    assert result["retry_count"] == 1
    assert result["max_retries"] == 1


def test_retry_requires_pending_retry_state(monkeypatch):
    monkeypatch.setattr(
        retry_service.database,
        "get_case_by_payment_id",
        lambda _: _case(recovery_status="RECOVERED"),
    )

    try:
        retry_service.authorize_retry_checkout("pay_failed_101")
    except retry_service.RetryAuthorizationError as exc:
        assert "no longer pending" in str(exc)
    else:
        raise AssertionError("Expected retry authorization to be rejected")


def test_retry_guardrail_cannot_exceed_max(monkeypatch):
    monkeypatch.setattr(
        retry_service.database,
        "get_case_by_payment_id",
        lambda _: _case(retry_count=2, max_retries=1),
    )

    try:
        retry_service.authorize_retry_checkout("pay_failed_101")
    except retry_service.RetryAuthorizationError as exc:
        assert "guardrail" in str(exc)
    else:
        raise AssertionError("Expected retry guardrail to reject checkout")


def test_retry_requires_original_order(monkeypatch):
    monkeypatch.setattr(
        retry_service.database,
        "get_case_by_payment_id",
        lambda _: _case(order_id=None),
    )

    try:
        retry_service.authorize_retry_checkout("pay_failed_101")
    except retry_service.RetryAuthorizationError as exc:
        assert "order" in str(exc).lower()
    else:
        raise AssertionError("Expected missing order to reject checkout")
