# =========================================================
# schemas.py - Pydantic models (FIXED)
# =========================================================

from pydantic import BaseModel, Field, EmailStr
from typing import Optional,List
from datetime import datetime

# =========================================================
# USER
# =========================================================
class User(BaseModel):
    user_id: str
    full_name: str
    email: str
    phone_number: Optional[str] = None
    is_active: bool = True
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()


# =========================================================
# PHASE 1: CREATE CARDHOLDER REQUEST (Strictly Matches Wasabi Docs)
# =========================================================
class CreateCardholderRequest(BaseModel):
    cardHolderModel: str = "B2C"
    cardTypeId: int
    areaCode: str = Field(..., description="Mobile phone area code. Length[2...5], e.g., '+254'")
    mobile: str = Field(..., description="Mobile phone number without area code. Length[5...20]")
    email: EmailStr = Field(..., description="Email address for verification codes")
    firstName: str = Field(..., description="First Name (English characters only)")
    lastName: str = Field(..., description="Last Name (English characters only)")
    birthday: str = Field(..., description="Format: yyyy-MM-dd")
    nationality: str = Field(..., description="Nationality Code (ISO 3166-1 alpha-2, e.g., 'KE')")
    country: str = Field(..., description="Country Code for billing address (ISO 3166-1 alpha-2)")
    town: str = Field(..., description="City/Town Code from City List Interface")
    address: str = Field(..., description="Billing street address. Letters, numbers, hyphens, spaces only")
    postCode: str = Field(..., description="Postal code for billing address")
    gender: str = Field(..., description="M for male, F for female")
    occupation: str = Field(..., description="Occupation code from interface parameters")
    annualSalary: str = Field("50000 USD", description="Annual salary text schema format")
    accountPurpose: str = Field("Living Expense", description="Only supports English text description")
    expectedMonthlyVolume: str = Field("10000 USD", description="Expected monthly transaction volume")
    idType: str = Field(..., description="PASSPORT, HK_HKID, DLN, or GOVERNMENT_ISSUED_ID_CARD")
    idNumber: str = Field(..., description="Document number identifier")
    issueDate: str = Field(..., description="ID document issuance date. Format: yyyy-MM-dd")
    idNoExpiryDate: str = Field(..., description="ID document expiration date. Format: yyyy-MM-dd")
    idFrontId: str = Field(..., description="Front photo File ID from upload interface")
    idBackId: str = Field(..., description="Back photo File ID from upload interface")
    idHoldId: str = Field(..., description="Face selfie photo File ID from upload interface")
    ipAddress: str = Field("127.0.0.1", description="Client IPV4 address string")
    
    # Injected automatically by your helper inside routeendpoint.py
    merchantOrderNo: Optional[str] = Field(default=None, min_length=20, max_length=40)


# =========================================================
# PHASE 2: CREATE CARD REQUEST (Strictly Matches Wasabi V2 Specs)
# =========================================================
class CreateCardRequest(BaseModel):
    cardTypeId: int  # Identifies the target card BIN profile
    holderId: int    # MANDATORY LONG ID returned from Phase 1 registration
    amount: float = Field(gt=0)  # Must meet depositAmountMinQuotaForActiveCard (\$10)
    
    # Optional gateway modifiers
    cardNumber: Optional[str] = None  # Required for physical card manufacturing tracking only
    accountId: Optional[int] = None   # Explicit shared asset pool token parameter
    designId: Optional[str] = None     # Brand indicator layout key for corporate white-label builds

    # Injected automatically by your routes helper function
    merchantOrderNo: Optional[str] = Field(default=None, min_length=15, max_length=65)


# =========================================================
# SUPPORT MODELS
# =========================================================
class CardIdRequest(BaseModel):
    cardId: str


class CardAmountRequest(BaseModel):
    cardId: str
    amount: float = Field(gt=0)
    merchantOrderNo: Optional[str] = Field(default=None, min_length=15, max_length=65)


class Card(BaseModel):
    card_id: str
    user_id: str
    currency: str
    status: str = "active"
    card_network: str
    last_four_digits: str
    expiration_date: datetime
    is_active: bool = True


class CardListRequest(BaseModel):
    user_id: str
    page: int = 1
    page_size: int = 10


class CardTransactionRequest(BaseModel):
    card_id: str
    amount: float = Field(gt=0)


#=========================================================
# COMPLIENCE FOR KYC SUBMISSION
#=========================================================

class KYCSubmissionRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255, description="Document title for KYC submission")
    target: str = Field(..., min_length=1, max_length=255, description="Target entity for KYC document (e.g., 'cardholder', 'account')")
    content: Optional[str] = Field(None, max_length=1000, description="Base64-encoded content for text-based KYC documents")
    files: Optional[List[str]] = Field(None, description="List of File IDs for file-based KYC documents uploaded via Wasabi's file upload endpoint")
    tradeType: str = Field("CARD_ACTIVE", description="CARD_ACTIVE or OTHER_TRADE_TYPE")

    # Injected programmatically by the router layer to ensure traceability
    merchantOrderNo: Optional[str] = Field(default=None, min_length=20, max_length=55)