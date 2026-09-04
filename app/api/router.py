# app/api/router.py
"""FastAPI router containing all endpoint definitions.
This file replaces the previous route definitions that lived in ``main.py``.
It imports business‑logic services from the new package layout.
"""
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
import logging
import time

# Import config for static paths and credentials
from app.core.config import RAZORPAY_KEY_ID, RAZORPAY_WEBHOOK_SECRET

# Services
from app.services.recovery_service import (
    process_recovery_success_event,
    reconcile_successful_payment_event,
)
from app.services.workflow_service import process_failed_payment
from app.services.merchant_service import DEFAULT_MERCHANT_KEY, get_merchant_by_key
from scripts import run_batch_demo

# Repository functions (database layer)
from app.repositories.database import (
    record_webhook_event,
    update_webhook_event_status,
    release_processing_webhook_event,
    add_audit_log,
    get_all_recovery_cases,
    get_audit_trail_for_case,
    get_recovery_metrics,
    get_case_by_payment_id,
    get_payment_lifecycle,
    record_payment_event,
    retry_failed_payment_event,
    update_payment_event_status,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _default_merchant_id() -> int:
    merchant = get_merchant_by_key(DEFAULT_MERCHANT_KEY)
    if not merchant:
        raise HTTPException(status_code=503, detail="Default merchant is unavailable")
    return merchant["id"]


def _payment_event_details(data: dict) -> dict:
    payload = data.get("payload", {})
    payment = payload.get("payment", {}).get("entity", {})
    payment_link = payload.get("payment_link", {}).get("entity", {})
    order = payload.get("order", {}).get("entity", {})
    return {
        "payment": payment,
        "payment_id": payment.get("id") or order.get("payment_id"),
        "order_id": payment.get("order_id") or order.get("id"),
        "amount_paise": (
            payment.get("amount")
            or payment_link.get("amount_paid")
            or payment_link.get("amount")
            or order.get("amount_paid")
            or order.get("amount")
        ),
        "currency": payment.get("currency") or payment_link.get("currency") or order.get("currency"),
        "payment_status": payment.get("status") or payment_link.get("status") or order.get("status"),
    }


def _record_simulation_payment_event(data: dict) -> None:
    details = _payment_event_details(data)
    event_type = data["event"]
    event_id = data["id"]
    record_payment_event(
        event_id=event_id,
        event_type=event_type,
        payload_dict=data,
        source="simulation",
        payment_id=details["payment_id"],
        order_id=details["order_id"],
        amount_paise=details["amount_paise"],
        currency=details["currency"],
        payment_status=details["payment_status"],
    )


@router.get("/health")
async def health():
    return {"status": "healthy"}

@router.get("/api/metrics")
async def api_metrics():
    return get_recovery_metrics(merchant_account_id=_default_merchant_id())

@router.post("/api/demo/run-batch")
async def run_batch_demo_api():
    """Run the deterministic 50-payment batch demo through the normal workflow."""
    try:
        events = run_batch_demo.generate_synthetic_failures()
        report = run_batch_demo.process_batch(events)
        return {
            "status": "ok",
            "message": "50-payment recovery batch completed",
            "report": report,
        }
    except Exception:
        logger.exception("Batch demo failed")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": "Batch demo failed",
            },
        )

@router.get("/api/cases")
async def list_api_cases():
    cases = get_all_recovery_cases(merchant_account_id=_default_merchant_id())
    return {"total_cases": len(cases), "cases": cases}


@router.get("/api/cases/{payment_id}")
async def get_api_case(payment_id: str):
    """Return non-sensitive authoritative lifecycle and recovery state."""
    merchant_account_id = _default_merchant_id()
    case = get_case_by_payment_id(payment_id, merchant_account_id=merchant_account_id)
    lifecycle = get_payment_lifecycle(payment_id, merchant_account_id=merchant_account_id)
    if not case and not lifecycle["events"]:
        legacy_lifecycle = get_payment_lifecycle(payment_id)
        if legacy_lifecycle["events"] and all(
            event["merchant_account_id"] in (None, merchant_account_id)
            for event in legacy_lifecycle["events"]
        ):
            lifecycle = legacy_lifecycle
    if not case and not lifecycle["events"]:
        raise HTTPException(status_code=404, detail="Payment state not found")
    policy_decisions = {
        "retry_payment": "ALLOW_RETRY",
        "send_payment_reminder": "ALLOW_REMINDER",
        "escalate_manual_review": "ESCALATE_MANUAL_REVIEW",
    }
    allowed_action = case.get("action_taken") if case else None
    return {
        "payment_id": payment_id,
        "order_id": case.get("order_id") if case else None,
        "amount": case.get("amount") if case else lifecycle["amount_paise"] / 100.0,
        "currency": case.get("currency") if case else lifecycle["currency"],
        "diagnosis": case.get("diagnosis_text") if case else None,
        "policy_decision": policy_decisions.get(allowed_action),
        "allowed_action": allowed_action,
        "recovery_status": case.get("recovery_status") if case else None,
        "recovery_source": case.get("recovery_source") if case else None,
        "recovered_payment_id": case.get("recovered_payment_id") if case else None,
        "recovered_amount": case.get("recovered_amount", 0.0) if case else 0.0,
        "payment_lifecycle_status": lifecycle["status"],
        "is_successful": lifecycle["is_successful"],
        "ai_provider": None,
        "ai_generated": None,
    }


