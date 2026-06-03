# =========================================================
# orchestrator.py
# Transaction lifecycle manager for payment processing
# =========================================================

from datetime import datetime
import uuid

from app.core.database import transactions_collection


# ---------------------------------------------------------
# CREATE TRANSACTION
# ---------------------------------------------------------

async def create_transaction(
    merchant_order_no: str,
    tx_type: str,
    payload: dict
):

    transaction = {

        "transaction_id": str(uuid.uuid4()),

        "merchant_order_no": merchant_order_no,

        "type": tx_type,

        "status": "INITIATED",

        "payload": payload,

        "created_at": datetime.utcnow()
    }

    await transactions_collection.insert_one(
        transaction
    )

    return transaction


# ---------------------------------------------------------
# UPDATE TRANSACTION STATUS
# ---------------------------------------------------------

async def update_transaction_status(
    merchant_order_no: str,
    status: str,
    provider_response: dict
):

    await transactions_collection.update_one(
        {
            "merchant_order_no": merchant_order_no
        },
        {
            "$set": {
                "status": status,
                "provider_response": provider_response
            }
        }
    )