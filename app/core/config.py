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

# AI is deliberately template-only for this project. No external model is
# configured or called, irrespective of the process environment.
AI_ENABLED = False
AI_PROVIDER = "template"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = PROJECT_ROOT / "frontend"
# Preserve the existing database by default; an explicit path is useful in tests.
DATABASE_PATH = os.getenv("DATABASE_PATH", str(PROJECT_ROOT / "revenue_recovery.db"))
