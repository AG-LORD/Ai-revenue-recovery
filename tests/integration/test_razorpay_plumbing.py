import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


class VerifiedGateway:
    def __init__(self) -> None:
        self.fetched: list[str] = []
        self.created = 0

    def verify_webhook_signature(self, body: bytes, signature: str, webhook_secret: str) -> bool:
        return True

    def fetch_payment_link(self, payment_link_id: str) -> dict:
        self.fetched.append(payment_link_id)
        return {
            "id": payment_link_id,
            "short_url": "https://rzp.io/rzp/authoritative",
            "amount": 50000,
            "currency": "INR",
            "status": "created",
        }

    def create_payment_link(self, payload: dict) -> dict:
        self.created += 1
        raise AssertionError("controlled demo must not create Payment Links")


def _captured_event(event_id: str) -> dict:
    return {
        "id": event_id,
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_{event_id}",
                    "order_id": f"order_{event_id}",
                    "amount": 1000,
                    "currency": "INR",
                    "status": "captured",
                }
            }
        },
    }


@pytest.mark.parametrize("path", ["/webhook/razorpay", "/razorpay"])
def test_razorpay_webhook_aliases_process_events(path: str) -> None:
    from main import app

    app.state.razorpay_client = VerifiedGateway()
    response = TestClient(app).post(
        path,
        json=_captured_event(f"evt_alias_{path.rsplit('/', 1)[-1]}"),
        headers={"X-Razorpay-Signature": "valid"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_simulation_returns_safe_processing_failure_reason(monkeypatch) -> None:
    from main import app

    router_module = importlib.import_module("app.api.router")
    app.state.razorpay_client = VerifiedGateway()

    def fail_processing(payment, gateway):
        raise RuntimeError("configured demo gateway limit reached")

    monkeypatch.setattr(router_module, "process_failed_payment", fail_processing)
    response = TestClient(app).post("/api/simulate", json={"scenario": "bank_failure"})

    assert response.status_code == 500
    assert response.json() == {
        "status": "error",
        "message": "Simulation failed: configured demo gateway limit reached",
    }


def test_cancellation_simulation_creates_one_visible_link_case(monkeypatch) -> None:
    from main import app
    from app.repositories import database

    router_module = importlib.import_module("app.api.router")
    gateway = VerifiedGateway()
    app.state.razorpay_client = gateway
    before = database.get_all_recovery_cases(
        merchant_account_id=router_module._default_merchant_id()
    )

    response = TestClient(app).post("/api/simulate", json={"scenario": "cancellation"})

    assert response.status_code == 200
    case = response.json()["case"]
    assert case["recovery_status"] == "LINK_CREATED"
    assert case["payment_link_id"].startswith("plink_simulated_")
    assert case["payment_link_url"] == ""
    assert gateway.fetched == []
    assert gateway.created == 0
    after = database.get_all_recovery_cases(
        merchant_account_id=router_module._default_merchant_id()
    )
    new_cases = [item for item in after if item["id"] not in {row["id"] for row in before}]
    assert len(new_cases) == 1
    assert new_cases[0]["id"] == case["id"]

    visible = TestClient(app).get("/api/cases").json()["cases"]
    assert any(
        item["id"] == case["id"]
        and item["payment_link_id"].startswith("plink_simulated_")
        and not item["payment_link_url"]
        for item in visible
    )
    dashboard = (
        Path(__file__).resolve().parents[2] / "frontend" / "dashboard.html"
    ).read_text(encoding="utf-8")
    assert "Synthetic Demo Link" in dashboard
    assert "View Payment Activity" in dashboard
    assert "View All Cases" in dashboard
    assert "payment-activity-modal" in dashboard
    assert "all-cases-modal" in dashboard
    assert "c.payment_link_url" in dashboard


def test_link_paid_simulation_uses_the_new_button_two_case(monkeypatch) -> None:
    from main import app

    router_module = importlib.import_module("app.api.router")
    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    cancellation = client.post("/api/simulate", json={"scenario": "cancellation"})
    assert cancellation.status_code == 200
    case_id = cancellation.json()["case"]["id"]

    paid = client.post("/api/simulate", json={"scenario": "link_paid"})

    assert paid.status_code == 200
    assert paid.json()["recovered"] is True
    assert paid.json()["case"]["id"] == case_id
    assert paid.json()["case"]["recovery_status"] == "RECOVERED"
    assert paid.json()["case"]["recovered_amount"] == paid.json()["case"]["amount"]


def test_two_cancellation_and_link_paid_pairs_recover_separate_cases() -> None:
    from main import app

    client = TestClient(app)
    first_cancel = client.post("/api/simulate", json={"scenario": "cancellation"})
    first_paid = client.post("/api/simulate", json={"scenario": "link_paid"})
    second_cancel = client.post("/api/simulate", json={"scenario": "cancellation"})
    second_paid = client.post("/api/simulate", json={"scenario": "link_paid"})

    assert first_cancel.status_code == first_paid.status_code == 200
    assert second_cancel.status_code == second_paid.status_code == 200
    assert first_paid.json()["recovered"] is True
    assert second_paid.json()["recovered"] is True
    assert first_paid.json()["case"]["id"] != second_paid.json()["case"]["id"]
    assert first_paid.json()["case"]["recovery_status"] == "RECOVERED"
    assert second_paid.json()["case"]["recovery_status"] == "RECOVERED"


def test_cancellation_simulation_can_be_repeated_without_a_real_link() -> None:
    from main import app

    client = TestClient(app)
    first = client.post("/api/simulate", json={"scenario": "cancellation"})
    second = client.post("/api/simulate", json={"scenario": "cancellation"})

    assert first.status_code == second.status_code == 200
    assert first.json()["case"]["payment_link_id"] != second.json()["case"]["payment_link_id"]
    assert first.json()["case"]["recovery_status"] == second.json()["case"]["recovery_status"] == "LINK_CREATED"

def test_link_paid_simulation_ignores_batch_and_foreign_cases() -> None:
    from main import app
    from app.repositories import database
    from app.services.merchant_service import get_merchant_by_key

    merchant = get_merchant_by_key("urban_cart")
    foreign = get_merchant_by_key("fit_gear")
    database.create_or_get_recovery_case(
        {
            "id": "demo_batch_v1_foreign",
            "order_id": "demo_batch_order_foreign",
            "amount": 50000,
            "currency": "INR",
            "error_reason": "payment_cancelled",
        },
        {
            "category": "customer_cancelled",
            "diagnosis": "Customer cancelled checkout.",
            "recoverable": True,
            "recommended_action": "send_payment_reminder",
            "max_retries": 0,
        },
        merchant_account_id=foreign["id"],
    )
    batch_case, _ = database.create_or_get_recovery_case(
        {
            "id": "demo_batch_v1_local",
            "order_id": "demo_batch_order_local",
            "amount": 50000,
            "currency": "INR",
            "error_reason": "payment_cancelled",
        },
        {
            "category": "customer_cancelled",
            "diagnosis": "Customer cancelled checkout.",
            "recoverable": True,
            "recommended_action": "send_payment_reminder",
            "max_retries": 0,
        },
        merchant_account_id=merchant["id"],
    )
    database.update_case_policy(batch_case["id"], batch_case["payment_id"], {
        "next_status": "LINK_CREATED",
        "action_allowed": "send_payment_reminder",
    })
    database.update_case_recovery_action(
        batch_case["id"],
        batch_case["payment_id"],
        "LINK_CREATED",
        "SYNTHETIC_PAYMENT_LINK_CREATED",
        payment_link_id="plink_simulated_batch",
    )

    response = TestClient(app).post("/api/simulate", json={"scenario": "link_paid"})

    assert response.status_code == 409
    assert "synthetic cancellation link" in response.json()["detail"]


def test_unknown_failure_simulation_creates_distinct_cases(monkeypatch) -> None:
    from main import app

    client = TestClient(app)
    first = client.post("/api/simulate", json={"scenario": "unknown_failure"})
    second = client.post("/api/simulate", json={"scenario": "unknown_failure"})

    assert first.status_code == second.status_code == 200
    first_case = first.json()["case"]
    second_case = second.json()["case"]
    assert first_case["id"] != second_case["id"]
    assert first_case["payment_id"] != second_case["payment_id"]
    assert first_case["order_id"] != second_case["order_id"]
    assert first_case["recovery_status"] == second_case["recovery_status"] == "ESCALATED"

    event = {
        "id": "evt_replay_unknown",
        "event": "payment.failed",
        "payload": {"payment": {"entity": {
            "id": "pay_replay_unknown",
            "order_id": "order_replay_unknown",
            "amount": 50000,
            "currency": "INR",
            "status": "failed",
            "error_reason": "unhandled_code_99",
            "error_source": "gateway",
            "error_step": "payment_capture",
        }}},
    }
    replay_client = TestClient(app)
    first_replay = replay_client.post(
        "/webhook/razorpay", json=event, headers={"X-Razorpay-Signature": "valid"}
    )
    second_replay = replay_client.post(
        "/webhook/razorpay", json=event, headers={"X-Razorpay-Signature": "valid"}
    )
    assert first_replay.status_code == second_replay.status_code == 200
    assert second_replay.json()["status"] == "ignored"


def test_unknown_failure_simulation_escalates_without_gateway_dependency() -> None:
    from main import app

    response = TestClient(app).post("/api/simulate", json={"scenario": "unknown_failure"})

    assert response.status_code == 200
    case = response.json()["case"]
    assert case["diagnosis_category"] == "unknown_failure"
    assert case["action_taken"] == "escalate_manual_review"
    assert case["recovery_status"] == "ESCALATED"
    assert not case["payment_link_id"]
