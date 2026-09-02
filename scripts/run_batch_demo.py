"""Run a deterministic, offline batch demo through the recovery workflow."""

import argparse
import random
from collections.abc import Iterable
from typing import Any

from app.integrations.razorpay_client import UnavailableRazorpayClient
from app.repositories import database
from app.services.recovery_service import process_recovery_success_event
from app.services.workflow_service import process_failed_payment


BATCH_PAYMENT_PREFIX = "demo_batch_v1_"
DEFAULT_SEED = 20260829
DEFAULT_EVENT_COUNT = 50


def generate_synthetic_failures(
    count: int = DEFAULT_EVENT_COUNT, seed: int = DEFAULT_SEED
) -> list[dict[str, Any]]:
    """Generate reproducible failed-payment facts recognized by the diagnosis service."""
    generator = random.Random(seed)
    scenarios = generator.choices(
        ("temporary_issuer_failure", "customer_cancelled", "unknown_failure"),
        weights=(40, 35, 25),
        k=count,
    )
    events: list[dict[str, Any]] = []
    for index, scenario in enumerate(scenarios, start=1):
        payment_id = f"{BATCH_PAYMENT_PREFIX}{index:03d}"
        payment = {
            "id": payment_id,
            "order_id": f"demo_batch_order_{index:03d}",
            "amount": generator.choice((25000, 50000, 75000)),
            "currency": "INR",
            "method": generator.choice(("card", "upi", "wallet")),
            "status": "failed",
        }
        if scenario == "temporary_issuer_failure":
            payment.update(
                {
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_source": "issuer",
                    "error_step": "payment_authorization",
                    "error_reason": "payment_failed",
                    "error_description": "Temporary issue with the issuing bank.",
                }
            )
        elif scenario == "customer_cancelled":
            payment.update(
                {
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_source": "customer",
                    "error_step": "payment_authentication",
                    "error_reason": "payment_cancelled",
                    "error_description": "Customer cancelled the payment during checkout.",
                }
            )
        else:
            payment.update(
                {
                    "error_code": "UNEXPECTED_GATEWAY_ERR",
                    "error_source": "gateway",
                    "error_step": "payment_capture",
                    "error_reason": "unhandled_code_99",
                    "error_description": "Unclassified gateway error.",
                }
            )
        events.append(payment)
    return events


def _demo_record_counts() -> dict[str, int]:
    conn = database.get_connection()
    try:
        case_count = conn.execute(
            "SELECT COUNT(*) AS count FROM recovery_cases WHERE payment_id LIKE ?",
            (f"{BATCH_PAYMENT_PREFIX}%",),
        ).fetchone()["count"]
        audit_count = conn.execute(
            "SELECT COUNT(*) AS count FROM audit_trail WHERE payment_id LIKE ?",
            (f"{BATCH_PAYMENT_PREFIX}%",),
        ).fetchone()["count"]
    finally:
        conn.close()
    return {"recovery_cases": int(case_count), "audit_trail": int(audit_count)}


def reset_demo_records() -> dict[str, int]:
    """Delete only records created by this deterministic demo batch."""
    counts = _demo_record_counts()
    print(
        "Resetting demo records only: "
        f"{counts['audit_trail']} audit_trail rows and {counts['recovery_cases']} recovery_cases rows "
        f"with payment_id prefix '{BATCH_PAYMENT_PREFIX}'."
    )
    conn = database.get_connection()
    try:
        conn.execute("DELETE FROM audit_trail WHERE payment_id LIKE ?", (f"{BATCH_PAYMENT_PREFIX}%",))
        conn.execute("DELETE FROM recovery_cases WHERE payment_id LIKE ?", (f"{BATCH_PAYMENT_PREFIX}%",))
        conn.commit()
    finally:
        conn.close()
    return counts


def _should_simulate_recovery(index: int) -> bool:
    """Deterministically recover half of payment-link cases."""
    return index % 2 == 0


def _simulate_payment_link_paid(case: dict[str, Any], index: int) -> tuple[dict[str, Any] | None, bool]:
    """Send an internal success-event payload for an already-created payment link."""
    stored_case = database.get_case_by_payment_id(case["payment_id"])
    if stored_case is None:
        raise RuntimeError(f"Recovery case not found for {case['payment_id']}")
    amount_paisa = int(round(stored_case["amount"] * 100))
    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": case["payment_link_id"],
                    "amount_paid": amount_paisa,
                    "notes": {"original_payment_id": case["payment_id"]},
                }
            },
            "payment": {
                "entity": {
                    "id": f"demo_batch_recovered_{index:03d}",
                    "order_id": case["order_id"],
                    "amount": amount_paisa,
                    "notes": {"original_payment_id": case["payment_id"]},
                }
            },
        },
    }
    return process_recovery_success_event("payment_link.paid", payload)


