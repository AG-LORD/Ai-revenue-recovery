from fastapi.testclient import TestClient

from main import app


client = TestClient(app)


def test_public_read_only_endpoints() -> None:
    for path in ("/", "/dashboard", "/checkout", "/api/metrics", "/api/cases"):
        response = client.get(path)
        assert response.status_code == 200


def test_webhook_rejects_missing_signature() -> None:
    response = client.post("/webhook/razorpay", content=b"{}")
    assert response.status_code == 400
