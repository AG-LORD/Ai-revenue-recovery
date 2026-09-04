"""Merchant registry and tenant-resolution helpers."""

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

DEFAULT_MERCHANT_KEY = "urban_cart"


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


def register_order(order_id: str, merchant_id: int) -> None:
    """Bind a Razorpay order to exactly one application merchant."""
    if not order_id or not merchant_id:
        raise ValueError("order_id and merchant_id are required")
    init_merchant_registry()
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO merchant_orders (order_id, merchant_account_id, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET merchant_account_id = excluded.merchant_account_id
            """,
            (order_id, merchant_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def get_merchant_by_order_id(order_id: str) -> dict | None:
    """Resolve the application merchant that owns a Razorpay order."""
    if not order_id:
        return None
    init_merchant_registry()
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT m.id, m.merchant_key, m.business_name, m.razorpay_account_id, m.status, m.created_at
            FROM merchant_accounts m
            JOIN merchant_orders o ON o.merchant_account_id = m.id
            WHERE o.order_id = ? AND m.status = 'ACTIVE'
            LIMIT 1
            """,
            (order_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def resolve_event_merchant(event: dict) -> dict | None:
    """Resolve webhook merchant from account context or known order mapping.

    Production webhooks should carry Razorpay account context. The order mapping
    provides a safe fallback for this single-test-account hackathon environment.
    """
    account_id = event.get("account_id")
    if account_id:
        init_merchant_registry()
        conn = get_connection()
        try:
            row = conn.execute(
                """
                SELECT id, merchant_key, business_name, razorpay_account_id, status, created_at
                FROM merchant_accounts
                WHERE razorpay_account_id = ? AND status = 'ACTIVE'
                ORDER BY id ASC
                """,
                (account_id,),
            ).fetchall()
            if len(row) == 1:
                return dict(row[0])
        finally:
            conn.close()

    payload = event.get("payload", {})
    payment = payload.get("payment", {}).get("entity", {})
    order = payload.get("order", {}).get("entity", {})
    payment_link = payload.get("payment_link", {}).get("entity", {})
    order_id = payment.get("order_id") or order.get("id") or payment_link.get("order_id")
    return get_merchant_by_order_id(order_id)
