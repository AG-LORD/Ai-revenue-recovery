import inspect

from app.integrations.razorpay_client import UnavailableRazorpayClient
from app.repositories import database
from app.services.workflow_service import process_failed_payment
from scripts import run_batch_demo


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


def test_batch_processes_through_workflow_and_calculates_persisted_metrics() -> None:
    report = run_batch_demo.process_batch(run_batch_demo.generate_synthetic_failures())
    cases = database.get_all_recovery_cases()

    assert report["payments_processed"] == 50
    assert report["payments_processed"] == len(cases)
    assert sum(report["diagnosis_counts"].values()) == 50
    assert report["revenue_at_risk"] == sum(case["amount"] for case in cases)
    assert report["revenue_recovered"] == sum(case["recovered_amount"] for case in cases)
    assert report["successful_recoveries"] == sum(case["recovery_status"] == "RECOVERED" for case in cases)
    assert report["revenue_recovered"] <= report["revenue_at_risk"]
    assert report["recovery_rate"] <= 100.0
    assert report["audit_events"] > 0


def test_reset_demo_records_preserves_unrelated_cases() -> None:
    unrelated_payment = {
        "id": "unrelated_payment",
        "order_id": "unrelated_order",
        "amount": 50000,
        "currency": "INR",
        "error_source": "issuer",
        "error_step": "payment_authorization",
        "error_description": "Temporary issue with the issuing bank.",
    }
    process_failed_payment(unrelated_payment, UnavailableRazorpayClient())
    run_batch_demo.process_batch(run_batch_demo.generate_synthetic_failures())

    removed = run_batch_demo.reset_demo_records()

    assert removed["recovery_cases"] == 50
    assert database.get_case_by_payment_id("unrelated_payment") is not None
    assert not [case for case in database.get_all_recovery_cases() if case["payment_id"].startswith("demo_batch_v1_")]
