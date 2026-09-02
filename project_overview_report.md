# AI Revenue Recovery — Architecture Review and Implementation Report

## Status

This report records the safe architecture cleanup completed after reviewing the
entire repository. The existing `revenue_recovery.db` was preserved: it was not
deleted, recreated, reset, or schema-migrated as part of this work.

The governing rule remains:

> AI never decides or executes a financial action. Policy determines the
> permitted action; recovery performs only that action; AI explains it.

## Review Summary

| Area | Initial score | Summary |
| --- | ---: | --- |
| Architecture | 4/10 | The intended package structure existed, but root-level duplicates bypassed it. |
| Code quality | 4/10 | Stale imports, broad exception handling, duplicate code, and `print` logging remained. |
| OOP / design | 4/10 | The SDK wrapper was useful, but services were mostly adapters over legacy modules. |
| Security | 4/10 | Webhook verification existed, but CORS and error handling were unsafe. |
| Test quality | 2/10 | Legacy phase scripts were not organized as a maintainable test suite. |
| Production readiness | 3/10 | SQLite and demo behaviours are appropriate for a hackathon, not a production deployment. |

## Problems Found and Resolved

### Critical

1. **The primary webhook path was broken.**
   `app/api/router.py` called `evaluate_recovery_policy`, which was not imported
   after the refactor. A real `payment.failed` webhook would fail before a
   recovery action could be safely processed.

   **Resolution:** the router now delegates to
   [`process_failed_payment`](app/services/workflow_service.py), which calls the
   policy service consistently.

2. **Recovery could bypass the policy gate.**
   The recovery service used `recommended_action` when `action_taken` was absent.
   That allowed a diagnosis result to act as an authorization.

   **Resolution:** recovery now accepts only `action_taken`, which is written by
   the deterministic policy gate.

### High Priority

1. **Duplicate implementations bypassed the target architecture.** Root
   `database.py`, `diagnosis.py`, `policy.py`, `recovery.py`, and `ai_agent.py`
   continued to contain the working logic while package modules merely imported
   them.

   **Resolution:** moved the active logic into `app/` and deleted obsolete root
   copies.

2. **Razorpay SDK construction leaked outside the integration boundary.**
   Root scripts constructed `razorpay.Client` directly.

   **Resolution:** there is now exactly one executable SDK construction, in
   [`app/integrations/razorpay_client.py`](app/integrations/razorpay_client.py).
   Scripts use the wrapper factory.

3. **Webhook deduplication was race-prone.** It checked whether an event existed
   and inserted it in separate operations.

   **Resolution:** `record_webhook_event` now relies on the database unique
   constraint and returns whether insertion succeeded.

4. **The recovery-success implementation still referenced deleted legacy code.**

   **Resolution:** successful payment-link matching and recovery marking now
   live in `app/services/recovery_service.py`.

### Medium Priority

- Removed permissive credentialed CORS. The demo UI is served from the same
  FastAPI application and does not need it.
- Replaced provider-error exposure with a generic `503` order-creation response
  and server-side logging.
- Replaced `print` diagnostics with standard logging in application code.
- Centralized project and frontend paths in `app/core/config.py`.
- Added a gateway-unavailable implementation so read-only endpoints and local
  demos start safely when Razorpay credentials are absent.

### Low Priority Cleanup

- Removed duplicate root HTML files; `frontend/` is the sole frontend location.
- Replaced legacy root phase scripts with focused `tests/unit` and
  `tests/integration` tests.
- Added ignore rules for `.env`, virtual environments, bytecode, and pytest
  cache.

## Final Architecture

```text
main.py
  -> app.api.router
       -> app.services.workflow_service
            -> diagnosis_service       (facts only)
            -> policy_service          (financial authorization)
            -> ai_service              (template explanation only)
            -> recovery_service        (only permitted action)
       -> repositories.database
       -> integrations.razorpay_client
```

The router owns HTTP concerns: request extraction, signature validation,
idempotency response, and JSON responses. `workflow_service` owns the ordered
application pipeline. Services depend on the repository and payment-gateway
abstraction; neither depends on FastAPI.

