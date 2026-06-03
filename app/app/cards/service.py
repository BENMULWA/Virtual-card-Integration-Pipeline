# app/cards/service.py

from app.cards.wasabi_client import create_card, submit_kyc_work_order
from app.transactions.orchestrator import create_transaction, update_transaction_status

async def issue_card(payload: dict):
    merchant_order_no = payload["merchantOrderNo"]

    # 1. Log transaction
    tx = await create_transaction(merchant_order_no, "CREATE_CARD", payload)

    # 2. Extract strictly what /createCard allows
    card_api_payload = {
        "merchantOrderNo": payload.get("merchantOrderNo"),
        "holderId": int(payload["holderId"]),  # Mandated for B2C
        "cardTypeId": int(payload["cardTypeId"]),
        "amount": float(payload["amount"]),
    }

    # 3. Call the API
    response = await create_card(card_api_payload)

    # 4. Save Status
    if response.get("success"):
        await update_transaction_status(merchant_order_no, "WAIT_PROCESS", response)
    else:
        await update_transaction_status(merchant_order_no, "FAILED", response)

    return response


# =========================================================
# ORCHESTRATE WORK ORDER SUBMISSION
# =========================================================
async def process_work_order(payload: dict):
    merchant_order_no = payload["merchantOrderNo"]

    # 1. Create a local tracking reference inside your DB ledger
    tx = await create_transaction(
        merchant_order_no,
        "SUBMIT_WORK_ORDER",
        payload
    )

    # 2. Call Wasabi's signed post pipeline wrapper
    response = await submit_kyc_work_order(payload)

    # 3. Process the response according to gateway response payload
    if response.get("success"):
        # Extracts data dictionary layer safely
        data_layer = response.get("data", {})
        gateway_status = data_layer.get("tradeStatus", "wait_process")
        
        # Map target internal ledger status based on gateway parameters
        internal_status = "WAIT_PROCESS"
        if gateway_status == "processing":
            internal_status = "PROCESSING"
        elif gateway_status == "success":
            internal_status = "SUCCESS"
        elif gateway_status == "fail":
            internal_status = "FAILED"

        await update_transaction_status(
            merchant_order_no,
            internal_status,
            response
        )
    else:
        await update_transaction_status(
            merchant_order_no,
            "FAILED",
            response
        )

    return response
