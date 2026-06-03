import json
import base64
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from app.core.config import RSA_PRIVATE_KEY_PATH, RSA_PUBLIC_KEY_PATH

def generate_signature(payload) -> tuple[str, str]:
    # Ensure payload matches the exact JSON string being transmitted
    if isinstance(payload, str):
        canonical_message = payload
    else:
        canonical_message = json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True
        )

    print("\n=== CANONICAL MESSAGE ===")
    print(canonical_message)
    print("================================")

    message_bytes = canonical_message.encode("utf-8")

    # Load private key
    with open(RSA_PRIVATE_KEY_PATH, "rb") as f:
        private_key = serialization.load_pem_private_key(
            f.read(),
            password=None
        )

    # RSA SHA256 sign
    signature_bytes = private_key.sign(
        message_bytes,
        padding.PKCS1v15(), 
        hashes.SHA256()
    )

    # FIXED: Convert directly to BASE64 string (Required by WasabiCard)
    signature_b64 = base64.b64encode(signature_bytes).decode("utf-8")

    print("\n=== GENERATED RSA SIGNATURE (BASE64) ===")
    print(signature_b64)
    print("================================\n")

    return signature_b64, canonical_message



# =========================================================
# GET PUBLIC KEY
# =========================================================
def get_public_key():
    with open(RSA_PUBLIC_KEY_PATH, "rb") as f:
        key = f.read().decode()

    if not key:
        raise ValueError("Public key is empty or not loaded")

    key = key.replace("-----BEGIN PUBLIC KEY-----", "")
    key = key.replace("-----END PUBLIC KEY-----", "")
    key = key.replace("\n", "").strip()

    return key