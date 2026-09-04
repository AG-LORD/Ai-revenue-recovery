"""Merchant registry and tenant-resolution helpers.

The prototype uses logical merchant accounts so the recovery engine can be
merchant-aware even when the hackathon environment has one Razorpay Test Mode
credential. In production, ``razorpay_account_id`` maps to the merchant's
Razorpay account context.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.repositories.database import get_connection


DEMO_MERCHANTS = (
    {
        "merchant_key": "urban_cart",
        "business_name": "UrbanCart",
        "razorpay_account_id": "demo_account",
    },
    {
        "merchant_key": "fit_gear",
        "business_name": "FitGear",
        "razorpay_account_id": "demo_account",
    },
    {
        "merchant_key": "learn_pro",
        "business_name": "LearnPro",
        "razorpay_account_id": "demo_account",
    },
)


def init_merchant_registry() -> None:
    """Create the merchant registry and seed deterministic demo merchants."""
    conn = get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS merchant_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                merchant_key TEXT UNIQUE NOT NULL,
                business_name TEXT NOT NULL,
                razorpay_account_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                created_at TEXT NOT NULL
            )
            """
        )

        now = datetime.now(timezone.utc).isoformat()
        for merchant in DEMO_MERCHANTS:
            conn.execute(
                """
                INSERT INTO merchant_accounts (
                    merchant_key, business_name, razorpay_account_id, status, created_at
                ) VALUES (?, ?, ?, 'ACTIVE', ?)
                ON CONFLICT(merchant_key) DO UPDATE SET
                    business_name = excluded.business_name,
                    razorpay_account_id = excluded.razorpay_account_id,
                    status = 'ACTIVE'
                """,
                (
                    merchant["merchant_key"],
                    merchant["business_name"],
                    merchant["razorpay_account_id"],
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def get_merchants() -> list[dict]:
    """Return all active merchants in stable display order."""
    init_merchant_registry()
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, merchant_key, business_name, razorpay_account_id, status, created_at
            FROM merchant_accounts
            WHERE status = 'ACTIVE'
            ORDER BY id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_merchant_by_key(merchant_key: str) -> dict | None:
    """Resolve a merchant by its application-level key."""
    if not merchant_key:
        return None
    init_merchant_registry()
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT id, merchant_key, business_name, razorpay_account_id, status, created_at
            FROM merchant_accounts
            WHERE merchant_key = ? AND status = 'ACTIVE'
            LIMIT 1
            """,
            (merchant_key,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_merchant_by_id(merchant_id: int) -> dict | None:
    """Resolve a merchant by its internal numeric ID."""
    init_merchant_registry()
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT id, merchant_key, business_name, razorpay_account_id, status, created_at
            FROM merchant_accounts
            WHERE id = ? AND status = 'ACTIVE'
            LIMIT 1
            """,
            (merchant_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