def process_batch(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Process supplied failures through the existing workflow and return stored results."""
    database.reset_batch_demo_cases(BATCH_PAYMENT_PREFIX)
    gateway = UnavailableRazorpayClient()

    for index, payment in enumerate(events, start=1):
        outcome = process_failed_payment(payment, gateway, use_ai=False)
        case = outcome["case"]
        if outcome["recovery"].get("status") == "link_created" and _should_simulate_recovery(index):
            _simulate_payment_link_paid(case, index)
    return build_batch_report()


def build_batch_report() -> dict[str, Any]:
    """Calculate batch results from persisted demo records and audit entries."""
    conn = database.get_connection()
    try:
        cases = conn.execute(
            """
            SELECT diagnosis_category, action_taken, recovery_status, amount, recovered_amount, payment_link_url
            FROM recovery_cases WHERE payment_id LIKE ?
            """,
            (f"{BATCH_PAYMENT_PREFIX}%",),
        ).fetchall()
        audit_events = conn.execute(
            "SELECT COUNT(*) AS count FROM audit_trail WHERE payment_id LIKE ?",
            (f"{BATCH_PAYMENT_PREFIX}%",),
        ).fetchone()["count"]
    finally:
        conn.close()

    diagnosis_counts: dict[str, int] = {}
    policy_violations = 0
    total_at_risk_paisa = 0
    total_recovered_paisa = 0
    retries = links = escalations = recoveries = 0
    expected_actions = {
        "temporary_issuer_failure": "retry_payment",
        "customer_cancelled": "send_payment_reminder",
        "unknown_failure": "escalate_manual_review",
    }
    for case in cases:
        category = case["diagnosis_category"]
        diagnosis_counts[category] = diagnosis_counts.get(category, 0) + 1
        total_at_risk_paisa += case["amount"]
        total_recovered_paisa += case["recovered_amount"]
        retries += int(case["action_taken"] == "retry_payment")
        links += int(bool(case["payment_link_url"]))
        escalations += int(case["recovery_status"] == "ESCALATED")
        recoveries += int(case["recovery_status"] == "RECOVERED")
        policy_violations += int(case["action_taken"] != expected_actions.get(category))

    total_at_risk = total_at_risk_paisa / 100.0
    total_recovered = total_recovered_paisa / 100.0
    return {
        "payments_processed": len(cases),
        "revenue_at_risk": total_at_risk,
        "diagnosis_counts": diagnosis_counts,
        "bounded_retries": retries,
        "payment_links": links,
        "manual_escalations": escalations,
        "successful_recoveries": recoveries,
        "revenue_recovered": total_recovered,
        "revenue_still_at_risk": total_at_risk - total_recovered,
        "recovery_rate": (total_recovered / total_at_risk * 100.0) if total_at_risk else 0.0,
        "audit_events": int(audit_events),
        "policy_violations": policy_violations,
    }


def print_report(report: dict[str, Any]) -> None:
    """Print the stored batch metrics in a Buildathon-friendly format."""
    diagnosis = report["diagnosis_counts"]
    print("\n========================================")
    print("AI REVENUE RECOVERY - BATCH DEMO")
    print("========================================")
    print(f"Payments processed:       {report['payments_processed']}")
    print(f"Revenue at risk:          Rs. {report['revenue_at_risk']:,.2f}")
    print("\nDiagnosis:")
    print(f"  Temporary issuer failure: {diagnosis.get('temporary_issuer_failure', 0)}")
    print(f"  Customer cancelled:       {diagnosis.get('customer_cancelled', 0)}")
    print(f"  Unknown failure:          {diagnosis.get('unknown_failure', 0)}")
    print("\nActions:")
    print(f"  Bounded retries:          {report['bounded_retries']}")
    print(f"  Payment links:            {report['payment_links']}")
    print(f"  Manual escalations:       {report['manual_escalations']}")
    print("\nRecoveries:")
    print(f"  Successful recoveries:    {report['successful_recoveries']}")
    print(f"  Revenue recovered:        Rs. {report['revenue_recovered']:,.2f}")
    print(f"  Revenue still at risk:    Rs. {report['revenue_still_at_risk']:,.2f}")
    print(f"  Recovery rate:            {report['recovery_rate']:.1f}%")
    print("\nAudit events:              " + str(report["audit_events"]))
    print("Policy violations:         " + str(report["policy_violations"]))
    print("\n========================================")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the deterministic offline batch recovery demo.")
    parser.add_argument(
        "--reset-demo",
        action="store_true",
        help="delete only prior demo_batch_v1_ records before processing the demo",
    )
    args = parser.parse_args()
    existing = _demo_record_counts()
    if args.reset_demo:
        reset_demo_records()
    elif existing["recovery_cases"] or existing["audit_trail"]:
        raise SystemExit("Existing batch-demo records found. Re-run with --reset-demo to replace only those records.")

    report = process_batch(generate_synthetic_failures())
    print_report(report)


if __name__ == "__main__":
    main()
