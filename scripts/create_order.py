"""Create a test Razorpay order through the project's integration boundary."""

from app.integrations.razorpay_client import create_razorpay_client


def main() -> None:
    client = create_razorpay_client()
    order = client.create_order(amount=50000, currency="INR", receipt="test_receipt_001")
    print(f"Order ID: {order['id']}")
    print(f"Amount: {order['amount']} {order['currency']}")


if __name__ == "__main__":
    main()
