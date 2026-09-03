# main.py - Thin FastAPI entry point
"""Application entry point that wires together configuration, the Razorpay client,
and the API router. All business logic lives in the ``app`` package.
"""

import logging
import random
import time
from fastapi import FastAPI, Request
from starlette.responses import FileResponse, JSONResponse

from app.core.config import FRONTEND_DIR, RAZORPAY_KEY_ID
from app.repositories.database import init_db
from app.integrations.razorpay_client import create_razorpay_client
from app.api.router import router

logging.basicConfig(level=logging.INFO)

# Initialise the SQLite database (creates tables if missing)
init_db()

app = FastAPI(title="AI Revenue Recovery")

# Instantiate the Razorpay client once and store in FastAPI state for DI
app.state.razorpay_client = create_razorpay_client()

# Include the router that defines all endpoints
app.include_router(router)


# Small server-side demo catalog. Prices are intentionally varied so the
# customer journey looks like a real order rather than a fixed ₹500 test.
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
    """Create a fresh random demo-store order and matching Razorpay order."""
    cart = build_demo_cart()
    subtotal = sum(item["line_total"] for item in cart)
    delivery = 0 if subtotal >= 999 else 49
    total = subtotal + delivery
    receipt = f"store_{int(time.time())}_{random.randint(1000, 9999)}"

    razorpay_client = request.app.state.razorpay_client
    try:
        order = razorpay_client.create_order(
            amount=total * 100,
            currency="INR",
            receipt=receipt,
        )
        return {
            "status": "success",
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
        return JSONResponse(
            status_code=503,
            content={"status": "error", "message": "Store checkout is currently unavailable"},
        )


# Serve static files from the ``frontend`` folder directly for convenience
@app.get("/")
async def home():
    return FileResponse(FRONTEND_DIR / "dashboard.html")


@app.get("/dashboard")
async def serve_dashboard():
    return FileResponse(FRONTEND_DIR / "dashboard.html")


@app.get("/checkout")
async def serve_checkout():
    return FileResponse(FRONTEND_DIR / "checkout.html")
