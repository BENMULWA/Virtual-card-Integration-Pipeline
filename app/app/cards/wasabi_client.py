# =========================================================
# wasabi_client.py - Wasabi API integration layer
# =========================================================

import httpx
import json

from app.core.config import (WASABI_BASE_URL,WASABI_API_KEY)

from app.core.signature import (generate_signature,get_public_key)

# =========================================================
# PAYLOAD STRUCTURE
# =========================================================
def clean_payload(payload: dict) -> dict:
    return {
        k: v
        for k, v in payload.items()
        if v is not None
    }


# =========================================================
# GENERIC POST REQUEST (WASABI SIGNED)
# =========================================================
async def post(endpoint: str, payload: dict = None):

    payload = payload if payload is not None else {}

    url = f"{WASABI_BASE_URL}{endpoint}"

    # -----------------------------------------------------
    # 1. CLEAN PAYLOAD (CRITICAL)
    # -----------------------------------------------------
    clean = clean_payload(payload)

    # OPTIONAL: enforce consistent numeric types
    if "amount" in clean:
        clean["amount"] = int(clean["amount"])

    # -----------------------------------------------------
    # 2. STRINGIFY PAYLOAD (STRICT ALPHABETICAL ORDERING)
    # -----------------------------------------------------
    # We generate the EXACT unspaced, sorted string that your 
    # signature script uses, and that Wasabi expects.

    
    canonical_string = json.dumps(clean, separators=(",", ":"), sort_keys=True)

    # -----------------------------------------------------
    # 3. GENERATE SIGNATURE FROM CLEAN PAYLOAD ONLY
    # -----------------------------------------------------
    signature,_= generate_signature(clean)  # Pass empty dict if no payload, to ensure signature is generated from empty string

    # -----------------------------------------------------
    # 4. BUILD HEADERS (STRICT MATCH WASABI DOCS)
    # -----------------------------------------------------
    headers = {
        "Content-Type": "application/json",
        "X-WSB-API-KEY": WASABI_API_KEY,
        "X-WSB-SIGNATURE": signature
    }
    

    # -----------------------------------------------------
    # 5. DEBUG (VERY IMPORTANT FOR SIGNATURE ISSUES)
    # -----------------------------------------------------
    print("\n=== WASABI REQUEST DEBUG ===")
    print("URL:", url)
    print("CLEAN PAYLOAD:", json.dumps(clean, indent=2))
    print("CANONICAL STRING:", canonical_string)
    print("SIGNATURE:", signature)
    print("============================\n")

    # -----------------------------------------------------
    # 6. SEND REQUEST (FIXED TO TRANSMIT EXACT BYTES)
    # -----------------------------------------------------
    async with httpx.AsyncClient(timeout=40) as client:
        response = await client.post(
            url,
            content=canonical_string,  # CRITICAL FIX: Use content instead of json=
            headers=headers
        )

    # -----------------------------------------------------
    # 7. LOG RESPONSE
    # -----------------------------------------------------
    print("STATUS:", response.status_code)
    print("BODY:", response.text)

    return response.json()


# =========================================================
# CARD TYPES
# =========================================================

async def get_card_types():

    return await post(
        "/merchant/core/mcb/card/v2/cardTypes",
        {}
    )

# =========================================================
# CITIES / TOWNS
# =========================================================

async def get_wasabi_cities(region_code: str = "KE"):
    """
    Queries Wasabi's database explicitly for the specified region code.
    """
    endpoint = "/merchant/core/mcb/common/city"
    
    # Ensure regionCode matches case-sensitivity required by documentation
    payload = {"regionCode": str(region_code).upper()} 
    
    return await post(endpoint, payload)

# app/cards/wasabi_client.py KYC

async def create_cardholder(payload: dict):
    """
    Registers a cardholder via Wasabi's explicit Cardholder-Create-v2 architecture.
    """
    return await post(
        "/merchant/core/mcb/card/holder/v2/create",
        payload
    )

#==========================================================
# UPLOADING FILE FOR KYC TRANSFER PIPELINE
#==========================================================
async def upload_wasabi_file(file_bytes: bytes,filename: str,content_type: str):
    url = f"{WASABI_BASE_URL}/merchant/core/mcb/common/file/upload"

    signature, _ = generate_signature({})

    headers = {
        "X-WSB-API-KEY": WASABI_API_KEY,
        "X-WSB-SIGNATURE": signature,
    }

    files = {
        "file": (filename, file_bytes, content_type)
    }

    # Note: Wasabi's file upload endpoint expects multipart/form-data with the file in the "file" field, and an empty JSON body for signature generation. 
    # The signature is generated from an empty payload, as per their documentation.
    data = {
        "category": "card"
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            url,
            headers=headers,
            data=data,
            files=files
        )

        print("Status:", response.status_code)
        print("Response:", response.text)

        return response.json()


# =========================================================
# SUBMIT WORK ORDER FOR COMPLIENCE/ KYC REVIEW AND APPROVAL
# =========================================================

async def submit_kyc_work_order(payload: dict):
    """
    Submits a KYC work order to Wasabi's compliance engine.
    The payload must include the merchantOrderNo for traceability, and either content or files for the KYC document.
    """
    return await post(
        "/merchant/core/mcb/work/submit",
        payload
    )

# =========================================================
# CREATE CARD
# =========================================================

async def create_card(payload: dict):

    return await post(
        "/merchant/core/mcb/card/v2/createCard",
        payload
    )


# =========================================================
# CARD INFO
# =========================================================

async def card_info(payload: dict):

    return await post(
        "/merchant/core/mcb/card/info",
        payload
    )


# =========================================================
# CARD SENSITIVE
# =========================================================

async def card_sensitive(payload: dict):

    return await post(
        "/merchant/core/mcb/card/sensitive",
        payload
    )


# =========================================================
# CARD BALANCE
# =========================================================

async def card_balance(payload: dict):

    return await post(
        "/merchant/core/mcb/card/balanceInfo",
        payload
    )


# =========================================================
# CARD LIST
# =========================================================

async def card_list(payload: dict):

    return await post(
        "/merchant/core/mcb/card/list",
        payload
    )


# =========================================================
# FREEZE CARD
# =========================================================

async def freeze_card(payload: dict):

    return await post(
        "/merchant/core/mcb/card/v2/freeze",
        payload
    )


# =========================================================
# UNFREEZE CARD
# =========================================================

async def unfreeze_card(payload: dict):

    return await post(
        "/merchant/core/mcb/card/v2/unfreeze",
        payload
    )


# =========================================================
# DEPOSIT
# =========================================================

async def deposit(payload: dict):

    return await post(
        "/merchant/core/mcb/card/deposit",
        payload
    )


# =========================================================
# WITHDRAW
# =========================================================

async def withdraw(payload: dict):

    return await post(
        "/merchant/core/mcb/card/withdraw",
        payload
    )


# =========================================================
# CANCEL CARD
# =========================================================

async def cancel_card(payload: dict):

    return await post(
        "/merchant/core/mcb/card/cancel",
        payload
    )