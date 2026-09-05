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
    monkeypatch.setattr(router_module, "RAZORPAY_DEMO_PAYMENT_LINK_ID", "plink_controlled")
    monkeypatch.setattr(
        router_module,
        "RAZORPAY_DEMO_PAYMENT_LINK_URL",
        "https://example.invalid/must-not-win",
    )
    before = database.get_all_recovery_cases(
        merchant_account_id=router_module._default_merchant_id()
    )

    response = TestClient(app).post("/api/simulate", json={"scenario": "cancellation"})

    assert response.status_code == 200
    case = response.json()["case"]
    assert case["recovery_status"] == "LINK_CREATED"
    assert case["payment_link_id"] == "plink_controlled"
    assert case["payment_link_url"] == "https://rzp.io/rzp/authoritative"
    assert gateway.fetched == ["plink_controlled"]
    assert gateway.created == 0
    after = database.get_all_recovery_cases(
        merchant_account_id=router_module._default_merchant_id()
    )
    new_cases = [item for item in after if item["id"] not in {row["id"] for row in before}]
    assert len(new_cases) == 1
    assert new_cases[0]["id"] == case["id"]

    visible = TestClient(app).get("/api/cases").json()["cases"]
    assert any(item["id"] == case["id"] and item["payment_link_url"] for item in visible)
    dashboard = (
        Path(__file__).resolve().parents[2] / "frontend" / "dashboard.html"
    ).read_text(encoding="utf-8")
    assert "Open Link" in dashboard
    assert "c.payment_link_url" in dashboard


def test_link_paid_simulation_uses_the_new_button_two_case(monkeypatch) -> None:
    from main import app

    router_module = importlib.import_module("app.api.router")
    monkeypatch.setattr(router_module, "RAZORPAY_DEMO_PAYMENT_LINK_ID", "plink_button_two")
    monkeypatch.setattr(
        router_module,
        "RAZORPAY_DEMO_PAYMENT_LINK_URL",
        "https://rzp.io/rzp/button-two",
    )
    app.state.razorpay_client = VerifiedGateway()
    client = TestClient(app)
    cancellation = client.post("/api/simulate", json={"scenario": "cancellation"})
    assert cancellation.status_code == 200
    case_id = cancellation.json()["case"]["id"]

    paid = client.post("/api/simulate", json={"scenario": "link_paid"})

    assert paid.status_code == 200
    assert paid.json()["recovered"] is True
    assert paid.json()["case"]["id"] == case_id


def test_cancellation_simulation_fails_when_demo_link_is_missing(monkeypatch) -> None:
    from main import app

    router_module = importlib.import_module("app.api.router")
    monkeypatch.setattr(router_module, "RAZORPAY_DEMO_PAYMENT_LINK_ID", None)
    monkeypatch.setattr(router_module, "RAZORPAY_DEMO_PAYMENT_LINK_URL", None)

    response = TestClient(app).post("/api/simulate", json={"scenario": "cancellation"})

    assert response.status_code == 503
    assert "Demo Payment Link ID is not configured" in response.json()["message"]


def test_cancellation_simulation_rejects_unusable_fetched_link(monkeypatch) -> None:
    from main import app

    router_module = importlib.import_module("app.api.router")
    monkeypatch.setattr(router_module, "RAZORPAY_DEMO_PAYMENT_LINK_ID", "plink_paid")

    class PaidLinkGateway(VerifiedGateway):
        def fetch_payment_link(self, payment_link_id: str) -> dict:
            return {
                "id": payment_link_id,
                "short_url": "https://rzp.io/rzp/paid",
                "amount": 50000,
                "currency": "INR",
                "status": "paid",
            }

    app.state.razorpay_client = PaidLinkGateway()
    response = TestClient(app).post("/api/simulate", json={"scenario": "cancellation"})

    assert response.status_code == 502
    assert "Payment link creation failed" in response.json()["message"]


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
