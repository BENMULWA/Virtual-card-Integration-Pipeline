# =========================================================
# router.py - Card API endpoints
# =========================================================

import secrets
from datetime import datetime
from fastapi import APIRouter, Depends, Query, UploadFile, File


# Ensure auth_guard returns your User model or user contextual information
from app.core.security import auth_guard
from app.models import (CreateCardRequest, CardIdRequest, CardAmountRequest, CreateCardholderRequest, KYCSubmissionRequest)
   
from app.cards.service import issue_card
from app.cards.wasabi_client import (
    get_card_types,
    get_wasabi_cities,
    create_cardholder,
    submit_kyc_work_order,
    upload_wasabi_file,
    card_info,
    card_sensitive,
    card_balance,
    card_list,
    freeze_card,
    unfreeze_card,
    deposit,
    withdraw,
    cancel_card
)


router = APIRouter()

# =========================================================
# HELPER: ORDER NUMBER GENERATOR
# =========================================================
def generate_merchant_order_no(user_id: str, prefix: str = "ORD") -> str:
    """
    Generates a deterministic unique reference string for transactions.
    Format: [PREFIX][YYYYMMDDHHMMSS][6-HEX-CHARS][USER_ID]
    """
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    random_suffix = secrets.token_hex(3).upper()
    return f"{prefix}{timestamp}{random_suffix}U{user_id}"


# =========================================================
# GET CARD TYPES
# =========================================================
@router.post("/supported-BINS/types")
async def card_types(current_user: dict = Depends(auth_guard)):
    return await get_card_types()


# =========================================================
# GET CARD LIST (FIXED: Converted from Body to Query Params)
# =========================================================
@router.get("/list-BINS")
async def list_cards(
    page: int = Query(1, alias="page"),
    size: int = Query(10, alias="pageSize"),
    current_user: dict = Depends(auth_guard)
):
    # Map layout to match what wasabi_client expects for filtering lists
    payload = {
        "pageNum": page,
        "pageSize": size,
        "userId": str(current_user.get("id"))
    }
    return await card_list(payload)


# =========================================================
# Card Sensitive Data
# =========================================================
@router.post("/sensitive")
async def sensitive(payload: CardIdRequest, current_user: dict = Depends(auth_guard)):
    return await card_sensitive(payload.dict())
#=========================================================
# city /towns 
# =========================================================
@router.get("/diagnostic/kenya-cities")
async def lookup_kenya_cities(current_user: dict = Depends(auth_guard)):
    """
    Helper endpoint to print valid Wasabi town codes for Kenya.
    """
    return await get_wasabi_cities("KE")

#==========================================================
# UPLOAD KYC FILES
#==========================================================
@router.post("/kyc/upload")
async def upload_kyc_file(file: UploadFile = File(...),
        #current_user: dict = Depends(auth_guard)
                                      
):
    
    """
    Uploads a verification document binary to secure a valid file ID string.
    Only JPG, PNG, and PDF formats are suppoted by Wasabi's compliance engine.
    """
    file_bytes = await file.read()
    return await upload_wasabi_file(
        file_bytes, file.filename, 
        file.content_type
        )

# =========================================================
# CREATE CARDHOLDER (Strictly Matches Wasabi V2 Specs)
# =========================================================
@router.post("/cardholder/register")
async def register_holder(payload: CreateCardholderRequest, current_user: dict = Depends(auth_guard)):
    data = payload.dict()
    # Inject client transaction trace ID block manually for the validation chain
    user_id = str(current_user.get("id", "guest"))
    data["merchantOrderNo"] = generate_merchant_order_no(user_id, "KYC")
    
    return await create_cardholder(data)


# =========================================================
# SUBMIT WORK ODER FOR KYC REVIEW AND APPROVAL  
# =========================================================
@router.post("/kyc/submit")
async def submit_kyc(payload: KYCSubmissionRequest, current_user: dict = Depends(auth_guard)):
    data = payload.dict()

    #Inject unique tracking key matching same format on the models
    user_id = str(current_user.get("id", "guest"))
    data["merchantOrderNo"] = generate_merchant_order_no(user_id, "KYC")

    return await submit_kyc_work_order(data)

# =========================================================
# CREATE CARD (Uses issue_card Service Layer)
# =========================================================
@router.post("/create")
async def create(payload: CreateCardRequest, current_user: dict = Depends(auth_guard)):
    data = payload.dict()
    
    # Inject generated unique tracking key
    user_id = str(current_user.get("id", "guest"))
    data["merchantOrderNo"] = generate_merchant_order_no(user_id, "ORD")
    
    return await issue_card(data)


# =========================================================
# CARD INFO
# =========================================================
@router.post("/info")
async def info(payload: CardIdRequest, current_user: dict = Depends(auth_guard)):
    return await card_info(payload.dict())


# =========================================================
# Deposit TO CARD
# =========================================================
@router.post("/deposit")
async def deposit_to_card(payload: CardAmountRequest, current_user: dict = Depends(auth_guard)):
    data = payload.dict()
    
    # Inject tracking number for the deposit ledger
    user_id = str(current_user.get("id", "guest"))
    data["merchantOrderNo"] = generate_merchant_order_no(user_id, "DEP")
    
    return await deposit(data)


# =========================================================
# CARD BALANCE
# =========================================================
@router.post("/balance")
async def balance(payload: CardIdRequest, current_user: dict = Depends(auth_guard)):
    return await card_balance(payload.dict())


# =========================================================
# Withdraw from CARD
# =========================================================
@router.post("/withdraw")
async def withdraw_from_card(payload: CardAmountRequest, current_user: dict = Depends(auth_guard)):
    data = payload.dict()
    
    # Inject tracking number for the withdrawal log
    user_id = str(current_user.get("id", "guest"))
    data["merchantOrderNo"] = generate_merchant_order_no(user_id, "WTH")
    
    return await withdraw(data)   


# =========================================================
# FREEZE CARD
# =========================================================
@router.post("/freeze")
async def freeze(payload: CardIdRequest, current_user: dict = Depends(auth_guard)):
    return await freeze_card(payload.dict())


# =========================================================
# UNFREEZE CARD
# =========================================================
@router.post("/unfreeze")
async def unfreeze(payload: CardIdRequest, current_user: dict = Depends(auth_guard)):
    return await unfreeze_card(payload.dict())


# =========================================================
# CANCEL CARD TRANSACTION
# =========================================================
@router.post("/cancel")
async def cancel(payload: CardIdRequest, current_user: dict = Depends(auth_guard)):
    data = payload.dict()
    
    # Some gateways require an order number to trace the cancellation request
    user_id = str(current_user.get("id", "guest"))
    data["merchantOrderNo"] = generate_merchant_order_no(user_id, "CNL")
    
    return await cancel_card(data)
