import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# --- test-only environment (never the real .env / live keys) ---------------------------------
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_tmp = Path(os.environ.get("PYTEST_TMPKEYS", "/tmp/claude-1000/-home-software-engineer/319445dc-fd65-42f8-a6f3-afc5b9e2cc97/scratchpad/testkeys"))
_tmp.mkdir(parents=True, exist_ok=True)


def _write_pair(name):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (_tmp / f"{name}.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (_tmp / f"{name}_pub.pem").write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))


_write_pair("merchant")
_write_pair("wasabi")

os.environ.update({
    "WASABI_BASE_URL": "https://wasabi.test",
    "WASABI_API_KEY": "test-key",
    "RSA_PRIVATE_KEY_PATH": str(_tmp / "merchant.pem"),
    "RSA_PUBLIC_KEY_PATH": str(_tmp / "merchant_pub.pem"),
    "WASABI_PUBLIC_KEY_PATH": str(_tmp / "wasabi_pub.pem"),
    "JWT_SECRET": "x" * 40,
    "JWT_ALGORITHM": "HS256",
    "LIVE_MONEY_ENABLED": "true",
    "MAX_SINGLE_AMOUNT": "100",
    "APP_ENV": "test",
    "ALLOWED_CARD_TYPE_IDS": "7,8,111078",
    "MONGO_URL": "mongodb://localhost:27017",   # never the real cluster; collections are replaced by an in-memory fake
    "DB_NAME": "test",
    "ENABLE_WEBHOOK_SIGNATURE_VALIDATION": "false",
    "ENABLE_TEST_TOKEN": "false",
    "DEV_AUTO_VERIFY_EMAIL": "false",
    "TRUST_PROXY_HEADERS": "false",
    "WASABI_TIMEOUT_SECONDS": "5",
    "FX_KES_PER_USD": "130",
    "FX_SPREAD_PCT": "2",
    "CARD_FUNDING_FEE_KES": "0",
    "MPESA_BASE_URL": "https://daraja.test",
    "MPESA_CONSUMER_KEY": "ck", "MPESA_CONSUMER_SECRET": "cs",
    "MPESA_SHORTCODE": "174379", "MPESA_PASSKEY": "pk",
    "MPESA_CALLBACK_BASE_URL": "https://api.example.test",
    "MPESA_CALLBACK_SECRET": "s3cret-callback",
    # gateway: fixed test values so a developer .env can never route tests at the real gateway
    "AIRTEL_API_BASE_URL": "https://gateway.test/api/v1",
    "AIRTEL_API_USERNAME": "test_merchant", "AIRTEL_API_PASSWORD": "pw",
    "AIRTEL_CALLBACK_BASE_URL": "https://api.example.test",
    "AIRTEL_CALLBACK_SIGNING_SECRET": "gateway-signing-secret",
    "WALLET_WITHDRAW_ENABLED": "true",
})
# Load the test env over any developer .env: python-dotenv does not override existing vars.

from mongomock_motor import AsyncMongoMockClient  # noqa: E402


@pytest.fixture(autouse=True)
def fake_db(monkeypatch):
    from app.core import database
    db = AsyncMongoMockClient()["test"]
    names = ["cards_collection", "holders_collection", "transactions_collection",
             "webhook_events_collection", "ledger_collection", "wallets_collection", "topups_collection",
             "users_collection", "verification_tokens_collection", "kyc_documents_collection",
             "payouts_collection", "system_state_collection", "tenants_collection", "partner_customers_collection", "partner_events_collection"]
    for name in names:
        coll = db[name.replace("_collection", "")]
        monkeypatch.setattr(database, name, coll)
        for module in ("app.cards.service", "app.cards.routeendpoint", "app.transactions.orchestrator",
                       "app.webhooks.router", "app.journey.state", "app.users.service",
                       "app.wallet.ledger", "app.mpesa.topup", "app.mobilemoney.service", "app.wallet.router", "app.partners.service", "app.partners.events",
                       "app.partners.router"):
            mod = sys.modules.get(module)
            if mod is not None and hasattr(mod, name):
                monkeypatch.setattr(mod, name, coll)
    yield db


@pytest.fixture
def wasabi_private_key():
    return serialization.load_pem_private_key((_tmp / "wasabi.pem").read_bytes(), password=None)


@pytest.fixture(autouse=True)
def no_wasabi_balance(monkeypatch):
    """The treasury pre-flight asks Wasabi for our balance. Tests must never reach it: an unknown balance
    fails open, exactly as in production when Wasabi cannot be asked. Treasury tests override this."""
    from app.cards import wasabi_client as wc
    from app.core import treasury

    async def unreachable():
        raise wc.WasabiUnknownOutcome("tests do not reach Wasabi")

    monkeypatch.setattr(wc, "account_info", unreachable)
    treasury.forget_balance()
    yield
    treasury.forget_balance()
