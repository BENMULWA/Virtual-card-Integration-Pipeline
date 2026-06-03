# =========================================================
# database.py
# MongoDB connection layer
# =========================================================

from motor.motor_asyncio import AsyncIOMotorClient
from wasabi-cards-integation.core.config import MONGO_URL, DB_NAME


client = AsyncIOMotorClient(MONGO_URL)

db = client[DB_NAME]

wallets_collection = db["wallets"]
cards_collection = db["cards"]
transactions_collection = db["transactions"]
ledger_collection = db["ledger"]
users_collection = db["users"]