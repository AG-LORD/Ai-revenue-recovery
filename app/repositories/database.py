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
    """Initializes database tables if they do not already exist.
    Tables:
    1. webhook_events: Stores incoming webhook events to guarantee idempotency.
    2. recovery_cases: Stores failed payments, diagnoses, policy state, and recovery outcomes.
    3. audit_trail: Immutable event log recording every detection, diagnosis, and action.
    """
    conn = get_connection()
    cursor = conn.cursor()

    # 1. Webhook Events Table (for Idempotency)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS webhook_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT UNIQUE NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL,
            received_at TEXT NOT NULL
        );
        """,
    )

    # 2. Recovery Cases Table (Core Financial & Recovery State)
    # Check if the old table exists and has the old schema (REAL columns)
    cursor.execute("PRAGMA table_info(recovery_cases)")
    columns = {col[1]: col[2] for col in cursor.fetchall()}
    need_to_migrate = False
    if "amount" in columns and columns["amount"].upper() == "REAL":
        need_to_migrate = True
    if "recovered_amount" in columns and columns["recovered_amount"].upper() == "REAL":
        need_to_migrate = True

    if need_to_migrate:
        # Rename the old table
        cursor.execute("ALTER TABLE recovery_cases RENAME TO recovery_cases_old")
        # Create the new table with INTEGER columns
        cursor.execute(
            """
            CREATE TABLE recovery_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id TEXT UNIQUE NOT NULL,
                order_id TEXT,
                amount INTEGER NOT NULL,  -- Stores paisa
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
                recovered_amount INTEGER NOT NULL DEFAULT 0,  -- Stores paisa
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """,
        )
        # Copy the data from the old table, converting amounts from REAL to INTEGER paisa
        cursor.execute(
            """
            INSERT INTO recovery_cases (
                id, payment_id, order_id, amount, currency, payment_method, wallet,
                error_code, error_source, error_step, error_reason, error_description,
                diagnosis_category, diagnosis_text, is_recoverable, recommended_action,
                max_retries, retry_count, recovery_status, action_taken, action_result,
                payment_link_id, payment_link_url, ai_explanation, customer_message,
                recovered_amount, created_at, updated_at
            ) SELECT
                id, payment_id, order_id,
                ROUND(amount * 100) as amount,  -- Convert rupees to paisa
                currency, payment_method, wallet,
                error_code, error_source, error_step, error_reason, error_description,
                diagnosis_category, diagnosis_text, is_recoverable, recommended_action,
                max_retries, retry_count, recovery_status, action_taken, action_result,
                payment_link_id, payment_link_url, ai_explanation, customer_message,
                ROUND(recovered_amount * 100) as recovered_amount,  -- Convert rupees to paisa
                created_at, updated_at
            FROM recovery_cases_old
            """,
        )
        # Drop the old table
        cursor.execute("DROP TABLE recovery_cases_old")
    else:
        # If no migration needed, create the table if it doesn't exist
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS recovery_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id TEXT UNIQUE NOT NULL,
                order_id TEXT,
                amount INTEGER NOT NULL,  -- Stores paisa
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
                recovered_amount INTEGER NOT NULL DEFAULT 0,  -- Stores paisa
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """,
        )

    # 3. Audit Trail Table (Immutable Decision Log)
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
            FOREIGN KEY (case_id) REFERENCES recovery_cases(id)
        );
        """,
    )

    conn.commit()
    conn.close()
    logger.info("Database initialized: %s", DATABASE_PATH)
def is_event_processed(event_id: str) -> bool:
    """Check if a webhook event ID has already been recorded and processed.
    Returns True if already processed, False otherwise.
    """
    if not event_id:
        return False
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM webhook_events WHERE event_id = ?", (event_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def record_webhook_event(event_id: str, event_type: str, payload_dict: dict, status: str = "PROCESSED") -> bool:
    """Atomically record a webhook event; return False if it was already seen."""
    now = datetime.now(timezone.utc).isoformat()
    payload_json = json.dumps(payload_dict)
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """INSERT INTO webhook_events (event_id, event_type, payload, status, received_at)
            VALUES (?, ?, ?, ?, ?)""",
            (event_id, event_type, payload_json, status, now),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def update_webhook_event_status(event_id: str, status: str) -> None:
    """Update the status of an already-reserved webhook event."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE webhook_events SET status = ? WHERE event_id = ?", (status, event_id))
    conn.commit()
    conn.close()


