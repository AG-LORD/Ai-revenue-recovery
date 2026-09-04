"""Merchant registry and tenant-resolution helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from app.repositories.database import get_connection
from app.repositories.merchant_scope import find_payment_event_merchant


DEMO_MERCHANTS = (
    {"merchant_key": "urban_cart", "business_name": "UrbanCart", "razorpay_account_id": "demo_account"},
    {"merchant_key": "fit_gear", "business_name": "FitGear", "razorpay_account_id": "demo_account"},
    {"merchant_key": "learn_pro", "business_name": "LearnPro", "razorpay_account_id": "demo_account"},
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
                    status = 'ACTIVE'
                """,
                (merchant["merchant_key"], merchant["business_name"], merchant["razorpay_account_id"], now),
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
            FROM merchant_accounts WHERE status = 'ACTIVE' ORDER BY id ASC
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
            FROM merchant_accounts WHERE merchant_key = ? AND status = 'ACTIVE' LIMIT 1
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
            FROM merchant_accounts WHERE id = ? AND status = 'ACTIVE' LIMIT 1
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
            WHERE o.order_id = ? AND m.status = 'ACTIVE' LIMIT 1
            """,
            (order_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_merchant_by_payment_id(payment_id: str) -> dict | None:
    """Resolve ownership from persisted payment history or recovery case."""
    if not payment_id:
        return None
    init_merchant_registry()
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT m.id, m.merchant_key, m.business_name, m.razorpay_account_id, m.status, m.created_at
            FROM merchant_accounts m
            JOIN recovery_cases c ON c.merchant_account_id = m.id
            WHERE c.payment_id = ? AND m.status = 'ACTIVE'
            ORDER BY c.id DESC LIMIT 1
            """,
            (payment_id,),
        ).fetchone()
        if row:
            return dict(row)
    finally:
        conn.close()

    merchant_id = find_payment_event_merchant(payment_id)
    return get_merchant_by_id(merchant_id) if merchant_id else None


def resolve_event_merchant(event: dict) -> dict | None:
    """Resolve a webhook merchant without guessing across tenants.

    Resolution order:
    1. A unique Razorpay account mapping.
    2. Our registered order ownership.
    3. Persisted payment/recovery ownership via payment ID.
    4. Original failed payment referenced by a recovery Payment Link.

    Shared demo accounts deliberately fall through to order/case ownership.
    If none resolve, return ``None`` and do not perform a financial action.
    """
    account_id = event.get("account_id")
    if account_id:
        init_merchant_registry()
        conn = get_connection()
        try:
            rows = conn.execute(
                """
                SELECT id, merchant_key, business_name, razorpay_account_id, status, created_at
                FROM merchant_accounts
                WHERE razorpay_account_id = ? AND status = 'ACTIVE'
                ORDER BY id ASC
                """,
                (account_id,),
            ).fetchall()
            if len(rows) == 1:
                return dict(rows[0])
        finally:
            conn.close()

    payload = event.get("payload", {}) or {}
    payment = payload.get("payment", {}).get("entity", {}) or {}
    order = payload.get("order", {}).get("entity", {}) or {}
    payment_link = payload.get("payment_link", {}).get("entity", {}) or {}

    order_id = payment.get("order_id") or order.get("id") or payment_link.get("order_id")
    merchant = get_merchant_by_order_id(order_id)
    if merchant:
        return merchant

    payment_id = payment.get("id") or order.get("payment_id")
    merchant = get_merchant_by_payment_id(payment_id)
    if merchant:
        return merchant

    notes = payment_link.get("notes", {}) or {}
    original_payment_id = notes.get("original_payment_id")
    if not original_payment_id:
        original_payment_id = payment.get("notes", {}).get("original_payment_id")
    return get_merchant_by_payment_id(original_payment_id)