@router.get("/cases")
async def list_cases():
    cases = get_all_recovery_cases()
    return {"total_cases": len(cases), "cases": cases}

@router.get("/cases/{payment_id}/audit")
async def case_audit_trail(payment_id: str):
    trail = get_audit_trail_for_case(payment_id, merchant_account_id=_default_merchant_id())
    return {"payment_id": payment_id, "audit_trail": trail}

@router.post("/api/create-order")
async def create_dynamic_order(request: Request):
    """Create a fresh Razorpay order for checkout.
    The Razorpay client is accessed via the FastAPI ``state`` object.
    """
    razorpay_client = request.app.state.razorpay_client
    try:
        order_data = {
            "amount": 50000,
            "currency": "INR",
            "receipt": f"rcpt_{int(time.time())}",
        }
        order = razorpay_client.create_order(**order_data)
        return {
            "status": "success",
            "order_id": order["id"],
            "amount": order["amount"],
            "currency": order["currency"],
            "key_id": RAZORPAY_KEY_ID,
        }
    except Exception:
        logger.exception("Order creation failed")
        return JSONResponse(status_code=503, content={"status": "error", "message": "Order creation is currently unavailable"})

@router.post("/api/simulate")
async def simulate_scenario(request: Request):
    """Simulate Razorpay webhook scenarios for the demo UI.
    It runs the same logic as the real webhook handler but skips signature checks.
    """
    body_json = await request.json()
    scenario = body_json.get("scenario", "bank_failure")
    ts = int(time.time())
    # Build payloads for each scenario (same as original implementation)
    if scenario == "bank_failure":
        payload = {
            "entity": "event",
            "event": "payment.failed",
            "id": f"event_sim_bank_{ts}",
            "payload": {
                "payment": {
                    "entity": {
                        "id": f"pay_bank_{ts}",
"amount": 50000,
"currency": "INR",
"status": "failed",
"order_id": f"order_sim_{ts}",
"demo_scenario": "bank_failure",
"method": "wallet",
                        "wallet": "airtelmoney",
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_description": "Your payment didn't go through due to a temporary issue with the bank.",
                        "error_source": "issuer",
                        "error_step": "payment_authorization",
                        "error_reason": "payment_failed",
                    }
                }
            },
        }
    elif scenario == "cancellation":
        payload = {
            "entity": "event",
            "event": "payment.failed",
            "id": f"event_sim_cancel_{ts}",
            "payload": {
                "payment": {
                    "entity": {
                        "id": f"pay_cancel_{ts}",
                        "amount": 50000,
                        "currency": "INR",
                        "status": "failed",
                        "order_id": f"order_sim_{ts}",
                        "method": "wallet",
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_description": "Your payment has been cancelled.",
                        "error_source": "customer",
                        "error_step": "payment_authentication",
                        "error_reason": "payment_cancelled",
                    }
                }
            },
        }
    elif scenario == "link_paid":
        all_cases = get_all_recovery_cases(merchant_account_id=_default_merchant_id())
        link_case = next((c for c in all_cases if c["recovery_status"] == "LINK_CREATED"), None)
        if not link_case:
            raise HTTPException(status_code=409, detail="No LINK_CREATED recovery case is available")
        plink_id = link_case["payment_link_id"]
        orig_pay_id = link_case["payment_id"]
        ord_id = link_case["order_id"]
        amount_paise = int(round(link_case["amount"] * 100))
        currency = link_case["currency"]
        payload = {
            "entity": "event",
            "event": "payment_link.paid",
            "id": f"event_sim_paid_{ts}",
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": plink_id,
                        "amount": amount_paise,
                        "amount_paid": amount_paise,
                        "status": "paid",
                        "currency": currency,
                        "order_id": ord_id,
                        "notes": {"original_payment_id": orig_pay_id},
                    }
                },
                "payment": {
                    "entity": {
                        "id": f"pay_recovered_{ts}",
                        "order_id": ord_id,
                        "amount": amount_paise,
                        "currency": currency,
                        "status": "captured",
                        "notes": {"original_payment_id": orig_pay_id},
                    }
                },
            },
        }
    else:
        payload = {
            "entity": "event",
            "event": "payment.failed",
            "id": f"event_sim_unknown_{ts}",
            "payload": {
                "payment": {
                    "entity": {
                        "id": f"pay_unk_{ts}",
                        "amount": 50000,
                        "currency": "INR",
                        "status": "failed",
                        "order_id": f"order_sim_{ts}",
                        "method": "card",
                        "error_code": "UNEXPECTED_GATEWAY_ERR",
                        "error_description": "Unclassified gateway error",
                        "error_source": "gateway",
                        "error_step": "payment_capture",
                        "error_reason": "unhandled_code_99",
                    }
                }
            },
        }
    # Process payload using same workflow as webhook
    event_type = payload["event"]
    _record_simulation_payment_event(payload)
    if event_type == "payment.failed":
        pay_ent = payload["payload"]["payment"]["entity"]
        razorpay_client = request.app.state.razorpay_client
        outcome = process_failed_payment(pay_ent, razorpay_client)
        case = outcome["case"]
        return {"status": "ok", "message": f"{scenario} simulated successfully", "case": case}
    elif event_type == "payment_link.paid":
        rec_case, matched = process_recovery_success_event(event_type, payload)
        return {"status": "ok", "message": "payment_link.paid simulated", "recovered": matched, "case": rec_case}
    else:
        raise HTTPException(status_code=400, detail="Unknown simulation scenario")

