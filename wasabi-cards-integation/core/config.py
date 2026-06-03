

# =========================================================
# config.py
# Centralized environment configuration
# =========================================================

import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------
# APP
# ---------------------------------------------------------

APP_NAME = os.getenv("APP_NAME")

# ---------------------------------------------------------
# DATABASE
# ---------------------------------------------------------

MONGO_URL = os.getenv("MONGO_URL")
DB_NAME = os.getenv("DB_NAME")

# ---------------------------------------------------------
# JWT
# ---------------------------------------------------------

JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM")

# ---------------------------------------------------------
# WASABI
# ---------------------------------------------------------

WASABI_BASE_URL = os.getenv("WASABI_BASE_URL")
WASABI_API_KEY = os.getenv("WASABI_API_KEY")
WASABI_MERCHANT_ID = os.getenv("WASABI_MERCHANT_ID")

# ---------------------------------------------------------
# WEBHOOKS
# ---------------------------------------------------------

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")