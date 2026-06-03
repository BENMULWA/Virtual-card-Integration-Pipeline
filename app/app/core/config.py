import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")


def require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise ValueError(f"Missing required env var: {key}")
    return value.strip()  # FIXED: Automatically cleans \r, \n, and trailing spaces


def get_optional_env(key: str, default: str = "") -> str:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip()  #cleans the sting on th API KEY 


# =========================
# KEYS (FILE BASED)
# =========================
RSA_PRIVATE_KEY_PATH = (BASE_DIR / "app" / "keys" / "private.pem").resolve()
RSA_PUBLIC_KEY_PATH = (BASE_DIR / "app" / "keys" / "public.pem").resolve()

# =========================
# APP
# =========================
APP_NAME = get_optional_env("APP_NAME")
APP_ENV = get_optional_env("APP_ENV")
DEBUG = get_optional_env("DEBUG", "false").lower() == "true"

# =========================
# DATABASE
# =========================
MONGO_URL = get_optional_env("MONGO_URL")
DB_NAME = get_optional_env("DB_NAME")

# =========================
# JWT
# =========================
JWT_SECRET = get_optional_env("JWT_SECRET")
JWT_ALGORITHM = get_optional_env("JWT_ALGORITHM")

# =========================
# WASABI
# =========================
WASABI_BASE_URL = require_env("WASABI_BASE_URL")
WASABI_API_KEY = require_env("WASABI_API_KEY")
WASABI_MERCHANT_ID = get_optional_env("WASABI_MERCHANT_ID")

# =========================
# PUBLIC KEY (OPTIONAL)
# =========================
WASABI_PUBLIC_KEY = get_optional_env("WASABI_PUBLIC_KEY")

# =========================
# WEBHOOK
# =========================
WEBHOOK_SECRET = get_optional_env("WEBHOOK_SECRET")