def release_processing_webhook_event(event_id: str) -> None:
    """Release a failed processing reservation so the gateway can retry it."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM webhook_events WHERE event_id = ? AND status = 'PROCESSING'",
        (event_id,),
    )
    conn.commit()
    conn.close()


def add_audit_log(payment_id: str, actor: str, action: str, details: str, case_id: int = None):
    """Append an immutable log entry to the audit_trail table.
    Actors can be: 'SYSTEM', 'DIAGNOSIS_RULE', 'POLICY_GATE', 'AI_AGENT', 'RECOVERY_ENGINE'.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO audit_trail (case_id, payment_id, actor, action, details, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (case_id, payment_id, actor, action, details, now),
    )
    conn.commit()
    conn.close()

# The rest of the original functions are copied verbatim below.

def create_or_get_recovery_case(payment: dict, diagnosis: dict) -> tuple[dict, bool]:
    payment_id = payment.get("id")
    order_id = payment.get("order_id")
    amount_paisa = payment.get("amount", 0)  # Store in paisa
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
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM recovery_cases WHERE payment_id = ?", (payment_id,))
    existing = cursor.fetchone()
    if existing:
        # Convert existing case from paisa to rupees for the rest of the app
        existing_dict = dict(existing)
        existing_dict['amount'] = existing_dict['amount'] / 100.0
        existing_dict['recovered_amount'] = existing_dict['recovered_amount'] / 100.0
        case_dict = existing_dict
        conn.close()
        return case_dict, False

    cursor.execute("""
        INSERT INTO recovery_cases (
            payment_id, order_id, amount, currency, payment_method, wallet,
            error_code, error_source, error_step, error_reason, error_description,
            diagnosis_category, diagnosis_text, is_recoverable, recommended_action,
            max_retries, retry_count, recovery_status, action_taken, action_result,
            payment_link_id, payment_link_url, ai_explanation, customer_message,
            recovered_amount, created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
    """, (
        payment_id,
        order_id,
        amount_paisa,
        currency,
        payment_method,
        wallet,
        error_code,
        error_source,
        error_step,
        error_reason,
        error_description,
        diagnosis_category,
        diagnosis_text,
        is_recoverable,
        recommended_action,
        max_retries,
        0,  # retry_count
        'DETECTED',  # recovery_status
        '',  # action_taken
        '',  # action_result
        '',  # payment_link_id
        '',  # payment_link_url
        '',  # ai_explanation
        '',  # customer_message
        0,  # recovered_amount in paisa
        now,
        now,
    ))
    case_id = cursor.lastrowid
    conn.commit()
    cursor.execute("SELECT * FROM recovery_cases WHERE id = ?", (case_id,))
    new_case = dict(cursor.fetchone())
    # Convert new case from paisa to rupees for the rest of the app
    new_case['amount'] = new_case['amount'] / 100.0
    new_case['recovered_amount'] = new_case['recovered_amount'] / 100.0
    conn.close()

    add_audit_log(
        payment_id=payment_id,
        case_id=case_id,
        actor="SYSTEM",
        action="PAYMENT_FAILURE_DETECTED",
        details=f"Payment failure received for Rs. {new_case['amount']} (Method: {payment_method}, Reason: {error_reason})",
    )
    add_audit_log(
        payment_id=payment_id,
        case_id=case_id,
        actor="DIAGNOSIS_RULE",
        action="DIAGNOSIS_PERFORMED",
        details=f"Classified as '{diagnosis_category}' (Recoverable: {bool(is_recoverable)}, Action: {recommended_action}, Max Retries: {max_retries})\n",
    )
    return new_case, True
