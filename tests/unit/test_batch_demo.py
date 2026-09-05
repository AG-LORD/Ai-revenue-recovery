import inspect
from unittest.mock import patch

from app.integrations.razorpay_client import UnavailableRazorpayClient
from app.repositories import database
from app.services.workflow_service import process_failed_payment
from scripts import run_batch_demo


def fake_nim_response(*args, **kwargs):
    return {
        "explanation": "Payment failure analyzed using the approved recovery policy.",
        "customer_message": "Please complete your payment using the provided payment link.",
    }


def test_generator_is_deterministic_and_has_fifty_recognized_events() -> None:
    first = run_batch_demo.generate_synthetic_failures()
    second = run_batch_demo.generate_synthetic_failures()

    assert first == second
    assert len(first) == 50
    assert {event["error_reason"] for event in first} <= {
        "payment_failed",
        "payment_cancelled",
        "unhandled_code_99",
    }


def test_generator_does_not_insert_database_records_directly() -> None:
    source = inspect.getsource(run_batch_demo.generate_synthetic_failures)
    assert "INSERT INTO" not in source
    assert "sqlite" not in source.lower()


@patch("app.services.ai_service._generate_with_nim", side_effect=fake_nim_response)
def test_batch_processes_through_workflow_and_calculates_persisted_metrics(
    mock_nim,
) -> None:
    report = run_batch_demo.process_batch(
        run_batch_demo.generate_synthetic_failures()
    )
    cases = database.get_all_recovery_cases()

    assert report["payments_processed"] == 50
    assert report["revenue_at_risk"] == 22250.0
    assert report["revenue_recovered"] == 4250.0
    assert round(report["recovery_rate"], 1) == 19.1
    assert report["policy_violations"] == 0
    assert report["payments_processed"] == len(cases)
    assert sum(report["diagnosis_counts"].values()) == 50
    assert report["revenue_at_risk"] == sum(case["amount"] for case in cases)
    assert report["revenue_recovered"] == sum(
        case["recovered_amount"] for case in cases
    )
    assert report["successful_recoveries"] == sum(
        case["recovery_status"] == "RECOVERED" for case in cases
    )
    assert report["revenue_recovered"] <= report["revenue_at_risk"]
    assert report["recovery_rate"] <= 100.0
    assert report["audit_events"] > 0
    assert report["mode"] == "synthetic_demo"
    assert report["recovery_mode"] == "synthetic_confirmation"
    assert report["seed"] == run_batch_demo.DEFAULT_SEED
    assert report["revenue_at_risk"] == (
        report["revenue_recovered"] + report["revenue_still_at_risk"]
    )
    expected_rate = report["revenue_recovered"] / report["revenue_at_risk"] * 100
    assert report["recovery_rate"] == expected_rate
    assert report["policy_violations"] == 0
    assert report["retry_attempts"] <= report["bounded_retries"] * 1
    assert all(
        not str(case.get("payment_link_url") or "").startswith("https://rzp.io/")
        for case in cases
        if case["payment_id"].startswith("demo_batch_v1_")
    )

    # Make sure the batch test did not accidentally call real NIM.
    assert mock_nim.call_count == 0


@patch("app.services.ai_service._generate_with_nim", side_effect=fake_nim_response)
def test_reset_demo_records_preserves_unrelated_cases(mock_nim) -> None:
    unrelated_payment = {
        "id": "unrelated_payment",
        "order_id": "unrelated_order",
        "amount": 50000,
        "currency": "INR",
        "error_source": "issuer",
        "error_step": "payment_authorization",
        "error_description": "Temporary issue with the issuing bank.",
    }

    with patch("app.services.ai_service.AI_ENABLED", True), patch(
        "app.services.ai_service.AI_PROVIDER", "nim"
    ), patch("app.services.ai_service.NIM_API_KEY", "test-key"):
        process_failed_payment(
            unrelated_payment,
            UnavailableRazorpayClient(),
        )

    run_batch_demo.process_batch(
        run_batch_demo.generate_synthetic_failures()
    )

    removed = run_batch_demo.reset_demo_records()

    assert removed["recovery_cases"] == 50
    assert database.get_case_by_payment_id("unrelated_payment") is not None
    assert not [
        case
        for case in database.get_all_recovery_cases()
        if case["payment_id"].startswith("demo_batch_v1_")
    ]

    # 1 unrelated case + 50 demo cases; the batch itself is offline.
    assert mock_nim.call_count == 1


def test_batch_report_is_reproducible_for_same_seed() -> None:
    events = run_batch_demo.generate_synthetic_failures(seed=12345)
    first = run_batch_demo.process_batch(events, seed=12345)
    second = run_batch_demo.process_batch(
        run_batch_demo.generate_synthetic_failures(seed=12345),
        seed=12345,
    )

    assert first == second