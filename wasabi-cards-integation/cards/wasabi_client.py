

import requests

from app.core.config import WASABI_BASE_URL, WASABI_API_KEY, WASABI_API_SECRET, HEADERS


# =========================================================
# GENERIC POST WRAPPER
# =========================================================
def post(endpoint: str, payload: dict):

    url = f"{WASABI_BASE_URL}{endpoint}"

    response = requests.post(
        url,
        json=payload,
        headers=HEADERS,
        timeout=40
    )

    return response.json()


# =========================================================
#CCard Type / BINS
# =========================================================
def get_card_types():

    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/v2/cardTypes"

    response = requests.post(
        url,
        json={},
        headers=HEADERS
    )

    return response.json()


# =========================================================
# CREATE CARD wrapper
# =========================================================
def create_card(payload: dict):

    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/v2/createCard"

    response = requests.post(
        url,
        json=payload,
        headers=HEADERS
    )

    return response.json()


# =========================================================
# Card INFO wrapper
# =========================================================
def card_info(payload: dict):
    
    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/info"

    response = requests.post(
        url,
        json=payload,
        headers=HEADERS
    )

    return response.json()


# =========================================================
# WASABI API HEADERS
# =========================================================
def card_sensitive(payload: dict):
    
    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/sensitive"

    response = requests.post(
        url,
        json=payload,
        headers=HEADERS
    )

    return response.json()


# =========================================================
# Card Balance
# =========================================================
def card_balance(payload: dict):
    
    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/balanceInfo"

    response = requests.post(
        url,
        json=payload,
        headers=HEADERS
    )

    return response.json()


# =========================================================
#Card List
# =========================================================
def card_list(payload: dict):
    
    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/list"

    response = requests.post(
        url,
        json=payload,
        headers=HEADERS
    )

    return response.json()


# =========================================================
# Freeze card 
# =========================================================
def freeze_card(payload: dict):
    
    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/v2/freeze"

    response = requests.post(
        url,
        json=payload,
        headers=HEADERS
    )

    return response.json()


# =========================================================
# Unfreeze Card
# =========================================================
def unfreeze_card(payload: dict):
    
    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/v2/unfreeze"

    response = requests.post(
        url,
        json=payload,
        headers=HEADERS
    )

    return response.json()

# =========================================================
# Withdraw money from the card
# =========================================================

def withdraw_card(payload: dict):
    
    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/withdraw"

    response = requests.post(
        url,
        json=payload,
        headers=HEADERS
    )

    return response.json()


# =========================================================
# Cance Card transaction
# =========================================================
def cancel_card_transaction(payload: dict):
    
    url = f"{WASABI_BASE_URL}/merchant/core/mcb/card/cancel"

    response = requests.post(
        url,
        json=payload,
        headers=HEADERS
    )

    return response.json()