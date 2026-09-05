import sqlite3
import json
import logging
from datetime import datetime, timezone
from app.core.config import DATABASE_PATH

logger = logging.getLogger(__name__)

# Use DATABASE_PATH from config; default points to revenue_recovery.db in project root

def get_connection() -> sqlite3.Connection:
    """Returns a connection to the SQLite database.
    Row factory is set to sqlite3.Row so columns can be accessed by name.
    """
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initializes database tables if they do not already exist."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS webhook_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT UNIQUE NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL,
            received_at TEXT NOT NULL,
            merchant_account_id INTEGER
        );
        """,
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS payment_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT UNIQUE NOT NULL,
            event_type TEXT NOT NULL,
            payment_id TEXT,
            order_id TEXT,
            amount_paise INTEGER,
            currency TEXT,
            payment_status TEXT,
            source TEXT NOT NULL CHECK (source IN ('razorpay_webhook', 'simulation', 'system')),
            payload TEXT NOT NULL,
            received_at TEXT NOT NULL,
            processed_at TEXT,
            processing_status TEXT NOT NULL,
            merchant_account_id INTEGER
        );
        """,
    )

    cursor.execute("PRAGMA table_info(recovery_cases)")
    columns = {col[1]: col[2] for col in cursor.fetchall()}
    need_to_migrate = False
    if "amount" in columns and columns["amount"].upper() == "REAL":
        need_to_migrate = True
    if "recovered_amount" in columns and columns["recovered_amount"].upper() == "REAL":
        need_to_migrate = True

    if need_to_migrate:
        cursor.execute("ALTER TABLE recovery_cases RENAME TO recovery_cases_old")
        cursor.execute(
            """
            CREATE TABLE recovery_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id TEXT UNIQUE NOT NULL,
                order_id TEXT,
                amount INTEGER NOT NULL,
                currency TEXT NOT NULL,
                payment_method TEXT,
                wallet TEXT,
                error_code TEXT,
                error_source TEXT,
                error_step TEXT,
                error_reason TEXT,
                error_description TEXT,
                diagnosis_category TEXT,
                diagnosis_text TEXT,
                is_recoverable INTEGER NOT NULL,
                recommended_action TEXT,
                max_retries INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                recovery_status TEXT NOT NULL DEFAULT 'DETECTED',
                action_taken TEXT,
                action_result TEXT,
                payment_link_id TEXT,
                payment_link_url TEXT,
                ai_explanation TEXT,
                customer_message TEXT,
                recovered_amount INTEGER NOT NULL DEFAULT 0,
                recovery_source TEXT,
                recovered_payment_id TEXT,
                recovered_event_id TEXT,
                recovery_confirmed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                merchant_account_id INTEGER
            );
            """,
        )
        cursor.execute(
            """
            INSERT INTO recovery_cases (
                id, payment_id, order_id, amount, currency, payment_method, wallet,
                error_code, error_source, error_step, error_reason, error_description,
                diagnosis_category, diagnosis_text, is_recoverable, recommended_action,
                max_retries, retry_count, recovery_status, action_taken, action_result,
                payment_link_id, payment_link_url, ai_explanation, customer_message,
                recovered_amount, created_at, updated_at, merchant_account_id
            ) SELECT
                id, payment_id, order_id,
                ROUND(amount * 100),
                currency, payment_method, wallet,
                error_code, error_source, error_step, error_reason, error_description,
                diagnosis_category, diagnosis_text, is_recoverable, recommended_action,
                max_retries, retry_count, recovery_status, action_taken, action_result,
                payment_link_id, payment_link_url, ai_explanation, customer_message,
                ROUND(recovered_amount * 100),
                created_at, updated_at, NULL
            FROM recovery_cases_old
            """,
        )
        cursor.execute("DROP TABLE recovery_cases_old")
    else:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS recovery_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id TEXT UNIQUE NOT NULL,
                order_id TEXT,
                amount INTEGER NOT NULL,
                currency TEXT NOT NULL,
                payment_method TEXT,
                wallet TEXT,
                error_code TEXT,
                error_source TEXT,
                error_step TEXT,
                error_reason TEXT,
                error_description TEXT,
                diagnosis_category TEXT,
                diagnosis_text TEXT,
                is_recoverable INTEGER NOT NULL,
                recommended_action TEXT,
                max_retries INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                recovery_status TEXT NOT NULL DEFAULT 'DETECTED',
                action_taken TEXT,
                action_result TEXT,
                payment_link_id TEXT,
                payment_link_url TEXT,
                ai_explanation TEXT,
                customer_message TEXT,
                recovered_amount INTEGER NOT NULL DEFAULT 0,
                recovery_source TEXT,
                recovered_payment_id TEXT,
                recovered_event_id TEXT,
                recovery_confirmed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                merchant_account_id INTEGER
            );
            """,
        )

    cursor.execute("PRAGMA table_info(audit_trail)")
    audit_columns = {col[1]: col[2] for col in cursor.fetchall()}

    if "timestamp" in audit_columns:
        cursor.execute("ALTER TABLE audit_trail RENAME TO audit_trail_old")
        cursor.execute(
            """
            CREATE TABLE audit_trail (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER,
                payment_id TEXT,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL,
                merchant_account_id INTEGER,
                FOREIGN KEY (case_id) REFERENCES recovery_cases(id)
            );
            """
        )
        if "created_at" in audit_columns:
            cursor.execute(
                """
                INSERT INTO audit_trail (
                    id, case_id, payment_id, actor, action, details, created_at, merchant_account_id
                )
                SELECT id, case_id, payment_id, actor, action, details,
                       COALESCE(created_at, timestamp), NULL
                FROM audit_trail_old
                """
            )
        else:
            cursor.execute(
                """
                INSERT INTO audit_trail (
                    id, case_id, payment_id, actor, action, details, created_at, merchant_account_id
                )
                SELECT id, case_id, payment_id, actor, action, details, timestamp, NULL
                FROM audit_trail_old
                """
            )
        cursor.execute("DROP TABLE audit_trail_old")
    else:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_trail (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER,
                payment_id TEXT,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL,
                merchant_account_id INTEGER,
                FOREIGN KEY (case_id) REFERENCES recovery_cases(id)
            );
            """,
        )

    for table, column in (
        ("webhook_events", "merchant_account_id"),
        ("payment_events", "merchant_account_id"),
        ("recovery_cases", "merchant_account_id"),
        ("audit_trail", "merchant_account_id"),
    ):
        existing = {col[1] for col in cursor.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER")

    # Recovery outcome is intentionally separate from the policy action.  These
    # additions are nullable so existing recovery cases retain their history.
    for column, definition in (
        ("recovery_source", "TEXT"),
        ("recovered_payment_id", "TEXT"),
        ("recovered_event_id", "TEXT"),
        ("recovery_confirmed_at", "TEXT"),
    ):
        existing = {col[1] for col in cursor.execute("PRAGMA table_info(recovery_cases)").fetchall()}
        if column not in existing:
            cursor.execute(f"ALTER TABLE recovery_cases ADD COLUMN {column} {definition}")

    conn.commit()
    conn.close()
    logger.info("Database initialized: %s", DATABASE_PATH)


def is_event_processed(event_id: str) -> bool:
    if not event_id:
        return False
    conn = get_connection()
    try:
        row = conn.execute("SELECT id FROM webhook_events WHERE event_id = ?", (event_id,)).fetchone()
        return row is not None
    finally:
        conn.close()


def record_webhook_event(event_id: str, event_type: str, payload_dict: dict, status: str = "PROCESSED", merchant_account_id: int | None = None) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    payload_json = json.dumps(payload_dict)
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO webhook_events (event_id, event_type, payload, status, received_at, merchant_account_id)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, event_type, payload_json, status, now, merchant_account_id),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def update_webhook_event_status(event_id: str, status: str) -> None:
    conn = get_connection()
    try:
        conn.execute("UPDATE webhook_events SET status = ? WHERE event_id = ?", (status, event_id))
        conn.commit()
    finally:
        conn.close()


