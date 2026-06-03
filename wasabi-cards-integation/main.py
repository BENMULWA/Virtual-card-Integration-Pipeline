# =========================================================
# main.py
# Mamlaka Hub & Spoke + Wasabi Virtual Cards intergration
# =========================================================

from fastapi import FastAPI, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

from datetime import datetime, timedelta
import requests
import os
import uuid
import jwt
import hashlib 
import json 
# =========================================================
# LOAD ENV VARIABLES
# =========================================================

load_dotenv()

# =========================================================
# APP CONFIGURATION
# =========================================================

app = FastAPI(
    title="Mamlaka Wasabi Cards API",
    version="1.0.0",
    description="Production Ready Virtual Card Infrastructure"
)

# =========================================================
# ENVIRONMENT VARIABLES
# =========================================================

# DATABASE
MONGO_URL = os.getenv("MONGO_URL")
DB_NAME = os.getenv("DB_NAME")

# WASABI
WASABI_BASE_URL = os.getenv("WASABI_BASE_URL")
WASABI_API_KEY = os.getenv("WASABI_API_KEY")
WASABI_MERCHANT_ID = os.getenv("WASABI_MERCHANT_ID")

# WEBHOOKS
WASABI_WEBHOOK_URL = os.getenv("WASABI_WEBHOOK_URL")

# JWT
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM")

# RSA KEYS
RSA_PRIVATE_KEY_PATH = os.getenv("RSA_PRIVATE_KEY_PATH")
RSA_PUBLIC_KEY_PATH = os.getenv("RSA_PUBLIC_KEY_PATH")
WASABI_PUBLIC_KEY_PATH = os.getenv("WASABI_PUBLIC_KEY_PATH")

# =========================================================
# DATABASE CONNECTION
# =========================================================

client = AsyncIOMotorClient(MONGO_URL)

db = client[DB_NAME]

# collections
users_collection = db["users"]
wallets_collection = db["wallets"]
cards_collection = db["cards"]
transactions_collection = db["transactions"]
ledger_collection = db["ledger"]


# =========================================================
# JWT TOKEN Creation
# =========================================================
def create_access_token(data: dict, expires_delta: int = 3600):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(seconds=expires_delta)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return encoded_jwt


# =========================================================
# JWT Verify Token
# =========================================================
def verify_token(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail="Token has expired"
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication token"
        )


# =========================================================
# Verify Signature
# =========================================================
def verify_signature(payload: dict, signature: str):
    with open(WASABI_PUBLIC_KEY_PATH, "r") as f:
        public_key = f.read()

    payload_str = json.dumps(payload, separators=(',', ':'))
    payload_hash = hashlib.sha256(payload_str.encode()).hexdigest()

    try:
        jwt.decode(signature, public_key, algorithms=["RS256"], options={"verify_exp": False})
        return True
    except jwt.InvalidSignatureError:
        return False
    except Exception as e:
        print(f"Error verifying signature: {e}")
        return False

# =========================================================
# WASABI API HEADERS
# =========================================================

HEADERS = {
    "Content-Type": "application/json",
    "X-WSB-API-KEY": WASABI_API_KEY
}



# =========================================================
# ROOT ENDPOINT
# =========================================================

@app.get("/")
async def root():

    return {
        "status": "running",
        "service": "Mamlaka Wasabi Cards API"
    }

# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/health")
async def health():

    return {
        "success": True,
        "database": "connected",
        "environment": "production"
    }

# =========================================================
# WALLET ENDPOINTS
# =========================================================

# CREATE WALLET
@app.post("/wallets/create")
async def create_wallet(payload: WalletCreate):

    existing_wallet = await wallets_collection.find_one({
        "user_id": payload.user_id
    })

    if existing_wallet:
        raise HTTPException(
            status_code=400,
            detail="Wallet already exists"
        )

    wallet = {
        "user_id": payload.user_id,
        "balance": 0,
        "currency": "USD",
        "created_at": datetime.utcnow()
    }

    await wallets_collection.insert_one(wallet)

    return {
        "success": True,
        "wallet": wallet
    }

# CREDIT WALLET
@app.post("/wallets/credit")
async def credit_wallet(payload: WalletCredit):

    await wallets_collection.update_one(
        {"user_id": payload.user_id},
        {"$inc": {"balance": payload.amount}}
    )

    wallet = await wallets_collection.find_one({
        "user_id": payload.user_id
    })

    # ledger entry
    ledger_entry = {
        "user_id": payload.user_id,
        "amount": payload.amount,
        "transaction_type": "CREDIT",
        "reference": str(uuid.uuid4()),
        "created_at": datetime.utcnow()
    }

    await ledger_collection.insert_one(ledger_entry)

    return {
        "success": True,
        "wallet": wallet
    }

# GET WALLET
@app.get("/wallets/{user_id}")
async def get_wallet(user_id: str):

    wallet = await wallets_collection.find_one({
        "user_id": user_id
    })

    if not wallet:
        raise HTTPException(
            status_code=404,
            detail="Wallet not found"
        )

    return wallet

# =========================================================
# WASABI CARD ENDPOINTS
# =========================================================

# =========================================================
# 1. GET CARD TYPES / BINS
# POST /merchant/core/mcb/card/v2/cardTypes
# =========================================================

@app.get("/cards/types")
async def get_card_types():

    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/v2/cardTypes"

    response = requests.post(
        url,
        json={},
        headers=HEADERS
    )

    return response.json()

