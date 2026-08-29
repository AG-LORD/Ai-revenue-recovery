"""Read-only console view of recovery cases and their audit entries."""

from app.repositories.database import get_all_recovery_cases, get_audit_trail_for_case


def main() -> None:
    for case in get_all_recovery_cases():
        print(f"Case #{case['id']} | {case['payment_id']} | {case['recovery_status']} | Rs. {case['amount']}")
        for event in get_audit_trail_for_case(case["payment_id"]):
            print(f"  [{event['created_at']}] {event['actor']}: {event['action']}")


if __name__ == "__main__":
    main()
