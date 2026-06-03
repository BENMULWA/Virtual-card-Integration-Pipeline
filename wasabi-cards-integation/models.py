

# =========================================================
# models orchstracture for the application
# =========================================================

from fastapi import FastAPI    
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from enum import Enum

class User(BaseModel):
    user_id: str
    full_name: str
    email: str
    phone_number: Optional[str] = None
    is_active: bool = True
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()


class CardRequest(BaseModel):
    user_id: str
    card_network: str  # e.g., "Visa", "mastercard"
    currency: str  # e.g., "USD"
    spending_limit: float
    created_at: datetime = datetime.utcnow()
    expiration_date: Optional[datetime] = None

class Card(BaseModel):
    card_id: str
    user_id: str
    currency: str  # e.g., "USD"
    status: str  = "active", "inactive", "blocked"
    card_network: str  # e.g., "Visa", "mastercard"
    last_four_digits: str
    expiration_date: datetime
    is_active: bool = True
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

class Wallet(BaseModel):
    user_id: str
    balance: float = 0.0
    currency: str  # e.g., "USD"
    frozen_balance: float = 0.0
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

class Transaction(BaseModel):
    transaction_id: str
    user_id: str
    card_id: Optional[str] = None
    amount: float
    currency: str  # e.g., "USD"
    transaction_type: str  # e.g., "authorization", "capture", "refund"
    status: str  = "pending", "completed", "failed"
    provider_reference: Optional[str] = None
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

class LedgerEntry(BaseModel):
    user_id: str
    amount: float
    currency: str  # e.g., "USD"
    transaction_type: str  # e.g., "debit", "credit"
    reference: str  # e.g., "card_transaction", "refund"
    description: Optional[str] = None
    created_at: datetime = datetime.utcnow()