# =========================================================
# 2. CREATE VIRTUAL CARD
# POST /merchant/core/mcb/card/v2/createCard
# =========================================================

@app.post("/cards/create")
async def create_card(payload: CardRequest):

    # check wallet
    wallet = await wallets_collection.find_one({
        "user_id": payload.userId
    })

    if not wallet:
        raise HTTPException(
            status_code=404,
            detail="Wallet not found"
        )

    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/v2/createCard"

    response = requests.post(
        url,
        json=payload.dict(),
        headers=HEADERS
    )

    result = response.json()

    # save locally
    if result.get("code") == "000000":

        card_data = {
            "user_id": payload.userId,
            "wasabi_response": result,
            "card_type_id": payload.cardTypeId,
            "status": "ACTIVE",
            "created_at": datetime.utcnow()
        }

        await cards_collection.insert_one(card_data)

    return result

# =========================================================
# 3. CARD INFO
# POST /merchant/core/mcb/card/info
# =========================================================

@app.post("/cards/info")
async def card_info(payload: CardInfoRequest):

    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/info"

    response = requests.post(
        url,
        json=payload.dict(),
        headers=HEADERS
    )

    return response.json()

# =========================================================
# 4. CARD SENSITIVE INFO
# POST /merchant/core/mcb/card/sensitive
# =========================================================

@app.post("/cards/sensitive")
async def card_sensitive(payload: CardInfoRequest):

    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/sensitive"

    response = requests.post(
        url,
        json=payload.dict(),
        headers=HEADERS
    )

    return response.json()

# =========================================================
# 5. CARD BALANCE
# POST /merchant/core/mcb/card/balanceInfo
# =========================================================

@app.post("/cards/balance")
async def card_balance(payload: CardInfoRequest):

    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/balanceInfo"

    response = requests.post(
        url,
        json=payload.dict(),
        headers=HEADERS
    )

    return response.json()

# =========================================================
# 6. CARD LIST
# POST /merchant/core/mcb/card/list
# =========================================================

@app.post("/cards/list")
async def card_list(payload: dict):

    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/list"

    response = requests.post(
        url,
        json=payload,
        headers=HEADERS
    )

    return response.json()

# =========================================================
# 7. FREEZE CARD
# POST /merchant/core/mcb/card/v2/freeze
# =========================================================

@app.post("/cards/freeze")
async def freeze_card(payload: FreezeRequest):

    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/v2/freeze"

    response = requests.post(
        url,
        json=payload.dict(),
        headers=HEADERS
    )

    return response.json()

# =========================================================
# 8. UNFREEZE CARD
# POST /merchant/core/mcb/card/v2/unfreeze
# =========================================================

@app.post("/cards/unfreeze")
async def unfreeze_card(payload: FreezeRequest):

    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/v2/unfreeze"

    response = requests.post(
        url,
        json=payload.dict(),
        headers=HEADERS
    )

    return response.json()

# =========================================================
# 9. CARD DEPOSIT
# POST /merchant/core/mcb/card/deposit
# =========================================================

@app.post("/cards/deposit")
async def deposit_card(payload: DepositRequest):

    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/deposit"

    response = requests.post(
        url,
        json=payload.dict(),
        headers=HEADERS
    )

    result = response.json()

    # ledger
    ledger_entry = {
        "card_id": payload.cardId,
        "amount": payload.amount,
        "transaction_type": "DEPOSIT",
        "reference": str(uuid.uuid4()),
        "created_at": datetime.utcnow()
    }

    await ledger_collection.insert_one(ledger_entry)

    return result

# =========================================================
# 10. CARD WITHDRAW
# POST /merchant/core/mcb/card/withdraw
# =========================================================

@app.post("/cards/withdraw")
async def withdraw_card(payload: WithdrawRequest):

    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/withdraw"

    response = requests.post(
        url,
        json=payload.dict(),
        headers=HEADERS
    )

    result = response.json()

    ledger_entry = {
        "card_id": payload.cardId,
        "amount": payload.amount,
        "transaction_type": "WITHDRAW",
        "reference": str(uuid.uuid4()),
        "created_at": datetime.utcnow()
    }

    await ledger_collection.insert_one(ledger_entry)

    return result

# =========================================================
# 11. CANCEL CARD
# POST /merchant/core/mcb/card/cancel
# =========================================================

@app.post("/cards/cancel")
async def cancel_card(payload: FreezeRequest):

    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/cancel"

    response = requests.post(
        url,
        json=payload.dict(),
        headers=HEADERS
    )

    return response.json()

# =========================================================
# WEBHOOK ENDPOINT
# =========================================================

@app.post("/webhooks/wasabi")
async def wasabi_webhook(request: Request):

    payload = await request.json()

    print("Webhook received")

    print(payload)

    # store webhook
    await transactions_collection.insert_one({
        "payload": payload,
        "created_at": datetime.utcnow()
    })

    return {
        "success": True
    }

# =========================================================
# GET USER CARDS
# =========================================================

@app.get("/cards/user/{user_id}")
async def get_user_cards(user_id: str):

    cards = []

    async for card in cards_collection.find({
        "user_id": user_id
    }):
        cards.append(card)

    return {
        "success": True,
        "cards": cards
    }

# =========================================================
# GET LEDGER ENTRIES
# =========================================================

@app.get("/ledger/{user_id}")
async def get_ledger(user_id: str):

    entries = []

    async for entry in ledger_collection.find({
        "user_id": user_id
    }):
        entries.append(entry)

    return {
        "success": True,
        "ledger": entries
    }