def release_processing_webhook_event(event_id: str) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM webhook_events WHERE event_id = ? AND status = 'PROCESSING'", (event_id,))
        conn.commit()
    finally:
        conn.close()


def record_payment_event(
    event_id: str,
    event_type: str,
    payload_dict: dict,
    source: str,
    payment_id: str | None = None,
    order_id: str | None = None,
    amount_paise: int | None = None,
    currency: str | None = None,
    payment_status: str | None = None,
    processing_status: str = "PROCESSED",
    merchant_account_id: int | None = None,
) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO payment_events (
                event_id, event_type, payment_id, order_id, amount_paise,
                currency, payment_status, source, payload, received_at,
                processed_at, processing_status, merchant_account_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id, event_type, payment_id, order_id, amount_paise,
                currency, payment_status, source, json.dumps(payload_dict), now,
                now if processing_status == "PROCESSED" else None,
                processing_status, merchant_account_id,
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def update_payment_event_status(event_id: str, processing_status: str) -> None:
    processed_at = datetime.now(timezone.utc).isoformat() if processing_status == "PROCESSED" else None
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE payment_events SET processing_status = ?, processed_at = ? WHERE event_id = ?",
            (processing_status, processed_at, event_id),
        )
        conn.commit()
    finally:
        conn.close()


