"""Database migration for merchant-aware recovery data.

The migration is additive: existing hackathon data is preserved and assigned to
the default demo merchant, while new order/payment/recovery rows inherit merchant
ownership from the merchant order registry.
"""

from __future__ import annotations

from app.repositories.database import get_connection
from app.services.merchant_service import DEFAULT_MERCHANT_KEY, init_merchant_registry


def _add_column_if_missing(conn, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _create_tenant_triggers(conn) -> None:
    """Propagate merchant ownership through the existing repository write paths.

    This keeps the mature payment/recovery repository functions intact while the
    application becomes multi-tenant. Triggers only fill NULL merchant values;
    they never overwrite an explicitly assigned tenant.
    """
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS trg_payment_event_merchant
        AFTER INSERT ON payment_events
        WHEN NEW.merchant_account_id IS NULL AND NEW.order_id IS NOT NULL
        BEGIN
            UPDATE payment_events
            SET merchant_account_id = (
                SELECT merchant_account_id FROM merchant_orders
                WHERE order_id = NEW.order_id
            )
            WHERE id = NEW.id;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_recovery_case_merchant
        AFTER INSERT ON recovery_cases
        WHEN NEW.merchant_account_id IS NULL AND NEW.order_id IS NOT NULL
        BEGIN
            UPDATE recovery_cases
            SET merchant_account_id = (
                SELECT merchant_account_id FROM merchant_orders
                WHERE order_id = NEW.order_id
            )
            WHERE id = NEW.id;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_audit_case_merchant
        AFTER INSERT ON audit_trail
        WHEN NEW.merchant_account_id IS NULL AND NEW.case_id IS NOT NULL
        BEGIN
            UPDATE audit_trail
            SET merchant_account_id = (
                SELECT merchant_account_id FROM recovery_cases
                WHERE id = NEW.case_id
            )
            WHERE id = NEW.id;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_webhook_payment_merchant
        AFTER INSERT ON webhook_events
        WHEN NEW.merchant_account_id IS NULL
        BEGIN
            UPDATE webhook_events
            SET merchant_account_id = (
                SELECT mo.merchant_account_id
                FROM merchant_orders mo
                WHERE mo.order_id = COALESCE(
                    json_extract(NEW.payload, '$.payload.payment.entity.order_id'),
                    json_extract(NEW.payload, '$.payload.order.entity.id'),
                    json_extract(NEW.payload, '$.payload.payment_link.entity.order_id')
                )
                LIMIT 1
            )
            WHERE id = NEW.id;
        END;
        """
    )


def init_merchant_data() -> None:
    """Create merchant tenancy tables and safely migrate existing records."""
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
            "CREATE INDEX IF NOT EXISTS idx_payment_events_merchant ON payment_events(merchant_account_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recovery_cases_merchant ON recovery_cases(merchant_account_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_trail_merchant ON audit_trail(merchant_account_id)"
        )

        # Historical single-merchant data predates merchant_orders. Preserve it
        # under UrbanCart and register known historical orders there.
        for table in ("webhook_events", "payment_events", "recovery_cases", "audit_trail"):
            conn.execute(
                f"UPDATE {table} SET merchant_account_id = ? WHERE merchant_account_id IS NULL",
                (default_merchant_id,),
            )

        conn.execute(
            """
            INSERT OR IGNORE INTO merchant_orders (order_id, merchant_account_id, created_at)
            SELECT order_id, ?, COALESCE(MIN(received_at), CURRENT_TIMESTAMP)
            FROM payment_events
            WHERE order_id IS NOT NULL
            GROUP BY order_id
            """,
            (default_merchant_id,),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO merchant_orders (order_id, merchant_account_id, created_at)
            SELECT order_id, ?, COALESCE(MIN(created_at), CURRENT_TIMESTAMP)
            FROM recovery_cases
            WHERE order_id IS NOT NULL
            GROUP BY order_id
            """,
            (default_merchant_id,),
        )

        _create_tenant_triggers(conn)
        conn.commit()
    finally:
        conn.close()
