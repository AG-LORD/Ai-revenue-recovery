"""Database migration for merchant-aware recovery data.

This migration is intentionally additive. Existing hackathon data is preserved
and assigned to the default demo merchant (UrbanCart) until all write paths
become merchant-aware.
"""

from __future__ import annotations

from app.repositories.database import get_connection
from app.services.merchant_service import DEMO_MERCHANTS, init_merchant_registry


DEFAULT_MERCHANT_KEY = "urban_cart"


def _add_column_if_missing(conn, table: str, column: str, definition: str) -> None:
    columns = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_merchant_data() -> None:
    """Create merchants and add merchant context to existing event tables."""
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

        # Nullable during migration so existing rows can be preserved safely.
        for table in ("webhook_events", "payment_events", "recovery_cases", "audit_trail"):
            _add_column_if_missing(conn, table, "merchant_account_id", "INTEGER")

        # Existing single-merchant records belong to the default demo tenant.
        for table in ("webhook_events", "payment_events", "recovery_cases", "audit_trail"):
            conn.execute(
                f"UPDATE {table} SET merchant_account_id = ? WHERE merchant_account_id IS NULL",
                (default_merchant_id,),
            )

        conn.commit()
    finally:
        conn.close()
