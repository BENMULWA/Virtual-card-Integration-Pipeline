# =========================================================
# models.py - request schemas (match Wasabi V2 field names)
# =========================================================

from datetime import date
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.core.money import parse_amount


class _Amount(BaseModel):
    amount: Decimal = Field(gt=0)

    @field_validator("amount", mode="before")
    @classmethod
    def _check_amount(cls, v):
        # Strings or ints only: JSON floats already lost precision before reaching us.
        return parse_amount(v)


# =========================================================
# CARDHOLDER (B2C) - values come from the customer / server, never fabricated defaults
# =========================================================
class KycVerification(BaseModel):
    provider: str = Field(..., description="SUMSUB, DIDIT, MYINFO, JUMIO, SHUFTI or REGTANK")
    referenceId: str


class CreateCardholderRequest(BaseModel):
    # Derived from the card type's metadata when the client omits it (see cards/service.py).
    cardHolderModel: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9_]{2,16}$")
    cardTypeId: int
    areaCode: str = Field(..., min_length=2, max_length=5, description="e.g. '+254'")
    mobile: str = Field(..., min_length=5, max_length=20)
    email: EmailStr
    firstName: str = Field(..., min_length=2, max_length=32)
    lastName: str = Field(..., min_length=2, max_length=32)
    birthday: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    nationality: str = Field(..., min_length=2, max_length=2)
    country: str = Field(..., min_length=2, max_length=2)
    town: str = Field(..., min_length=2, max_length=40, pattern=r"^[A-Za-z][A-Za-z .'\-]*$")
    address: str = Field(..., min_length=2, max_length=40, pattern=r"^[A-Za-z0-9\-,. /#]+$")
    postCode: str = Field(..., min_length=3, max_length=15, pattern=r"^[A-Za-z0-9][A-Za-z0-9 \-]*$")
    gender: str = Field(..., pattern=r"^[MF]$")
    occupation: Optional[str] = None
    annualSalary: Optional[str] = None
    accountPurpose: Optional[str] = None
    expectedMonthlyVolume: Optional[str] = None
    idType: str = Field(..., pattern=r"^(PASSPORT|HK_HKID|DLN|GOVERNMENT_ISSUED_ID_CARD)$")
    idNumber: str = Field(..., min_length=2, max_length=50)
    issueDate: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    idNoExpiryDate: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    idFrontId: str
    idBackId: Optional[str] = None
    idHoldId: str
    ssn: Optional[str] = Field(None, pattern=r"^\d{9}$", description="Required for U.S. nationals")
    kycVerification: Optional[KycVerification] = None
    # ipAddress and merchantOrderNo are set server-side, never accepted from the client.

    @field_validator("firstName", "lastName", "town", "address", "postCode", "idNumber", mode="before")
    @classmethod
    def _trim(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("nationality", "country", mode="before")
    @classmethod
    def _upper_country(cls, v):
        return v.strip().upper() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _wasabi_contract(self):
        # Wasabi: firstName + lastName must not exceed 32 characters in total.
        if len(self.firstName) + len(self.lastName) > 32:
            raise ValueError("First name and last name together must be at most 32 characters")

        if self.idType != "PASSPORT" and not self.idBackId:
            raise ValueError("idBackId is required unless idType is PASSPORT")
        if self.nationality == "US" and not self.ssn:
            raise ValueError("ssn is required for U.S. nationals")

        try:
            birthday = date.fromisoformat(self.birthday)
            issued = date.fromisoformat(self.issueDate)
            expires = date.fromisoformat(self.idNoExpiryDate)
        except ValueError as exc:
            raise ValueError(f"Dates must be real calendar dates (YYYY-MM-DD): {exc}") from exc

        today = date.today()
        if birthday >= today:
            raise ValueError("birthday must be in the past")
        if issued > today:
            raise ValueError("issueDate cannot be in the future")
        if expires <= issued:
            raise ValueError("idNoExpiryDate must be after issueDate")
        return self


# =========================================================
# CARD
# =========================================================
class CreateCardRequest(_Amount):
    cardTypeId: int
    holderId: Optional[int] = None  # required only when the card type has needCardHolder=true
    designId: Optional[str] = None


class CardNoRequest(BaseModel):
    cardNo: str = Field(..., min_length=1, max_length=64)
    clientRemark: Optional[str] = Field(None, max_length=50, description="Optional client remark (0-50 chars)")


class CardAmountRequest(_Amount):
    cardNo: str = Field(..., min_length=1, max_length=64)
    clientRemark: Optional[str] = Field(None, max_length=50, description="Optional client remark (0-50 chars)")


class CardListQuery(BaseModel):
    pageNum: int = 1
    pageSize: int = Field(10, le=100)


# =========================================================
# COMPLIANCE FOR KYC SUBMISSION (work ticket)
# =========================================================
class KYCSubmissionRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    target: Optional[str] = Field(None, max_length=255)
    content: Optional[str] = Field(None, max_length=1000)
    files: Optional[List[str]] = None
    tradeType: str = Field("CARD_ACTIVE", description="CARD_ACTIVE or OTHER_TRADE_TYPE")
    cardNo: Optional[str] = None

    @model_validator(mode="after")
    def _card_required_for_card_ticket(self):
        if self.tradeType == "CARD_ACTIVE" and not self.cardNo:
            raise ValueError("cardNo is required when tradeType is CARD_ACTIVE")
        if self.tradeType != "CARD_ACTIVE" and not self.target:
            raise ValueError("target is required for non-card work orders")
        return self
