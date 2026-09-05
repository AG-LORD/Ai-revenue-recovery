# app/core/config.py
"""Centralized configuration for the AI Revenue Recovery project.
All secrets are read from environment variables at import time.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file if present
load_dotenv()

# Razorpay credentials (required for operation)
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")
RAZORPAY_DEMO_PAYMENT_LINK_ID = os.getenv("RAZORPAY_DEMO_PAYMENT_LINK_ID")
RAZORPAY_DEMO_PAYMENT_LINK_URL = os.getenv("RAZORPAY_DEMO_PAYMENT_LINK_URL")

# AI configuration
AI_ENABLED = os.getenv("AI_ENABLED", "false").lower() == "true"
AI_PROVIDER = os.getenv("AI_PROVIDER", "template")

# NVIDIA NIM configuration
NIM_API_KEY = os.getenv("NIM_API_KEY")
NIM_MODEL = os.getenv(
    "NIM_MODEL",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
)
NIM_BASE_URL = os.getenv(
    "NIM_BASE_URL",
    "https://integrate.api.nvidia.com/v1",
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = PROJECT_ROOT / "frontend"
# Preserve the existing database by default; an explicit path is useful in tests.
DATABASE_PATH = os.getenv("DATABASE_PATH", str(PROJECT_ROOT / "revenue_recovery.db"))
