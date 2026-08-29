"""AI communication layer; AI cannot decide or execute recovery."""

import json
import logging
from typing import Any

from openai import OpenAI

from app.core.config import (
    AI_ENABLED,
    AI_PROVIDER,
    NIM_API_KEY,
    NIM_BASE_URL,
    NIM_MODEL,
)

logger = logging.getLogger(__name__)


def _validate(content: str, policy: dict[str, Any]) -> bool:
    """Reject language that promises action forbidden by the policy."""
    forbidden = (
        "charging your card",
        "automatically charging",
        "retrying automatically",
    )

    if policy.get("decision") in {
        "ESCALATE_MANUAL_REVIEW",
        "BLOCKED_BY_GUARDRAIL",
        "ALLOW_REMINDER",
    }:
        return not any(
            phrase in content.lower()
            for phrase in forbidden
        )

    return True


def _template_response(
    case: dict[str, Any],
    diagnosis: dict[str, Any],
    policy: dict[str, Any],
    payment_link: str | None = None,
) -> dict[str, Any]:
    """Deterministic fallback used when NIM is unavailable or unsafe."""

    amount = case.get("amount", 0)
    category = diagnosis.get("category")
    decision = policy.get("decision")

    if (
        decision == "ALLOW_RETRY"
        and category == "temporary_issuer_failure"
    ):
        explanation = (
            "A temporary issuer authorization failure was identified. "
            f"Policy permits one bounded retry "
            f"({case.get('retry_count', 0) + 1}/"
            f"{case.get('max_retries', 0)})."
        )

        message = (
            f"We noticed a temporary bank issue with your payment "
            f"of Rs. {amount}. "
            "We are re-attempting it once; no action is required."
        )

    elif (
        decision == "ALLOW_REMINDER"
        and category == "customer_cancelled"
    ):
        link = (
            payment_link
            or case.get("payment_link_url")
            or "https://rzp.io/l/recovery"
        )

        explanation = (
            "Checkout was cancelled. Policy prohibits automatic "
            "charging and permits only a customer-initiated "
            "payment link."
        )

        message = (
            f"Your order of Rs. {amount} is ready to complete "
            f"whenever you are: {link}"
        )

    else:
        explanation = (
            "The payment could not be safely recovered automatically "
            "and has been routed for manual review."
        )

        message = (
            f"Your payment of Rs. {amount} could not be processed. "
            "Our support team will assist if needed."
        )

    return {
        "explanation": explanation,
        "customer_message": message,
        "recommended_action": policy.get(
            "action_allowed",
            "escalate_manual_review",
        ),
        "policy_decision": decision,
        "ai_generated": False,
        "provider": "template",
        "safety_validated": (
            _validate(explanation, policy)
            and _validate(message, policy)
        ),
        "ai_enabled": AI_ENABLED,
    }


def _generate_with_nim(
    case: dict[str, Any],
    diagnosis: dict[str, Any],
    policy: dict[str, Any],
    payment_link: str | None,
) -> dict[str, str]:
    """Ask NVIDIA NIM only for communication content."""

    if not NIM_API_KEY:
        raise RuntimeError("NIM_API_KEY is not configured")

    client = OpenAI(
        api_key=NIM_API_KEY,
        base_url=NIM_BASE_URL,
        timeout=30,
    )

    facts = {
        "amount": case.get("amount", 0),
        "currency": case.get("currency", "INR"),
        "diagnosis_category": diagnosis.get("category"),
        "diagnosis": diagnosis.get("diagnosis"),
        "policy_decision": policy.get("decision"),
        "allowed_action": policy.get("action_allowed"),
        "retry_count": case.get("retry_count", 0),
        "max_retries": case.get("max_retries", 0),
        "payment_link": payment_link,
    }

    system_prompt = """
You are the communication assistant for a payment recovery system.

The financial decision has ALREADY been made by a deterministic
policy engine.

You MUST NOT:
- change the policy decision
- recommend a different recovery action
- authorize a retry
- authorize a charge
- promise an automatic payment
- invent payment information
- invent refunds
- invent amounts
- invent payment links

Your ONLY job is to explain the existing decision clearly.

Return ONLY valid JSON with exactly these two fields:

{
  "explanation": "concise internal explanation",
  "customer_message": "concise customer-facing message"
}

The customer message must strictly respect the supplied
allowed_action and policy_decision.
""".strip()

    user_prompt = (
        "Existing payment recovery facts:\n"
        f"{json.dumps(facts, ensure_ascii=False, indent=2)}"
    )

    response = client.chat.completions.create(
        model=NIM_MODEL,
        messages=[
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],
        temperature=0,
        max_tokens=200,
        stream=False,
        extra_body={
            "chat_template_kwargs": {
                "enable_thinking": False,
            }
        },
    )

    if not response.choices:
        raise RuntimeError("NIM returned no choices")

    content = response.choices[0].message.content

    if not content:
        raise RuntimeError("NIM returned an empty response")

    content = content.strip()

    # Remove accidental markdown JSON fences if the model adds them.
    if content.startswith("```"):
        content = content.removeprefix("```json")
        content = content.removeprefix("```")
        content = content.removesuffix("```")
        content = content.strip()

    result = json.loads(content)

    explanation = str(result["explanation"]).strip()
    customer_message = str(result["customer_message"]).strip()

    if not explanation or not customer_message:
        raise RuntimeError(
            "NIM returned incomplete communication"
        )

    return {
        "explanation": explanation,
        "customer_message": customer_message,
    }


def generate_ai_recovery_insights(
    case: dict[str, Any],
    diagnosis: dict[str, Any],
    policy: dict[str, Any],
    payment_link: str | None = None,
) -> dict[str, Any]:
    """
    Generate AI communication while keeping financial decisions
    deterministic.

    NIM can explain the decision but cannot make or execute it.
    """

    fallback = _template_response(
        case,
        diagnosis,
        policy,
        payment_link,
    )

    # AI is optional. If disabled or incorrectly configured,
    # use the deterministic communication layer.
    if (
        not AI_ENABLED
        or AI_PROVIDER.lower() != "nim"
        or not NIM_API_KEY
    ):
        return fallback

    try:
        generated = _generate_with_nim(
            case,
            diagnosis,
            policy,
            payment_link,
        )

        explanation = generated["explanation"]
        customer_message = generated["customer_message"]

        safety_validated = (
            _validate(explanation, policy)
            and _validate(customer_message, policy)
        )

        if not safety_validated:
            logger.warning(
                "NIM output failed policy safety validation; "
                "using deterministic fallback"
            )
            return fallback

        return {
            "explanation": explanation,
            "customer_message": customer_message,
            "recommended_action": policy.get(
                "action_allowed",
                "escalate_manual_review",
            ),
            "policy_decision": policy.get("decision"),
            "ai_generated": True,
            "provider": "nim",
            "safety_validated": True,
            "ai_enabled": True,
        }

    except Exception:
        logger.exception(
            "NIM generation failed; using deterministic fallback"
        )
        return fallback