def retry_failed_payment_event(event_id: str) -> bool:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE payment_events
            SET processing_status = 'PROCESSING', processed_at = NULL
            WHERE event_id = ? AND processing_status = 'FAILED'
            """,
            (event_id,),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def has_payment_event(event_id: str) -> bool:
    conn = get_connection()
    try:
        row = conn.execute("SELECT 1 FROM payment_events WHERE event_id = ?", (event_id,)).fetchone()
        return row is not None
    finally:
        conn.close()


def get_payment_events(payment_id: str | None = None, order_id: str | None = None, merchant_account_id: int | None = None) -> list[dict]:
    clauses = []
    values: list[str | int] = []
    if payment_id is not None:
        clauses.append("payment_id = ?")
        values.append(payment_id)
    if order_id is not None:
        clauses.append("order_id = ?")
        values.append(order_id)
    if merchant_account_id is not None:
        clauses.append("merchant_account_id = ?")
        values.append(merchant_account_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = get_connection()
    try:
        rows = conn.execute(f"SELECT * FROM payment_events{where} ORDER BY id ASC", values).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_latest_payment_event(payment_id: str, merchant_account_id: int | None = None) -> dict | None:
    events = get_payment_events(payment_id=payment_id, merchant_account_id=merchant_account_id)
    return events[-1] if events else None


def get_payment_lifecycle(payment_id: str, merchant_account_id: int | None = None) -> dict:
    events = get_payment_events(payment_id=payment_id, merchant_account_id=merchant_account_id)
    if not events:
        return {"payment_id": payment_id, "status": None, "is_successful": False, "amount_paise": 0, "currency": None, "events": []}

    successful = [e for e in events if e["event_type"] in {"payment.captured", "order.paid"}]
    captured = next((e for e in successful if e["event_type"] == "payment.captured"), None)
    latest = events[-1]
    canonical = captured or (successful[-1] if successful else None)
    status = "captured" if canonical else latest["payment_status"] or latest["event_type"].split(".")[-1]
    return {
        "payment_id": payment_id,
        "status": status,
        "is_successful": canonical is not None,
        "amount_paise": int((canonical or latest).get("amount_paise") or 0),
        "currency": (canonical or latest).get("currency"),
        "events": events,
    }


def get_successful_payments(merchant_account_id: int | None = None) -> list[dict]:
    events = get_payment_events(merchant_account_id=merchant_account_id)
    successful = [e for e in events if e["processing_status"] == "PROCESSED" and e["event_type"] in {"payment.captured", "order.paid"}]
    groups: list[dict] = []
    payment_groups: dict[str, dict] = {}
    for event in successful:
        payment_id = event["payment_id"]
        order_id = event["order_id"]
        matching = payment_groups.get(payment_id) if payment_id else None
        if matching is None and order_id:
            candidates = [g for g in groups if order_id in g["order_ids"] and (not payment_id or not g["payment_ids"])]
            matching = candidates[0] if len(candidates) == 1 else None
        if matching is None:
            matching = {"events": [], "payment_ids": set(), "order_ids": set()}
            groups.append(matching)
        matching["events"].append(event)
        if payment_id:
            matching["payment_ids"].add(payment_id)
            payment_groups[payment_id] = matching
        if order_id:
            matching["order_ids"].add(order_id)

    canonical_payments = []
    for group in groups:
        captured = next((e for e in group["events"] if e["event_type"] == "payment.captured"), None)
        canonical = captured or group["events"][-1]
        canonical_payments.append({
            "payment_id": next(iter(group["payment_ids"]), None),
            "order_id": next(iter(group["order_ids"]), None),
            "amount_paise": int(canonical.get("amount_paise") or 0),
            "currency": canonical.get("currency"),
            "status": "captured",
            "event_id": canonical["event_id"],
            "event_type": canonical["event_type"],
            "source": canonical["source"],
            "timestamp": canonical["received_at"],
            "_sort_id": canonical["id"],
        })
    canonical_payments.sort(
        key=lambda payment: (payment["timestamp"], payment["_sort_id"]),
        reverse=True,
    )
    for payment in canonical_payments:
        payment.pop("_sort_id", None)
    return canonical_payments


def get_captured_revenue(merchant_account_id: int | None = None) -> int:
    return sum(payment["amount_paise"] for payment in get_successful_payments(merchant_account_id=merchant_account_id))


def get_payment_status(payment_id: str, merchant_account_id: int | None = None) -> str | None:
    return get_payment_lifecycle(payment_id, merchant_account_id=merchant_account_id)["status"]


def add_audit_log(payment_id: str, actor: str, action: str, details: str, case_id: int = None, merchant_account_id: int | None = None):
    now = datetime.now(timezone.utc).isoformat()
    if merchant_account_id is None and case_id is not None:
        conn = get_connection()
        try:
            row = conn.execute("SELECT merchant_account_id FROM recovery_cases WHERE id = ?", (case_id,)).fetchone()
            if row and row["merchant_account_id"] is not None:
                merchant_account_id = row["merchant_account_id"]
        finally:
            conn.close()
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO audit_trail (case_id, payment_id, actor, action, details, created_at, merchant_account_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (case_id, payment_id, actor, action, details, now, merchant_account_id),
        )
        conn.commit()
    finally:
        conn.close()


def create_or_get_recovery_case(payment: dict, diagnosis: dict, merchant_account_id: int | None = None) -> tuple[dict, bool]:
    payment_id = payment.get("id")
    order_id = payment.get("order_id")
    amount_paisa = payment.get("amount", 0)
    currency = payment.get("currency", "INR")
    payment_method = payment.get("method")
    wallet = payment.get("wallet")
    error_code = payment.get("error_code")
    error_source = payment.get("error_source")
    error_step = payment.get("error_step")
    error_reason = payment.get("error_reason")
    error_description = payment.get("error_description")
    diagnosis_category = diagnosis.get("category")
    diagnosis_text = diagnosis.get("diagnosis")
    is_recoverable = 1 if diagnosis.get("recoverable") else 0
    recommended_action = diagnosis.get("recommended_action")
    max_retries = diagnosis.get("max_retries", 0)
    now = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    try:
        resolved_merchant_id = merchant_account_id
        if resolved_merchant_id is None and order_id:
            merchant_row = conn.execute(
                "SELECT merchant_account_id FROM merchant_orders WHERE order_id = ? LIMIT 1",
                (order_id,),
            ).fetchone()
            resolved_merchant_id = merchant_row["merchant_account_id"] if merchant_row else None

        existing_payment = conn.execute(
            "SELECT merchant_account_id FROM recovery_cases WHERE payment_id = ? LIMIT 1",
            (payment_id,),
        ).fetchone()
        if (
            existing_payment
            and resolved_merchant_id is not None
            and existing_payment["merchant_account_id"] != resolved_merchant_id
        ):
            raise ValueError("Payment ID already belongs to another merchant")

        if resolved_merchant_id is None:
            row = conn.execute(
                "SELECT * FROM recovery_cases WHERE payment_id = ? AND merchant_account_id IS NULL",
                (payment_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM recovery_cases WHERE payment_id = ? AND merchant_account_id = ?",
                (payment_id, resolved_merchant_id),
            ).fetchone()
        if row:
            case = dict(row)
            case["amount"] = case["amount"] / 100.0
            case["recovered_amount"] = case["recovered_amount"] / 100.0
            return case, False

        conn.execute(
            """
            INSERT INTO recovery_cases (
                payment_id, order_id, amount, currency, payment_method, wallet,
                error_code, error_source, error_step, error_reason, error_description,
                diagnosis_category, diagnosis_text, is_recoverable, recommended_action,
                max_retries, retry_count, recovery_status, action_taken, action_result,
                payment_link_id, payment_link_url, ai_explanation, customer_message,
                recovered_amount, created_at, updated_at, merchant_account_id
            ) VALUES (
    ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?
)
            """,
            (
                payment_id, order_id, amount_paisa, currency, payment_method, wallet,
                error_code, error_source, error_step, error_reason, error_description,
                diagnosis_category, diagnosis_text, is_recoverable, recommended_action,
                max_retries, 0, "DETECTED", "", "", "", "", "", "", 0,
                now, now, resolved_merchant_id,
            ),
        )
        case_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
        new_case = dict(conn.execute("SELECT * FROM recovery_cases WHERE id = ?", (case_id,)).fetchone())
    finally:
        conn.close()

    new_case["amount"] = new_case["amount"] / 100.0
    new_case["recovered_amount"] = new_case["recovered_amount"] / 100.0

    add_audit_log(
        payment_id=payment_id,
        case_id=case_id,
        merchant_account_id=resolved_merchant_id,
        actor="SYSTEM",
        action="PAYMENT_FAILURE_DETECTED",
        details=f"Payment failure received for Rs. {new_case['amount']} (Method: {payment_method}, Reason: {error_reason})",
    )
    add_audit_log(
        payment_id=payment_id,
        case_id=case_id,
        merchant_account_id=merchant_account_id,
        actor="DIAGNOSIS_RULE",
        action="DIAGNOSIS_PERFORMED",
        details=f"Classified as '{diagnosis_category}' (Recoverable: {bool(is_recoverable)}, Action: {recommended_action}, Max Retries: {max_retries})",
    )
    return new_case, True


def get_all_recovery_cases(merchant_account_id: int | None = None):
    conn = get_connection()
    try:
        if merchant_account_id is None:
            rows = conn.execute("SELECT * FROM recovery_cases ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute("SELECT * FROM recovery_cases WHERE merchant_account_id = ? ORDER BY id DESC", (merchant_account_id,)).fetchall()
        result = [dict(row) for row in rows]
    finally:
        conn.close()
    for row in result:
        row["amount"] = row["amount"] / 100.0
        row["recovered_amount"] = row["recovered_amount"] / 100.0
    return result


def reset_batch_demo_cases(prefix: str = "demo_batch_v1_") -> None:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT id FROM recovery_cases WHERE payment_id LIKE ?", (f"{prefix}%",)).fetchall()
        case_ids = [row["id"] for row in rows]
        if case_ids:
            placeholders = ",".join("?" * len(case_ids))
            conn.execute(f"DELETE FROM audit_trail WHERE case_id IN ({placeholders})", case_ids)
        conn.execute("DELETE FROM recovery_cases WHERE payment_id LIKE ?", (f"{prefix}%",))
        conn.commit()
    finally:
        conn.close()


def get_case_by_payment_id(payment_id: str, merchant_account_id: int | None = None):
    conn = get_connection()
    try:
        if merchant_account_id is None:
            row = conn.execute("SELECT * FROM recovery_cases WHERE payment_id = ?", (payment_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM recovery_cases WHERE payment_id = ? AND merchant_account_id = ?", (payment_id, merchant_account_id)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    case = dict(row)
    case["amount"] = case["amount"] / 100.0
    case["recovered_amount"] = case["recovered_amount"] / 100.0
    return case


def update_case_policy(case_id: int, payment_id: str, policy_result: dict, merchant_account_id: int | None = None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    next_status = policy_result.get("next_status", "ESCALATED")
    action_allowed = policy_result.get("action_allowed", "escalate_manual_review")
    decision = policy_result.get("decision", "ESCALATE_MANUAL_REVIEW")
    reason = policy_result.get("reason", "")
    guardrail = policy_result.get("guardrail_triggered")
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE recovery_cases
            SET recovery_status = ?, action_taken = ?, updated_at = ?
            WHERE id = ? AND payment_id = ? AND recovery_status != 'RECOVERED'
              AND (? IS NULL OR merchant_account_id = ?)
            """,
            (next_status, action_allowed, now, case_id, payment_id, merchant_account_id, merchant_account_id),
        )
        conn.commit()
        updated_case = dict(conn.execute("SELECT * FROM recovery_cases WHERE id = ?", (case_id,)).fetchone())
    finally:
        conn.close()
    updated_case["amount"] = updated_case["amount"] / 100.0
    updated_case["recovered_amount"] = updated_case["recovered_amount"] / 100.0
    guardrail_note = f" [Guardrail: {guardrail}]" if guardrail else ""
    if cursor.rowcount == 1:
        add_audit_log(payment_id, "POLICY_GATE", f"POLICY_{decision}", f"Allowed Action: '{action_allowed}' -> Status: '{next_status}'. Rationale: {reason}{guardrail_note}", case_id=case_id)
    return updated_case


