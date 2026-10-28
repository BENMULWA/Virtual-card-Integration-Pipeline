import os
from decimal import Decimal
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")


def require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise ValueError(f"Missing required env var: {key}")
    return value.strip()  # automatically cleans \r, \n, and trailing spaces


def get_optional_env(key: str, default: str = "") -> str:
    value = os.getenv(key)
    if value is None or not value.strip():  # "KEY=" (blank) behaves like unset, so numeric defaults can't crash
        return default
    return value.strip()


def get_bool_env(key: str, default: bool = False) -> bool:
    return get_optional_env(key, str(default)).lower() in ("1", "true", "yes", "on")


def _resolve_path(env_key: str, default: Path) -> Path:
    raw = get_optional_env(env_key)
    if not raw:
        return default.resolve()
    path = Path(raw)
    return (path if path.is_absolute() else BASE_DIR / path).resolve()


# =========================
# KEYS (FILE BASED)
# =========================
RSA_PRIVATE_KEY_PATH = _resolve_path("RSA_PRIVATE_KEY_PATH", BASE_DIR / "app" / "keys" / "private.pem")
RSA_PUBLIC_KEY_PATH = _resolve_path("RSA_PUBLIC_KEY_PATH", BASE_DIR / "app" / "keys" / "public.pem")
WASABI_PUBLIC_KEY_PATH = _resolve_path("WASABI_PUBLIC_KEY_PATH", BASE_DIR / "app" / "keys" / "wsbpublic.pem")

# =========================
# APP
# =========================
APP_NAME = get_optional_env("APP_NAME")
APP_ENV = get_optional_env("APP_ENV", "development").lower()
DEBUG = get_bool_env("DEBUG")
IS_PRODUCTION = APP_ENV in ("prod", "production", "live")

# =========================
# CORS
# =========================
# Origins allowed to call this API from a browser. The user UI runs on its own
# origin (Vite dev server or the static build), so it must be listed here.
# "*" is allowed only for local development.
CORS_ORIGINS = [o.strip() for o in get_optional_env("CORS_ORIGINS").split(",") if o.strip()]

# =========================
# DATABASE
# =========================
MONGO_URL = get_optional_env("MONGO_URL")
DB_NAME = get_optional_env("DB_NAME")