## Pipeline Behaviour

| Stage | Component | Responsibility |
| --- | --- | --- |
| Detect | `router.py` | Verify Razorpay signature and record a unique webhook event. |
| Diagnose | `diagnosis_service.py` | Classify issuer failure, cancellation, or unknown failure. |
| Decide | `policy_service.py` | Apply retry and safety guardrails deterministically. |
| Explain | `ai_service.py` | Generate deterministic template text from policy facts. |
| Act | `recovery_service.py` | Retry, create a permitted payment link, or escalate. |
| Measure | `recovery_service.py` + repository | Match paid recovery links and mark a case recovered. |
| Audit | `repositories/database.py` | Persist detection, policy, recovery, and explanation audit entries. |

## AI Safety

- `AI_ENABLED` is hard-coded to `False` in `app/core/config.py`.
- `AI_PROVIDER` is `template`.
- The optional NVIDIA NIM client is used only for communication text when
  explicitly enabled; no AI provider can authorize financial actions.
- The AI service returns only explanation and customer-message fields. It does
  not select retry counts, authorize payment, create orders, create payment
  links, or override policy.

## Files Deleted

```text
ai_agent.py
database.py
diagnosis.py
policy.py
recovery.py
create_order.py
view_db.py
checkout.html
dashboard.html
test_phase1.py
test_phase2.py
test_phase3.py
test_phase4.py
test_razorpay.py
```

## Retained and Added Files

```text
app/
  api/router.py
  core/config.py
  integrations/razorpay_client.py
  repositories/database.py
  services/ai_service.py
  services/diagnosis_service.py
  services/policy_service.py
  services/recovery_service.py
  services/workflow_service.py       # added
frontend/
  checkout.html
  dashboard.html
scripts/
  create_order.py                    # relocated
  view_db.py                         # relocated
tests/
  unit/test_services.py              # added
  integration/test_api_read_only.py  # added
main.py
revenue_recovery.db
```

## Verification Performed

- Python compilation passed for `app`, `scripts`, and `main.py`.
- Four focused unit tests passed when invoked directly:
  diagnosis, retry guardrail, cancellation guardrail/AI safety, and hard AI
  disablement.
- FastAPI started with Uvicorn on a local port; `GET /health` returned `200`.
- The following endpoints returned `200` through an isolated FastAPI test run:
  `GET /`, `GET /dashboard`, `GET /checkout`, `GET /api/metrics`, and
  `GET /api/cases`.
- A webhook without `X-Razorpay-Signature` returned `400`.
- Isolated simulation flows verified:
  temporary issuer failure -> `PENDING_RETRY`; customer cancellation ->
  `LINK_CREATED`; payment-link paid -> `RECOVERED`.
- Simulation used a disposable database outside the repository. The project
  database remained in place and its observed SHA-256 after verification was
  `61A7B2F7F81EC1FA3D6A0F1233DC343D2A14DC26EE60956C6C418773FE28D598`.
- Search confirmed one executable `razorpay.Client(...)` construction, in the
  integration wrapper.
- `AI_ENABLED=false` keeps NVIDIA NIM calls disabled by default; when enabled,
  NIM is limited to explanation and customer-message generation.

`python -m pytest -q` could not be executed because the current virtual
environment does not contain the `pytest` package. No dependency was installed
solely for this review.

## Remaining Technical Debt

This is now a cleaner and safer hackathon architecture, but a production system
should still add:

1. Pydantic request schemas and explicit webhook payload validation.
2. Authentication and authorization for dashboard and case/audit endpoints.
3. A durable job queue for scheduled retry execution and outbound communication.
4. Idempotency tracking for successful recovery events, not only
   `payment.failed` events.
5. A production database and transaction strategy for concurrent webhook load.
6. Structured logging, monitoring, alerting, and secrets management through the
   deployment platform.
7. A complete pytest installation and CI pipeline that runs unit, API, signature,
   idempotency, recovery, and failure-path tests.