def increment_retry_count(case_id: int, payment_id: str, merchant_account_id: int | None = None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE recovery_cases
            SET retry_count = retry_count + 1, updated_at = ?
            WHERE id = ? AND payment_id = ? AND recovery_status != 'RECOVERED'
              AND (? IS NULL OR merchant_account_id = ?)
            """,
            (now, case_id, payment_id, merchant_account_id, merchant_account_id),
        )
        conn.commit()
        updated_case = dict(conn.execute("SELECT * FROM recovery_cases WHERE id = ?", (case_id,)).fetchone())
    finally:
        conn.close()
    updated_case["amount"] = updated_case["amount"] / 100.0
    updated_case["recovered_amount"] = updated_case["recovered_amount"] / 100.0
    if cursor.rowcount == 1:
        add_audit_log(payment_id, "RECOVERY_ENGINE", "RETRY_ATTEMPT_RECORDED", f"Retry attempt #{updated_case['retry_count']} executed of max {updated_case['max_retries']}.", case_id=case_id)
    return updated_case


def update_case_recovery_action(case_id: int, payment_id: str, recovery_status: str, action_result: str, payment_link_id: str = None, payment_link_url: str = None, merchant_account_id: int | None = None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE recovery_cases
            SET recovery_status = ?, action_result = ?,
                payment_link_id = COALESCE(?, payment_link_id),
                payment_link_url = COALESCE(?, payment_link_url),
                updated_at = ?
            WHERE id = ? AND payment_id = ? AND recovery_status != 'RECOVERED'
              AND (? IS NULL OR merchant_account_id = ?)
            """,
            (recovery_status, action_result, payment_link_id, payment_link_url, now, case_id, payment_id, merchant_account_id, merchant_account_id),
        )
        conn.commit()
        updated_case = dict(conn.execute("SELECT * FROM recovery_cases WHERE id = ?", (case_id,)).fetchone())
    finally:
        conn.close()
    updated_case["amount"] = updated_case["amount"] / 100.0
    updated_case["recovered_amount"] = updated_case["recovered_amount"] / 100.0
    return updated_case


