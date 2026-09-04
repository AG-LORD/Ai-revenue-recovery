from app.services.merchant_service import DEFAULT_MERCHANT_KEY, get_merchant_by_key, get_merchant_by_order_id
from app.services.workflow_service import _materialize_demo_order


class FakeGateway:
    def __init__(self) -> None:
        self.created_orders: list[dict] = []

    def create_order(self, amount, receipt, currency="INR", notes=None):
        order = {
            "id": "order_real_test_123",
            "amount": amount,
            "currency": currency,
            "receipt": receipt,
        }
        self.created_orders.append({"order": order, "notes": notes})
        return order


def test_synthetic_bank_failure_gets_real_checkout_order() -> None:
    gateway = FakeGateway()
    payment = {
    "id": "pay_bank_test_123",
    "amount": 25000,
    "currency": "INR",
    "order_id": "order_sim_test_123",
    "demo_scenario": "bank_failure",
}

    _materialize_demo_order(payment, gateway)

    assert payment["order_id"] == "order_real_test_123"
    assert len(gateway.created_orders) == 1
    assert gateway.created_orders[0]["order"]["amount"] == 25000
    assert gateway.created_orders[0]["notes"]["merchant_key"] == DEFAULT_MERCHANT_KEY

    merchant = get_merchant_by_order_id("order_real_test_123")
    assert merchant is not None
    assert merchant["merchant_key"] == DEFAULT_MERCHANT_KEY


def test_real_orders_are_not_rewritten() -> None:
    gateway = FakeGateway()
    payment = {
        "id": "pay_real_test_123",
        "amount": 25000,
        "currency": "INR",
        "order_id": "order_real_existing",
    }

    _materialize_demo_order(payment, gateway)

    assert payment["order_id"] == "order_real_existing"
    assert gateway.created_orders == []