@router.post("/webhook/razorpay")
async def razorpay_webhook(request: Request):
    body = await request.body()
    received_signature = request.headers.get("X-Razorpay-Signature")
    if not received_signature:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Missing signature"})
    razorpay_client = request.app.state.razorpay_client
    try:
        razorpay_client.verify_webhook_signature(body, received_signature, RAZORPAY_WEBHOOK_SECRET)
    except Exception:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid signature"})
    data = await request.json()
    event = data.get("event")
    event_id = data.get("id")
    supported_events = {
        "payment.failed",
        "payment.authorized",
        "payment.captured",
        "order.paid",
        "payment_link.paid",
    }
    if event not in supported_events:
        return {"status": "error", "message": "Unhandled event type"}

    details = _payment_event_details(data)
    payment = details["payment"]
    payment_id = details["payment_id"]
    if not event_id:
        event_id = f"event_{event}_{payment_id or details['order_id']}"
    if not record_webhook_event(event_id=event_id, event_type=event, payload_dict=data, status="PROCESSING"):
        add_audit_log(
            payment_id=payment_id,
            actor="SYSTEM",
            action="DUPLICATE_WEBHOOK_IGNORED",
            details=f"Received duplicate webhook event '{event_id}'. Skipped processing.",
        )
        return {"status": "ignored", "message": "Duplicate event already processed", "event_id": event_id}
    payment_event_recorded = record_payment_event(
        event_id=event_id,
        event_type=event,
        payload_dict=data,
        source="razorpay_webhook",
        payment_id=details["payment_id"],
        order_id=details["order_id"],
        amount_paise=details["amount_paise"],
        currency=details["currency"],
        payment_status=details["payment_status"],
        processing_status="PROCESSING",
    )
    if not payment_event_recorded and not retry_failed_payment_event(event_id):
        update_webhook_event_status(event_id, "PROCESSED")
        return {"status": "ignored", "message": "Duplicate payment event already stored", "event_id": event_id}

    if event == "payment.failed":
        try:
            outcome = process_failed_payment(payment, razorpay_client)
        except Exception:
            release_processing_webhook_event(event_id)
            update_payment_event_status(event_id, "FAILED")
            logger.exception("Failed to process payment.failed webhook event %s", event_id)
            return JSONResponse(
                status_code=500,
                content={"status": "error", "message": "Webhook processing failed; retry accepted", "event_id": event_id},
            )
        update_webhook_event_status(event_id, "PROCESSED")
        update_payment_event_status(event_id, "PROCESSED")
        case = outcome["case"]
        pol = outcome["policy"]
        rec = outcome["recovery"]
        response = {
            "status": "ok",
            "case": case,
            "policy_decision": pol.get("decision"),
            "action_allowed": pol.get("action_allowed"),
            "recovery_status": case.get("recovery_status"),
        }
        if rec.get("status"):
            response["recovery_result"] = rec["status"]
        return response
    elif event in {"payment.authorized", "payment.captured", "order.paid"}:
        reconciled_case = None
        recovered = False
        if event in {"payment.captured", "order.paid"}:
            reconciled_case, recovered = reconcile_successful_payment_event(event, data)
        update_webhook_event_status(event_id, "PROCESSED")
        update_payment_event_status(event_id, "PROCESSED")
        return {
            "status": "ok",
            "message": f"{event} recorded",
            "event_id": event_id,
            "recovered": recovered,
            "case": reconciled_case,
        }
    elif event == "payment_link.paid":
        try:
            rec_case, matched = process_recovery_success_event(event, data)
        except Exception:
            release_processing_webhook_event(event_id)
            update_payment_event_status(event_id, "FAILED")
            logger.exception("Failed to process payment_link.paid webhook event %s", event_id)
            return JSONResponse(
                status_code=500,
                content={"status": "error", "message": "Webhook processing failed; retry accepted", "event_id": event_id},
            )
        update_webhook_event_status(event_id, "PROCESSED")
        update_payment_event_status(event_id, "PROCESSED")
        return {"status": "ok", "recovered": matched, "case": rec_case}

    # Kept unreachable by the supported-events guard above.
    return {"status": "error", "message": "Unhandled event type"}