def find_case_for_recovery_event(order_id: str = None, payment_link_id: str = None, original_payment_id: str = None):
    conn = get_connection()
    try:
        row = None
        if payment_link_id:
            rows = conn.execute(
                "SELECT * FROM recovery_cases WHERE payment_link_id = ? ORDER BY id DESC",
                (payment_link_id,),
            ).fetchall()
            row = rows[0] if len(rows) == 1 else None
        if not row and original_payment_id:
            row = conn.execute("SELECT * FROM recovery_cases WHERE payment_id = ? ORDER BY id DESC LIMIT 1", (original_payment_id,)).fetchone()
        if not row and order_id:
            rows = conn.execute("SELECT * FROM recovery_cases WHERE order_id = ? AND recovery_status != 'RECOVERED' ORDER BY id DESC LIMIT 2", (order_id,)).fetchall()
            row = rows[0] if len(rows) == 1 else None
    finally:
        conn.close()
    if not row:
        return None
    case = dict(row)
    case["amount"] = case["amount"] / 100.0
    case["recovered_amount"] = case["recovered_amount"] / 100.0
    return case


def find_recovery_cases_by_payment_link_id(payment_link_id: str, merchant_account_id: int | None = None) -> list[dict]:
    conn = get_connection()
    try:
        if merchant_account_id is None:
            rows = conn.execute(
                "SELECT * FROM recovery_cases WHERE payment_link_id = ? ORDER BY id ASC",
                (payment_link_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM recovery_cases WHERE payment_link_id = ? AND merchant_account_id = ? ORDER BY id ASC",
                (payment_link_id, merchant_account_id),
            ).fetchall()
        result = [dict(row) for row in rows]
    finally:
        conn.close()
    for row in result:
        row["amount"] = row["amount"] / 100.0
        row["recovered_amount"] = row["recovered_amount"] / 100.0
    return result


def find_case_for_captured_payment(payment_id: str = None, order_id: str = None):
    conn = get_connection()
    try:
        if payment_id:
            row = conn.execute("SELECT * FROM recovery_cases WHERE payment_id = ? LIMIT 1", (payment_id,)).fetchone()
        elif order_id:
            rows = conn.execute("SELECT * FROM recovery_cases WHERE order_id = ? AND recovery_status != 'RECOVERED'", (order_id,)).fetchall()
            row = rows[0] if len(rows) == 1 else None
        else:
            row = None
    finally:
        conn.close()
    if not row:
        return None
    case = dict(row)
    case["amount"] = case["amount"] / 100.0
    case["recovered_amount"] = case["recovered_amount"] / 100.0
    return case


def mark_case_recovered_paisa(
    case_id: int,
    payment_id: str,
    recovered_amount_paisa: int,
    new_payment_id: str,
    event_type: str,
    recovery_source: str | None = None,
    event_id: str | None = None,
    merchant_account_id: int | None = None,
) -> tuple[dict, bool]:
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE recovery_cases
            SET recovery_status = 'RECOVERED', recovered_amount = ?, action_result = ?,
                recovery_source = ?, recovered_payment_id = ?, recovered_event_id = ?,
                recovery_confirmed_at = ?, updated_at = ?
            WHERE id = ? AND recovery_status != 'RECOVERED'
              AND payment_id = ?
              AND ? >= 0
              AND ? <= amount
              AND (? IS NULL OR merchant_account_id = ?)
            """,
            (
                recovered_amount_paisa,
                f"RECOVERED via {event_type} ({new_payment_id})",
                recovery_source,
                new_payment_id,
                event_id,
                now,
                now,
                case_id,
                payment_id,
                recovered_amount_paisa,
                recovered_amount_paisa,
                merchant_account_id,
                merchant_account_id,
            ),
        )
        changed = cursor.rowcount == 1
        conn.commit()
        updated_case = dict(conn.execute("SELECT * FROM recovery_cases WHERE id = ?", (case_id,)).fetchone())
    finally:
        conn.close()
    updated_case["amount"] = updated_case["amount"] / 100.0
    updated_case["recovered_amount"] = updated_case["recovered_amount"] / 100.0
    if changed:
        add_audit_log(
            payment_id,
            "RECOVERY_ENGINE",
            "REVENUE_RECOVERED",
            f"RECOVERY_CONFIRMED: {recovery_source or 'VERIFIED_PAYMENT'} recovered Rs. {updated_case['recovered_amount']} from successful {event_type} event ({new_payment_id}).",
            case_id=case_id,
        )
    return updated_case, changed


def mark_case_recovered(case_id: int, payment_id: str, recovered_amount: float, new_payment_id: str, event_type: str) -> dict:
    updated_case, _ = mark_case_recovered_paisa(case_id, payment_id, int(round(recovered_amount * 100)), new_payment_id, event_type)
    return updated_case


def update_case_ai_insights(case_id: int, payment_id: str, ai_explanation: str, customer_message: str, provider: str = "template") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE recovery_cases
            SET ai_explanation = ?, customer_message = ?, updated_at = ?
            WHERE id = ? AND payment_id = ? AND recovery_status != 'RECOVERED'
            """,
            (ai_explanation, customer_message, now, case_id, payment_id),
        )
        conn.commit()
        updated_case = dict(conn.execute("SELECT * FROM recovery_cases WHERE id = ?", (case_id,)).fetchone())
    finally:
        conn.close()
    updated_case["amount"] = updated_case["amount"] / 100.0
    updated_case["recovered_amount"] = updated_case["recovered_amount"] / 100.0
    preview = (ai_explanation[:80] + "...") if len(ai_explanation) > 80 else ai_explanation
    add_audit_log(payment_id, "AI_AGENT", "AI_EXPLANATION_GENERATED", f"[{provider.upper()}] {preview}", case_id=case_id)
    return updated_case


