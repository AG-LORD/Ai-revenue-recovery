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
from app.services.recovery_service import process_recovery_success_event
from app.services.workflow_service import process_failed_payment
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
)

router = APIRouter()
logger = logging.getLogger(__name__)

@router.get("/health")
async def health():
    return {"status": "healthy"}

@router.get("/api/metrics")
async def api_metrics():
    return get_recovery_metrics()

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
    cases = get_all_recovery_cases()
    return {"total_cases": len(cases), "cases": cases}

@router.get("/cases")
async def list_cases():
    cases = get_all_recovery_cases()
    return {"total_cases": len(cases), "cases": cases}

@router.get("/cases/{payment_id}/audit")
async def case_audit_trail(payment_id: str):
    trail = get_audit_trail_for_case(payment_id)
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
        all_cases = get_all_recovery_cases()
        link_case = next((c for c in all_cases if c["recovery_status"] == "LINK_CREATED"), None)
        plink_id = link_case["payment_link_id"] if link_case else f"plink_sim_{ts}"
        orig_pay_id = link_case["payment_id"] if link_case else f"pay_cancel_{ts}"
        ord_id = link_case["order_id"] if link_case else f"order_sim_{ts}"
        payload = {
            "entity": "event",
            "event": "payment_link.paid",
            "id": f"event_sim_paid_{ts}",
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": plink_id,
                        "amount": 50000,
                        "amount_paid": 50000,
                        "status": "paid",
                        "notes": {"original_payment_id": orig_pay_id},
                    }
                },
                "payment": {
                    "entity": {
                        "id": f"pay_recovered_{ts}",
                        "order_id": ord_id,
                        "amount": 50000,
                        "currency": "INR",
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
    if event == "payment.failed":
        payment = data["payload"]["payment"]["entity"]
        payment_id = payment.get("id")
        if not event_id:
            event_id = f"event_{event}_{payment_id}"
        if not record_webhook_event(event_id=event_id, event_type=event, payload_dict=data, status="PROCESSING"):
            add_audit_log(
                payment_id=payment_id,
                actor="SYSTEM",
                action="DUPLICATE_WEBHOOK_IGNORED",
                details=f"Received duplicate webhook event '{event_id}'. Skipped processing.",
            )
            return {"status": "ignored", "message": "Duplicate event already processed", "event_id": event_id}
        try:
            outcome = process_failed_payment(payment, razorpay_client)
        except Exception:
            release_processing_webhook_event(event_id)
            logger.exception("Failed to process payment.failed webhook event %s", event_id)
            return JSONResponse(
                status_code=500,
                content={"status": "error", "message": "Webhook processing failed; retry accepted", "event_id": event_id},
            )
        update_webhook_event_status(event_id, "PROCESSED")
        case = outcome["case"]
        pol = outcome["policy"]
        rec = outcome["recovery"]
        # Build response including policy and recovery info
        response = {
            "status": "ok",
            "case": case,
            "policy_decision": pol.get("decision"),
            "action_allowed": pol.get("action_allowed"),
            "recovery_status": case.get("recovery_status"),
        }
        # Include recovery result if available (e.g., retry_initiated, link_created, escalated)
        if rec.get("status"):
            response["recovery_result"] = rec["status"]
        return response
    elif event == "payment_link.paid":
        payment = data.get("payload", {}).get("payment", {}).get("entity", {})
        payment_link = data.get("payload", {}).get("payment_link", {}).get("entity", {})
        payment_id = payment.get("id")
        if not event_id:
            event_id = f"event_{event}_{payment_id or payment_link.get('id')}"
        if not record_webhook_event(event_id=event_id, event_type=event, payload_dict=data, status="PROCESSING"):
            add_audit_log(
                payment_id=payment_id,
                actor="SYSTEM",
                action="DUPLICATE_WEBHOOK_IGNORED",
                details=f"Received duplicate webhook event '{event_id}'. Skipped processing.",
            )
            return {"status": "ignored", "message": "Duplicate event already processed", "event_id": event_id}
        try:
            rec_case, matched = process_recovery_success_event(event, data)
        except Exception:
            release_processing_webhook_event(event_id)
            logger.exception("Failed to process payment_link.paid webhook event %s", event_id)
            return JSONResponse(
                status_code=500,
                content={"status": "error", "message": "Webhook processing failed; retry accepted", "event_id": event_id},
            )
        update_webhook_event_status(event_id, "PROCESSED")
        return {"status": "ok", "recovered": matched, "case": rec_case}
    else:
        return {"status": "error", "message": "Unhandled event type"}
