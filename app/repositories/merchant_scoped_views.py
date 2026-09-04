"""Merchant-scoped aggregate helpers for the recovery agent console."""

from __future__ import annotations

from app.repositories.database import get_connection


def _where(merchant_account_id: int | None, column: str = "merchant_account_id"):
    if merchant_account_id is None:
        return "", []
    return f" WHERE {column} = ?", [merchant_account_id]


def get_merchant_cases(merchant_account_id: int | None = None) -> list[dict]:
    where, params = _where(merchant_account_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT * FROM recovery_cases{where} ORDER BY id DESC",
            params,
        ).fetchall()
        cases = [dict(row) for row in rows]
        for case in cases:
            case["amount"] = case["amount"] / 100.0
            case["recovered_amount"] = case["recovered_amount"] / 100.0
        return cases
    finally:
        conn.close()


def get_merchant_metrics(merchant_account_id: int | None = None) -> dict:
    where, params = _where(merchant_account_id)
    conn = get_connection()
    try:
        row = conn.execute(
            f"""
            SELECT
                COALESCE(SUM(amount), 0) AS total_revenue_at_risk_paise,
                COALESCE(SUM(recovered_amount), 0) AS total_revenue_recovered_paise,
                COUNT(*) AS total_cases,
                COALESCE(SUM(CASE WHEN recovery_status = 'RECOVERED' THEN 1 ELSE 0 END), 0) AS recovered_cases,
                COALESCE(SUM(CASE WHEN recovery_status = 'ESCALATED' THEN 1 ELSE 0 END), 0) AS escalated_cases,
                COALESCE(SUM(CASE WHEN action_taken = 'retry_payment' THEN 1 ELSE 0 END), 0) AS retry_actions,
                COALESCE(SUM(CASE WHEN action_taken = 'send_payment_reminder' THEN 1 ELSE 0 END), 0) AS reminder_actions,
                COALESCE(SUM(CASE WHEN action_taken = 'escalate_manual_review' THEN 1 ELSE 0 END), 0) AS manual_escalations,
                COALESCE(SUM(CASE WHEN recovery_status IN ('DETECTED','PENDING_RETRY','PENDING_REMINDER','LINK_CREATED') THEN 1 ELSE 0 END), 0) AS pending_cases
            FROM recovery_cases
            {where}
            """,
            params,
        ).fetchone()
    finally:
        conn.close()

    risk = int(row["total_revenue_at_risk_paise"] or 0)
    recovered = int(row["total_revenue_recovered_paise"] or 0)
    return {
        "merchant_account_id": merchant_account_id,
        "total_revenue_at_risk": round(risk / 100.0, 2),
        "total_revenue_recovered": round(recovered / 100.0, 2),
        "recovery_rate_percentage": round((recovered / risk * 100.0), 1) if risk else 0.0,
        "total_cases": int(row["total_cases"]),
        "recovered_cases": int(row["recovered_cases"]),
        "escalated_cases": int(row["escalated_cases"]),
        "retry_actions": int(row["retry_actions"]),
        "reminder_actions": int(row["reminder_actions"]),
        "manual_escalations": int(row["manual_escalations"]),
        "pending_cases": int(row["pending_cases"]),
    }
