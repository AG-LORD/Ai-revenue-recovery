"""Merchant-scoped repository lookups for financial reconciliation."""

from __future__ import annotations

from app.repositories.database import get_connection


def find_case_for_recovery_event_scoped(
    merchant_account_id: int,
    order_id: str | None = None,
    payment_link_id: str | None = None,
    original_payment_id: str | None = None,
):
    """Find a recovery case only inside the resolved merchant tenant."""
    if not merchant_account_id:
        return None

    conn = get_connection()
    try:
        row = None
        if payment_link_id:
            row = conn.execute(
                """
                SELECT * FROM recovery_cases
                WHERE merchant_account_id = ? AND payment_link_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (merchant_account_id, payment_link_id),
            ).fetchone()

        if not row and original_payment_id:
            row = conn.execute(
                """
                SELECT * FROM recovery_cases
                WHERE merchant_account_id = ? AND payment_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (merchant_account_id, original_payment_id),
            ).fetchone()

        if not row and order_id:
            rows = conn.execute(
                """
                SELECT * FROM recovery_cases
                WHERE merchant_account_id = ?
                  AND order_id = ?
                  AND recovery_status != 'RECOVERED'
                ORDER BY id DESC
                """,
                (merchant_account_id, order_id),
            ).fetchall()
            row = rows[0] if len(rows) == 1 else None

        if not row:
            return None

        case = dict(row)
        case["amount"] = case["amount"] / 100.0
        case["recovered_amount"] = case["recovered_amount"] / 100.0
        return case
    finally:
        conn.close()


def find_case_for_captured_payment_scoped(
    merchant_account_id: int,
    payment_id: str | None = None,
    order_id: str | None = None,
):
    """Find a successful-payment recovery case only inside one merchant."""
    if not merchant_account_id:
        return None

    conn = get_connection()
    try:
        if payment_id:
            row = conn.execute(
                """
                SELECT * FROM recovery_cases
                WHERE merchant_account_id = ? AND payment_id = ?
                LIMIT 1
                """,
                (merchant_account_id, payment_id),
            ).fetchone()
        elif order_id:
            rows = conn.execute(
                """
                SELECT * FROM recovery_cases
                WHERE merchant_account_id = ?
                  AND order_id = ?
                  AND recovery_status != 'RECOVERED'
                """,
                (merchant_account_id, order_id),
            ).fetchall()
            row = rows[0] if len(rows) == 1 else None
        else:
            row = None

        if not row:
            return None

        case = dict(row)
        case["amount"] = case["amount"] / 100.0
        case["recovered_amount"] = case["recovered_amount"] / 100.0
        return case
    finally:
        conn.close()