def get_audit_trail_for_case(payment_id: str, merchant_account_id: int | None = None):
    conn = get_connection()
    try:
        if merchant_account_id is None:
            rows = conn.execute("SELECT * FROM audit_trail WHERE payment_id = ? ORDER BY id ASC", (payment_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM audit_trail WHERE payment_id = ? AND merchant_account_id = ? ORDER BY id ASC", (payment_id, merchant_account_id)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_recovery_metrics(merchant_account_id: int | None = None) -> dict:
    conn = get_connection()
    try:
        if merchant_account_id is None:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(amount),0) total_at_risk,
                       COALESCE(SUM(recovered_amount),0) total_recovered,
                       COUNT(id) total_cases,
                       COALESCE(SUM(CASE WHEN recovery_status='RECOVERED' THEN 1 ELSE 0 END),0) recovered_cases,
                       COALESCE(SUM(CASE WHEN recovery_status='ESCALATED' THEN 1 ELSE 0 END),0) escalated_cases,
                       COALESCE(SUM(CASE WHEN action_taken='retry_payment' THEN 1 ELSE 0 END),0) retry_actions,
                       COALESCE(SUM(CASE WHEN action_taken='send_payment_reminder' THEN 1 ELSE 0 END),0) reminder_actions,
                       COALESCE(SUM(CASE WHEN action_taken='escalate_manual_review' THEN 1 ELSE 0 END),0) manual_escalations,
                       COALESCE(SUM(CASE WHEN recovery_status IN ('DETECTED','PENDING_RETRY','PENDING_REMINDER','LINK_CREATED') THEN 1 ELSE 0 END),0) pending_cases
                FROM recovery_cases
                """
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(amount),0) total_at_risk,
                       COALESCE(SUM(recovered_amount),0) total_recovered,
                       COUNT(id) total_cases,
                       COALESCE(SUM(CASE WHEN recovery_status='RECOVERED' THEN 1 ELSE 0 END),0) recovered_cases,
                       COALESCE(SUM(CASE WHEN recovery_status='ESCALATED' THEN 1 ELSE 0 END),0) escalated_cases,
                       COALESCE(SUM(CASE WHEN action_taken='retry_payment' THEN 1 ELSE 0 END),0) retry_actions,
                       COALESCE(SUM(CASE WHEN action_taken='send_payment_reminder' THEN 1 ELSE 0 END),0) reminder_actions,
                       COALESCE(SUM(CASE WHEN action_taken='escalate_manual_review' THEN 1 ELSE 0 END),0) manual_escalations,
                       COALESCE(SUM(CASE WHEN recovery_status IN ('DETECTED','PENDING_RETRY','PENDING_REMINDER','LINK_CREATED') THEN 1 ELSE 0 END),0) pending_cases
                FROM recovery_cases WHERE merchant_account_id = ?
                """,
                (merchant_account_id,),
            ).fetchone()
    finally:
        conn.close()
    total_at_risk = float(row["total_at_risk"]) / 100.0
    total_recovered = float(row["total_recovered"]) / 100.0
    captured_revenue_paisa = get_captured_revenue(merchant_account_id=merchant_account_id)
    successful_count = len(get_successful_payments(merchant_account_id=merchant_account_id))
    return {
        "total_revenue_at_risk": round(total_at_risk, 2),
        "total_revenue_recovered": round(total_recovered, 2),
        "recovery_rate_percentage": round(total_recovered / total_at_risk * 100.0, 1) if total_at_risk else 0.0,
        "total_cases": int(row["total_cases"]),
        "recovered_cases": int(row["recovered_cases"]),
        "escalated_cases": int(row["escalated_cases"]),
        "retry_actions": int(row["retry_actions"]),
        "reminder_actions": int(row["reminder_actions"]),
        "manual_escalations": int(row["manual_escalations"]),
        "pending_cases": int(row["pending_cases"]),
        "total_captured_revenue": round(captured_revenue_paisa / 100.0, 2),
        "captured_payments": successful_count,
        "revenue_still_at_risk": round(max(total_at_risk - total_recovered, 0.0), 2),
    }
