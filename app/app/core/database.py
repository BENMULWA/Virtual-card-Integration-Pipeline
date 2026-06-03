# =========================================================
# database.py - MongoDB connection layer
# =========================================================

from motor.motor_asyncio import AsyncIOMotorClient
import certifi

from app.core.config import MONGO_URL, DB_NAME

# =========================================================
# Mongo Client
# =========================================================

client = AsyncIOMotorClient(
    MONGO_URL,
    tls=True,
    tlsCAFile=certifi.where()
)

db = client[DB_NAME]

# =========================================================
# Collections
# =========================================================

users_collection = db["users"]

wallets_collection = db["wallets"]

cards_collection = db["cards"]

transactions_collection = db["transactions"] # card creation

ledger_collection = db["ledger"]