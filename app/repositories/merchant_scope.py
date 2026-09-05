"""Merchant-scoped repository lookups for financial reconciliation."""

from __future__ import annotations

from app.repositories.database import get_connection


def _to_case(row):
    if not row:
        return None
    case = dict(row)
    case["amount"] = case["amount"] / 100.0
    case["recovered_amount"] = case["recovered_amount"] / 100.0
    return case


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
            rows = conn.execute(
                """
                SELECT * FROM recovery_cases
                WHERE merchant_account_id = ? AND payment_link_id = ?
                  AND recovery_status != 'RECOVERED'
                ORDER BY id ASC
                """,
                (merchant_account_id, payment_link_id),
            ).fetchall()
            if len(rows) == 1:
                row = rows[0]
            elif len(rows) > 1:
                return None

            if not row:
                recovered_rows = conn.execute(
                    """
                    SELECT * FROM recovery_cases
                    WHERE merchant_account_id = ? AND payment_link_id = ?
                    ORDER BY id DESC
                    """,
                    (merchant_account_id, payment_link_id),
                ).fetchall()
                if len(recovered_rows) == 1:
                    row = recovered_rows[0]

        if not row and original_payment_id:
            row = conn.execute(
                """
                SELECT * FROM recovery_cases
                WHERE merchant_account_id = ? AND payment_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (merchant_account_id, original_payment_id),
            ).fetchone()

        if not row and order_id and original_payment_id is None:
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

        return _to_case(row)
    finally:
        conn.close()


def find_case_for_captured_payment_scoped(
    merchant_account_id: int,
    payment_id: str | None = None,
    order_id: str | None = None,
):
    """Find a successful-payment recovery case only inside one merchant.

    A supplied payment ID is authoritative. We never fall back to order-only
    matching when the event already identifies a payment, because multiple
    payment attempts can exist for the same Razorpay order.
    """
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

        return _to_case(row)
    finally:
        conn.close()


def find_same_order_recovery_candidates_scoped(
    merchant_account_id: int,
    order_id: str,
    amount_paise: int,
    currency: str | None,
) -> list[dict]:
    """Return every unresolved, exact same-order recovery opportunity.

    Callers must only reconcile when this list contains exactly one case.  This
    deliberately does not accept customer-only, amount-only, or unscoped order
    matching.
    """
    if not merchant_account_id or not order_id or amount_paise is None or not currency:
        return []
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT * FROM recovery_cases
            WHERE merchant_account_id = ?
              AND order_id = ?
              AND amount = ?
              AND currency = ?
              AND recovery_status != 'RECOVERED'
            ORDER BY id ASC
            """,
            (merchant_account_id, order_id, int(amount_paise), currency),
        ).fetchall()
        return [_to_case(row) for row in rows]
    finally:
        conn.close()


def find_payment_event_merchant(payment_id: str) -> int | None:
    """Resolve event ownership from persisted payment history."""
    if not payment_id:
        return None
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT merchant_account_id
            FROM payment_events
            WHERE payment_id = ? AND merchant_account_id IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (payment_id,),
        ).fetchone()
        return int(row["merchant_account_id"]) if row else None
    finally:
        conn.close()


def find_webhook_event_merchant(event_id: str) -> int | None:
    """Return the merchant reserved for a webhook event."""
    if not event_id:
        return None
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT merchant_account_id FROM webhook_events WHERE event_id = ? LIMIT 1",
            (event_id,),
        ).fetchone()
        return int(row["merchant_account_id"]) if row and row["merchant_account_id"] is not None else None
    finally:
        conn.close()
