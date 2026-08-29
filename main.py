# main.py - Thin FastAPI entry point
"""Application entry point that wires together configuration, the Razorpay client,
and the API router. All business logic lives in the ``app`` package.
"""

import logging
from fastapi import FastAPI
from starlette.responses import FileResponse

from app.core.config import FRONTEND_DIR
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
