"""Database migration for merchant-aware recovery data."""

from __future__ import annotations

from app.repositories.database import get_connection
from app.services.merchant_service import DEFAULT_MERCHANT_KEY, init_merchant_registry


def _add_column_if_missing(conn, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_merchant_data() -> None:
    """Create merchant registry, order ownership, and migrate existing records."""
    init_merchant_registry()
    conn = get_connection()
    try:
        merchant = conn.execute(
            "SELECT id FROM merchant_accounts WHERE merchant_key = ? LIMIT 1",
            (DEFAULT_MERCHANT_KEY,),
        ).fetchone()
        if not merchant:
            raise RuntimeError("Default demo merchant was not seeded")
        default_merchant_id = merchant[0]

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS merchant_orders (
                order_id TEXT PRIMARY KEY,
                merchant_account_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (merchant_account_id) REFERENCES merchant_accounts(id)
            )
            """
        )

        for table in ("webhook_events", "payment_events", "recovery_cases", "audit_trail"):
            _add_column_if_missing(conn, table, "merchant_account_id", "INTEGER")
            conn.execute(
                f"UPDATE {table} SET merchant_account_id = ? WHERE merchant_account_id IS NULL",
                (default_merchant_id,),
            )

        conn.execute(
            """
            INSERT OR IGNORE INTO merchant_orders (order_id, merchant_account_id, created_at)
            SELECT DISTINCT order_id, ?, COALESCE(MIN(received_at), CURRENT_TIMESTAMP)
            FROM payment_events WHERE order_id IS NOT NULL GROUP BY order_id
            """,
            (default_merchant_id,),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO merchant_orders (order_id, merchant_account_id, created_at)
            SELECT DISTINCT order_id, ?, COALESCE(MIN(created_at), CURRENT_TIMESTAMP)
            FROM recovery_cases WHERE order_id IS NOT NULL GROUP BY order_id
            """,
            (default_merchant_id,),
        )
        conn.commit()
    finally:
        conn.close()