def get_all_recovery_cases():
    """Fetch all recovery cases from the database."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM recovery_cases ORDER BY id DESC")
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    # Convert each case from paisa to rupees
    for row in rows:
        row['amount'] = row['amount'] / 100.0
        row['recovered_amount'] = row['recovered_amount'] / 100.0
    return rows
def get_case_by_payment_id(payment_id: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM recovery_cases WHERE payment_id = ?", (payment_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        case = dict(row)
        # Convert from paisa to rupees
        case['amount'] = case['amount'] / 100.0
        case['recovered_amount'] = case['recovered_amount'] / 100.0
        return case
    return None
def update_case_policy(case_id: int, payment_id: str, policy_result: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    next_status = policy_result.get("next_status", "ESCALATED")
    action_allowed = policy_result.get("action_allowed", "escalate_manual_review")
    decision = policy_result.get("decision", "ESCALATE_MANUAL_REVIEW")
    reason = policy_result.get("reason", "")
    guardrail = policy_result.get("guardrail_triggered")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE recovery_cases
        SET recovery_status = ?,
            action_taken = ?,
            updated_at = ?
        WHERE id = ?
    """, (
        next_status,
        action_allowed,
        now,
        case_id,
    ))
    conn.commit()
    cursor.execute("SELECT * FROM recovery_cases WHERE id = ?", (case_id,))
    updated_case = dict(cursor.fetchone())
    # Convert from paisa to rupees
    updated_case['amount'] = updated_case['amount'] / 100.0
    updated_case['recovered_amount'] = updated_case['recovered_amount'] / 100.0
    conn.close()
    guardrail_note = f" [Guardrail: {guardrail}]" if guardrail else ""
    add_audit_log(
        payment_id=payment_id,
        case_id=case_id,
        actor="POLICY_GATE",
        action=f"POLICY_{decision}",
        details=f"Allowed Action: '{action_allowed}' -> Status: '{next_status}'. Rationale: {reason}{guardrail_note}",
    )
    return updated_case
def increment_retry_count(case_id: int, payment_id: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE recovery_cases
        SET retry_count = retry_count + 1,
            updated_at = ?
        WHERE id = ?
    """, (
        now,
        case_id,
    ))
    conn.commit()
    cursor.execute("SELECT * FROM recovery_cases WHERE id = ?", (case_id,))
    updated_case = dict(cursor.fetchone())
    # Convert from paisa to rupees
    updated_case['amount'] = updated_case['amount'] / 100.0
    updated_case['recovered_amount'] = updated_case['recovered_amount'] / 100.0
    conn.close()
    add_audit_log(
        payment_id=payment_id,
        case_id=case_id,
        actor="RECOVERY_ENGINE",
        action="RETRY_ATTEMPT_RECORDED",
        details=f"Retry attempt #{updated_case['retry_count']} executed of max {updated_case['max_retries']}.",
    )
    return updated_case
def update_case_recovery_action(case_id: int, payment_id: str, recovery_status: str, action_result: str, payment_link_id: str = None, payment_link_url: str = None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE recovery_cases
        SET recovery_status = ?,
            action_result = ?,
            payment_link_id = COALESCE(?, payment_link_id),
            payment_link_url = COALESCE(?, payment_link_url),
            updated_at = ?
        WHERE id = ?
    """, (
        recovery_status,
        action_result,
        payment_link_id,
        payment_link_url,
        now,
        case_id,
    ))
    conn.commit()
    cursor.execute("SELECT * FROM recovery_cases WHERE id = ?", (case_id,))
    updated_case = dict(cursor.fetchone())
    # Convert from paisa to rupees
    updated_case['amount'] = updated_case['amount'] / 100.0
    updated_case['recovered_amount'] = updated_case['recovered_amount'] / 100.0
    conn.close()
    return updated_case
def find_case_for_recovery_event(order_id: str = None, payment_link_id: str = None, original_payment_id: str = None):
    conn = get_connection()
    cursor = conn.cursor()
    case = None
    if payment_link_id:
        cursor.execute("SELECT * FROM recovery_cases WHERE payment_link_id = ? ORDER BY id DESC LIMIT 1", (payment_link_id,))
        case = cursor.fetchone()
    if not case and original_payment_id:
        cursor.execute("SELECT * FROM recovery_cases WHERE payment_id = ? ORDER BY id DESC LIMIT 1", (original_payment_id,))
        case = cursor.fetchone()
    if not case and order_id:
        cursor.execute("SELECT * FROM recovery_cases WHERE order_id = ? AND recovery_status != 'RECOVERED' ORDER BY id DESC LIMIT 1", (order_id,))
        case = cursor.fetchone()
    conn.close()
    if case:
        case = dict(case)
        # Convert from paisa to rupees
        case['amount'] = case['amount'] / 100.0
        case['recovered_amount'] = case['recovered_amount'] / 100.0
    return case
