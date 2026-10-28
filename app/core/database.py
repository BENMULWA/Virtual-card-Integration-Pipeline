# =========================================================
# database.py - MongoDB connection layer
# =========================================================

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING
import certifi

from app.core.config import MONGO_URL, DB_NAME

# =========================================================
# Mongo Client
# =========================================================
# Atlas (mongodb+srv://) always needs TLS; a local mongod usually runs without it.
# Passing tls=True to a local server fails the handshake, which made local
# development impossible. Detect the scheme instead of hard-coding one mode.
_IS_SRV = MONGO_URL.startswith("mongodb+srv://")
_USES_TLS = _IS_SRV or "tls=true" in MONGO_URL.lower() or "ssl=true" in MONGO_URL.lower()

_client_kwargs = {}
if _USES_TLS:
    _client_kwargs = {"tls": True, "tlsCAFile": certifi.where()}

client = AsyncIOMotorClient(MONGO_URL, **_client_kwargs)

db = client[DB_NAME]

# =========================================================
# Collections
# =========================================================

users_collection = db["users"]                  # user_id, email, password_hash, email_verified ...

verification_tokens_collection = db["verification_tokens"]   # hashed single-use tokens (email verification)

kyc_documents_collection = db["kyc_documents"]  # KYC files uploaded per user (front/back/selfie)

wallets_collection = db["wallets"]

cards_collection = db["cards"]            # card_no, owner_id, holder_id, status, balance ...

holders_collection = db["holders"]        # holder_id, owner_id, status ...

# New collection on purpose: the legacy "transactions" collection holds old test records with reused
# order numbers that cannot satisfy the unique index. Leave it alone (or archive it) - nothing reads it.
transactions_collection = db["provider_operations"]  # idempotent provider operations

webhook_events_collection = db["webhook_events"]  # deduplicated Wasabi callbacks

ledger_collection = db["ledger"]              # immutable, unique entry_key per balance change

topups_collection = db["topups"]              # M-Pesa / Airtel wallet top-ups

system_state_collection = db["system_state"]            # small operational state (e.g. treasury alert de-duplication)

tenants_collection = db["tenants"]                      # resellers: hashed API key, webhook endpoint, limits

partner_customers_collection = db["partner_customers"]  # a reseller's end customer -> our internal owner_id

partner_events_collection = db["partner_events"]        # outbound webhook queue (signed, retried)

payouts_collection = db["payouts"]            # wallet -> mobile money withdrawals (B2C)

# Card activity (authorisations, fees, transfers, 3DS requests). One row per provider
# event, keyed by the webhook request id so a provider retry cannot duplicate it.
card_transactions_collection = db["card_transactions"]


async def ensure_indexes():
    # Partial: legacy documents (raw webhook dumps) have no merchant_order_no and must not collide as nulls.
    await transactions_collection.create_index(
        "merchant_order_no", unique=True, partialFilterExpression={"merchant_order_no": {"$exists": True}})
    await transactions_collection.create_index([("owner_id", ASCENDING), ("created_at", ASCENDING)])
    await cards_collection.create_index("card_no", unique=True, sparse=True)
    await cards_collection.create_index("owner_id")
    await holders_collection.create_index("holder_id", unique=True, sparse=True)
    await holders_collection.create_index("owner_id")
    await webhook_events_collection.create_index("request_id", unique=True)
    await webhook_events_collection.create_index("owner_id")
    await ledger_collection.create_index("entry_key", unique=True)
    await wallets_collection.create_index("owner_id", unique=True)
    await topups_collection.create_index("checkout_request_id", unique=True)
    await topups_collection.create_index("idem_key", unique=True, sparse=True)
    await topups_collection.create_index("mpesa_receipt", unique=True, sparse=True)
    await topups_collection.create_index("airtel_receipt", unique=True, sparse=True)
    await topups_collection.create_index("owner_id")
    await tenants_collection.create_index("tenant_id", unique=True)
    await tenants_collection.create_index("slug", unique=True)
    await tenants_collection.create_index("key_hash", unique=True)
    await partner_customers_collection.create_index([("tenant_id", ASCENDING), ("external_id", ASCENDING)], unique=True)
    await partner_customers_collection.create_index("owner_id", unique=True)
    await partner_events_collection.create_index("event_id", unique=True)
    await partner_events_collection.create_index([("status", ASCENDING), ("next_attempt_at", ASCENDING)])
    await payouts_collection.create_index("external_id", unique=True)
    await payouts_collection.create_index("idem_key", unique=True, sparse=True)
    await payouts_collection.create_index([("owner_id", ASCENDING), ("created_at", ASCENDING)])
    await topups_collection.create_index([("owner_id", ASCENDING), ("created_at", ASCENDING)])
    # Users
    await users_collection.create_index("user_id", unique=True)
    await users_collection.create_index("email", unique=True)
    # Single-use tokens: the digest is the lookup key and must be unique.
    await verification_tokens_collection.create_index("token_hash", unique=True)
    await verification_tokens_collection.create_index(["user_id", "purpose"])
    # KYC documents: one row per uploaded file, unique per (user, file id).
    await kyc_documents_collection.create_index("owner_id")
    await kyc_documents_collection.create_index(["owner_id", "file_id"], unique=True, sparse=True)
    # Card activity: provider retries carry the same request id, so this is our dedupe key.
    await card_transactions_collection.create_index("event_key", unique=True, sparse=True)
    await card_transactions_collection.create_index("owner_id")
    await card_transactions_collection.create_index("card_no")
    await card_transactions_collection.create_index([("owner_id", ASCENDING), ("created_at", ASCENDING)])