# =========================
# JWT
# =========================
JWT_SECRET = get_optional_env("JWT_SECRET")
JWT_ALGORITHM = get_optional_env("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(get_optional_env("JWT_EXPIRE_MINUTES", "60"))
# The /token test endpoint mints tokens for anyone. Never enable it in production.
ENABLE_TEST_TOKEN = get_bool_env("ENABLE_TEST_TOKEN") and not IS_PRODUCTION

# =========================
# USER ACCOUNTS
# =========================
# Where the email verification link points. Must be a public HTTPS base URL.
APP_PUBLIC_URL = get_optional_env("APP_PUBLIC_URL").rstrip("/")
# Base URL of the mail provider API (e.g. SendGrid / Mailgun / SES proxy).
MAIL_API_URL = get_optional_env("MAIL_API_URL")
MAIL_API_KEY = get_optional_env("MAIL_API_KEY")
MAIL_FROM = get_optional_env("MAIL_FROM", "no-reply@mamlakapsp.com")
MAIL_FROM_NAME = get_optional_env("MAIL_FROM_NAME", "Mamlaka")
# Development only: expose the verification link through the API so the flow can be
# tested without a mail provider. Hard-blocked in production.
ENABLE_DEV_EMAIL_PREVIEW = get_bool_env("ENABLE_DEV_EMAIL_PREVIEW") and not IS_PRODUCTION
# Testing only: treat every account as email-verified (on register and on login) so the flow can be
# exercised without a mail provider. Hard-blocked in production.
DEV_AUTO_VERIFY_EMAIL = get_bool_env("DEV_AUTO_VERIFY_EMAIL") and not IS_PRODUCTION

# =========================
# WASABI
# =========================
WASABI_BASE_URL = require_env("WASABI_BASE_URL").rstrip("/")
WASABI_API_KEY = require_env("WASABI_API_KEY")
WASABI_MERCHANT_ID = get_optional_env("WASABI_MERCHANT_ID")
WASABI_PUBLIC_KEY = get_optional_env("WASABI_PUBLIC_KEY")
WASABI_TIMEOUT_SECONDS = float(get_optional_env("WASABI_TIMEOUT_SECONDS", "40"))

# =========================
# WEBHOOK
# =========================
WEBHOOK_SECRET = get_optional_env("WEBHOOK_SECRET")
# Wasabi's docs do not state the callback signing algorithm. Verification below assumes RSA/SHA-256
# (PKCS#1 v1.5) over the raw body using the Wasabi platform key; confirm with Wasabi, then enable.
ENABLE_WEBHOOK_SIGNATURE_VALIDATION = get_bool_env("ENABLE_WEBHOOK_SIGNATURE_VALIDATION")

# =========================
# MONEY SAFETY
# =========================
# Card creation (initial funding), deposit and withdraw move money from the merchant's Wasabi balance.
# They stay disabled until a wallet/ledger funding source is wired in and this flag is turned on.
LIVE_MONEY_ENABLED = get_bool_env("LIVE_MONEY_ENABLED")
MAX_SINGLE_AMOUNT = Decimal(get_optional_env("MAX_SINGLE_AMOUNT", "500"))
# A provider operation that is still PENDING after this long is failed and its wallet hold released.
PENDING_OPERATION_MAX_AGE_HOURS = int(get_optional_env("PENDING_OPERATION_MAX_AGE_HOURS", "24"))
# A top-up intent that is never paid expires (the callback is then ignored).
TOPUP_INTENT_TTL_HOURS = int(get_optional_env("TOPUP_INTENT_TTL_HOURS", "24"))

# =========================
# KYC UPLOADS
# =========================
MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # Wasabi limit: 2 MB
TRUST_PROXY_HEADERS = get_bool_env("TRUST_PROXY_HEADERS")

# =========================
# M-PESA (Daraja STK Push) - funds the customer wallet in KES
# =========================
MPESA_BASE_URL = get_optional_env("MPESA_BASE_URL", "https://sandbox.safaricom.co.ke").rstrip("/")
MPESA_CONSUMER_KEY = get_optional_env("MPESA_CONSUMER_KEY")
MPESA_CONSUMER_SECRET = get_optional_env("MPESA_CONSUMER_SECRET")
MPESA_SHORTCODE = get_optional_env("MPESA_SHORTCODE")          # paybill / till (BusinessShortCode)
MPESA_PASSKEY = get_optional_env("MPESA_PASSKEY")
MPESA_TRANSACTION_TYPE = get_optional_env("MPESA_TRANSACTION_TYPE", "CustomerPayBillOnline")  # or CustomerBuyGoodsOnline
MPESA_PARTY_B = get_optional_env("MPESA_PARTY_B") or MPESA_SHORTCODE  # till number for buy-goods
# Public HTTPS base, e.g. https://api.mamlakapsp.com ; the secret makes the callback URL unguessable.
MPESA_CALLBACK_BASE_URL = get_optional_env("MPESA_CALLBACK_BASE_URL").rstrip("/")
MPESA_CALLBACK_SECRET = get_optional_env("MPESA_CALLBACK_SECRET")
MPESA_MIN_TOPUP_KES = int(get_optional_env("MPESA_MIN_TOPUP_KES", "10"))
MPESA_MAX_TOPUP_KES = int(get_optional_env("MPESA_MAX_TOPUP_KES", "150000"))

# =========================
# AIRTEL MONEY (Paybill collection) - funds the same KES wallet as M-Pesa
# =========================
# The customer pays the Paybill with the account number we show them. Airtel then
# calls our callback; we credit the wallet only after the callback secret and the
# amount both check out. Airtel API credentials are not needed for collection.
AIRTEL_PAYBILL = get_optional_env("AIRTEL_PAYBILL")                 # e.g. 220100
AIRTEL_PAYBILL_NAME = get_optional_env("AIRTEL_PAYBILL_NAME", "Mamlaka")
AIRTEL_ACCOUNT_PREFIX = get_optional_env("AIRTEL_ACCOUNT_PREFIX", "MAML")
# Public HTTPS base for the callback, e.g. https://api.mamlakapsp.com
AIRTEL_CALLBACK_BASE_URL = get_optional_env("AIRTEL_CALLBACK_BASE_URL").rstrip("/")
AIRTEL_CALLBACK_SECRET = get_optional_env("AIRTEL_CALLBACK_SECRET")
AIRTEL_MIN_TOPUP_KES = int(get_optional_env("AIRTEL_MIN_TOPUP_KES", "10"))
AIRTEL_MAX_TOPUP_KES = int(get_optional_env("AIRTEL_MAX_TOPUP_KES", "150000"))

# =========================
# MAMLAKA PAYMENTS GATEWAY (STK collections + B2C payouts for M-Pesa and Airtel Money)
# =========================
# Auth: HTTP Basic against GET {base}/ returns a short-lived bearer token. The username is also the
# merchant id (impalaMerchantId). The gateway signs its callbacks: HMAC-SHA256 over the raw body,
# sent as `X-Mamlaka-Signature: sha256=<hex>`. Production may allowlist the calling server's IP.
AIRTEL_API_BASE_URL = get_optional_env("AIRTEL_API_BASE_URL").rstrip("/")
AIRTEL_API_USERNAME = get_optional_env("AIRTEL_API_USERNAME")
AIRTEL_API_PASSWORD = get_optional_env("AIRTEL_API_PASSWORD")
AIRTEL_CALLBACK_SIGNING_SECRET = get_optional_env("AIRTEL_CALLBACK_SIGNING_SECRET")
# Moving KES out of the wallet to a phone is real money leaving the merchant float: off until enabled.
WALLET_WITHDRAW_ENABLED = get_bool_env("WALLET_WITHDRAW_ENABLED")
WITHDRAW_MIN_KES = int(get_optional_env("WITHDRAW_MIN_KES", "10"))
WITHDRAW_MAX_KES = int(get_optional_env("WITHDRAW_MAX_KES", "20000"))

# =========================
# PRICING (wallet KES -> card USD). No FX feed is wired in: you must set the rate.
# =========================
FX_KES_PER_USD = get_optional_env("FX_KES_PER_USD")               # e.g. 129.00 ; funding is off while empty
FX_SPREAD_PCT = Decimal(get_optional_env("FX_SPREAD_PCT", "2.0"))   # margin added on KES->USD
CARD_FUNDING_FEE_KES = Decimal(get_optional_env("CARD_FUNDING_FEE_KES", "0"))  # flat fee per funding op

# =========================
# TREASURY: our real USD balance at Wasabi vs what partners have prepaid
# =========================
# Card issuance and card deposits spend this balance. Issuance is refused (cleanly) when it cannot cover the
# request, and an alert fires when it runs low or falls below the value of the partner floats.
TREASURY_CURRENCIES = {c.strip().upper() for c in get_optional_env("TREASURY_CURRENCIES", "USD").split(",") if c.strip()}
TREASURY_ALERT_BELOW_USD = Decimal(get_optional_env("TREASURY_ALERT_BELOW_USD", "100"))
TREASURY_RESERVE_USD = Decimal(get_optional_env("TREASURY_RESERVE_USD", "0"))   # never spend below this
ALERT_WEBHOOK_URL = get_optional_env("ALERT_WEBHOOK_URL")                       # Slack-style: POST {"text": "..."}

# =========================
# CARD PRODUCTS - only these cardTypeIds may be registered for / issued (Wasabi-confirmed: 111078 = BIN 524013)
# =========================
ALLOWED_CARD_TYPE_IDS = {int(x) for x in get_optional_env("ALLOWED_CARD_TYPE_IDS", "111078").replace(" ", "").split(",") if x}