def mark_case_recovered(case_id: int, payment_id: str, recovered_amount: float, new_payment_id: str, event_type: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    # Convert recovered_amount from rupees to paisa for storage
    recovered_amount_paisa = int(round(recovered_amount * 100))
    cursor.execute("""
        UPDATE recovery_cases
        SET recovery_status = 'RECOVERED',
            recovered_amount = ?,
            action_result = ?,
            updated_at = ?
        WHERE id = ?
    """, (
        recovered_amount_paisa,
        f"RECOVERED via {event_type} ({new_payment_id})",
        now,
        case_id,
    ))
    conn.commit()
    cursor.execute("SELECT * FROM recovery_cases WHERE id = ?", (case_id,))
    updated_case = dict(cursor.fetchone())
    # Convert from paisa to rupees for the rest of the app
    updated_case['amount'] = updated_case['amount'] / 100.0
    updated_case['recovered_amount'] = updated_case['recovered_amount'] / 100.0
    conn.close()
    add_audit_log(
        payment_id=payment_id,
        case_id=case_id,
        actor="RECOVERY_ENGINE",
        action="REVENUE_RECOVERED",
        details=f"Successfully recovered Rs. {updated_case['amount']} via {event_type}. New Payment ID: {new_payment_id}",
    )
    return updated_case
def update_case_ai_insights(case_id: int, payment_id: str, ai_explanation: str, customer_message: str, provider: str = "template") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE recovery_cases
        SET ai_explanation = ?,
            customer_message = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (ai_explanation, customer_message, now, case_id),
    )
    conn.commit()
    cursor.execute("SELECT * FROM recovery_cases WHERE id = ?", (case_id,))
    updated_case = dict(cursor.fetchone())
    conn.close()
    preview = (ai_explanation[:80] + "...") if len(ai_explanation) > 80 else ai_explanation
    add_audit_log(
        payment_id=payment_id,
        case_id=case_id,
        actor="AI_AGENT",
        action="AI_EXPLANATION_GENERATED",
        details=f"[{provider.upper()}] {preview}",
    )
    return updated_case

def get_audit_trail_for_case(payment_id: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM audit_trail WHERE payment_id = ? ORDER BY id ASC",
        (payment_id,)
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def get_recovery_metrics() -> dict:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 
            COALESCE(SUM(amount), 0) as total_at_risk,
            COALESCE(SUM(recovered_amount), 0) as total_recovered,
            COUNT(id) as total_cases,
            COALESCE(SUM(CASE WHEN recovery_status = 'RECOVERED' THEN 1 ELSE 0 END), 0) as recovered_cases,
            COALESCE(SUM(CASE WHEN recovery_status = 'ESCALATED' THEN 1 ELSE 0 END), 0) as escalated_cases,
            COALESCE(SUM(CASE WHEN recovery_status IN ('DETECTED', 'PENDING_RETRY', 'PENDING_REMINDER', 'LINK_CREATED') THEN 1 ELSE 0 END), 0) as pending_cases
        FROM recovery_cases
    """)
    row = cursor.fetchone()
    conn.close()
    # Convert from paisa to rupees
    total_at_risk = float(row["total_at_risk"]) / 100.0
    total_recovered = float(row["total_recovered"]) / 100.0
    recovery_rate = (total_recovered / total_at_risk * 100.0) if total_at_risk > 0 else 0.0
    return {
        "total_revenue_at_risk": round(total_at_risk, 2),
        "total_revenue_recovered": round(total_recovered, 2),
        "recovery_rate_percentage": round(recovery_rate, 1),
        "total_cases": int(row["total_cases"]),
        "recovered_cases": int(row["recovered_cases"]),
        "escalated_cases": int(row["escalated_cases"]),
        "pending_cases": int(row["pending_cases"]),
    }
