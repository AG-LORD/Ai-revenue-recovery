# main.py - Thin FastAPI entry point
"""Application entry point that wires together configuration, the Razorpay client,
and the API router. All business logic lives in the ``app`` package.
"""

import logging
import random
import time
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import FileResponse, JSONResponse, HTMLResponse

from app.core.config import FRONTEND_DIR, RAZORPAY_KEY_ID
from app.repositories.database import init_db
from app.repositories.merchant_migration import init_merchant_data
from app.services.merchant_service import DEFAULT_MERCHANT_KEY, get_merchant_by_key, register_order
from app.integrations.razorpay_client import create_razorpay_client
from app.services.retry_service import RetryAuthorizationError, authorize_retry_checkout
from app.api.router import router

logging.basicConfig(level=logging.INFO)

init_db()
init_merchant_data()

app = FastAPI(title="AI Revenue Recovery")
app.state.razorpay_client = create_razorpay_client()

app.include_router(router)

STORE_CATALOG = [
    {"id": "wireless-headphones", "name": "Wireless Headphones", "category": "Audio", "price": 1299, "emoji": "🎧", "rating": "4.7", "delivery": "Tomorrow"},
    {"id": "smart-watch", "name": "Smart Watch", "category": "Wearables", "price": 1799, "emoji": "⌚", "rating": "4.6", "delivery": "Tomorrow"},
    {"id": "running-shoes", "name": "Premium Running Shoes", "category": "Fashion", "price": 999, "emoji": "👟", "rating": "4.8", "delivery": "2 days"},
    {"id": "backpack", "name": "Everyday Laptop Backpack", "category": "Bags", "price": 749, "emoji": "🎒", "rating": "4.5", "delivery": "Tomorrow"},
    {"id": "coffee-maker", "name": "Compact Coffee Maker", "category": "Home", "price": 1499, "emoji": "☕", "rating": "4.7", "delivery": "2 days"},
    {"id": "desk-lamp", "name": "LED Study Lamp", "category": "Home", "price": 599, "emoji": "💡", "rating": "4.6", "delivery": "Tomorrow"},
]


def build_demo_cart() -> list[dict]:
    """Build a random but deterministic-looking customer cart for the demo."""
    count = random.choices([1, 2, 3], weights=[55, 35, 10], k=1)[0]
    products = random.sample(STORE_CATALOG, count)
    cart = []
    for product in products:
        quantity = random.choice([1, 1, 1, 2])
        cart.append({**product, "quantity": quantity, "line_total": product["price"] * quantity})
    return cart


@app.post("/api/store-order")
async def create_store_order(request: Request):
    """Create a fresh demo-store order owned by the selected merchant."""
    body = await request.json()
    merchant_key = body.get("merchant_key") or DEFAULT_MERCHANT_KEY
    merchant = get_merchant_by_key(merchant_key)
    if not merchant:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Unknown merchant"})

    cart = build_demo_cart()
    subtotal = sum(item["line_total"] for item in cart)
    delivery = 0 if subtotal >= 999 else 49
    total = subtotal + delivery
    receipt = f"{merchant_key}_{int(time.time())}_{random.randint(1000, 9999)}"

    razorpay_client = request.app.state.razorpay_client
    try:
        order = razorpay_client.create_order(
            amount=total * 100,
            currency="INR",
            receipt=receipt,
            notes={"merchant_key": merchant_key, "merchant_account_id": str(merchant["id"])},
        )
        register_order(order["id"], merchant["id"])
        return {
            "status": "success",
            "merchant": {
                "id": merchant["id"],
                "merchant_key": merchant["merchant_key"],
                "business_name": merchant["business_name"],
            },
            "order_id": order["id"],
            "amount": order["amount"],
            "currency": order["currency"],
            "key_id": RAZORPAY_KEY_ID,
            "cart": cart,
            "subtotal": subtotal,
            "delivery": delivery,
            "total": total,
            "delivery_label": "FREE" if delivery == 0 else f"₹{delivery}",
        }
    except Exception:
        logging.exception("Store order creation failed")
        return JSONResponse(status_code=503, content={"status": "error", "message": "Store checkout is currently unavailable"})


@app.post("/api/cases/{payment_id}/retry-checkout")
async def retry_checkout(payment_id: str):
    """Return the existing Razorpay order for an approved bounded retry."""
    try:
        return authorize_retry_checkout(payment_id)
    except RetryAuthorizationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def dashboard_html_response():
    """Serve the existing dashboard plus the small retry-action enhancement."""
    dashboard_path = FRONTEND_DIR / "dashboard.html"
    html = dashboard_path.read_text(encoding="utf-8")
    script_tag = '<script src="/retry-dashboard.js"></script>'
    if script_tag not in html:
        html = html.replace("</body>", f"{script_tag}\n</body>")
    return HTMLResponse(content=html)


@app.get("/retry-dashboard.js")
async def retry_dashboard_script():
    return FileResponse(FRONTEND_DIR / "retry-dashboard.js", media_type="application/javascript")


@app.get("/")
async def home():
    return dashboard_html_response()


@app.get("/dashboard")
async def serve_dashboard():
    return dashboard_html_response()


@app.get("/checkout")
async def serve_checkout():
    return FileResponse(FRONTEND_DIR / "checkout.html")


@app.get("/retry/{payment_id}")
async def serve_retry(payment_id: str):
    return FileResponse(FRONTEND_DIR / "retry.html")
