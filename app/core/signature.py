import base64
import json
from functools import lru_cache

from cryptography.exceptions import InvalidSignature, InvalidKey
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, utils

from app.core.config import RSA_PRIVATE_KEY_PATH, RSA_PUBLIC_KEY_PATH, WASABI_PUBLIC_KEY_PATH


def canonical_json(payload: dict) -> str:
    """The exact string that is both signed and transmitted."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _load_private_key(data: bytes):
    try:
        return serialization.load_pem_private_key(data, password=None)
    except ValueError:
        # Some generators wrap a PKCS#8 body in an "RSA PRIVATE KEY" (PKCS#1) header. OpenSSL accepts
        # that, strict parsers do not. Retry in memory with the correct label; the file is untouched.
        fixed = data.replace(b"BEGIN RSA PRIVATE KEY", b"BEGIN PRIVATE KEY") \
                    .replace(b"END RSA PRIVATE KEY", b"END PRIVATE KEY")
        return serialization.load_pem_private_key(fixed, password=None)


def _load_public_key(data: bytes):
    """Accepts a PEM public key or the bare base64 (X.509 SubjectPublicKeyInfo) Wasabi shows in its dashboard."""
    if b"BEGIN" in data:
        return serialization.load_pem_public_key(data)
    return serialization.load_der_public_key(base64.b64decode(b"".join(data.split())))


@lru_cache(maxsize=1)
def _private_key():
    with open(RSA_PRIVATE_KEY_PATH, "rb") as f:
        return _load_private_key(f.read())


@lru_cache(maxsize=1)
def _wasabi_public_key():
    with open(WASABI_PUBLIC_KEY_PATH, "rb") as f:
        return _load_public_key(f.read())


def sign_message(message: str) -> str:
    """RSA PKCS#1 v1.5 / SHA-256, Base64 encoded."""
    signature = _private_key().sign(message.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("utf-8")


def generate_signature(payload) -> tuple[str, str]:
    """Returns (signature_b64, canonical_message). Strings are signed as-is."""
    message = payload if isinstance(payload, str) else canonical_json(payload)
    return sign_message(message), message


def verify_wasabi_signature(raw_body: bytes, signature_b64: str) -> bool:
    """
    Verify a Wasabi-signed body (callback) against the platform public key.
    Assumes RSA/SHA-256 PKCS#1 v1.5 over the raw bytes - not documented by Wasabi, confirm with them.
    """
    if not signature_b64:
        return False
    try:
        _wasabi_public_key().verify(
            base64.b64decode(signature_b64),
            raw_body,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def get_public_key():
    with open(RSA_PUBLIC_KEY_PATH, "rb") as f:
        key = f.read().decode()

    if not key:
        raise ValueError("Public key is empty or not loaded")

    for marker in ("-----BEGIN PUBLIC KEY-----", "-----END PUBLIC KEY-----", "\n"):
        key = key.replace(marker, "")
    return key.strip()


def decrypt_message(ciphertext_b64: str) -> str:
    """
    Decrypt a Base64-encoded RSA ciphertext using the merchant's private key.
    Wasabi returns card PAN/CVV/expiry encrypted with the merchant's public key;
    this reverses it so the caller can expose plaintext card details (step-up auth
    should gate this endpoint in production).
    """
    try:
        ciphertext = base64.b64decode(ciphertext_b64)
        plaintext = _private_key().decrypt(
            ciphertext,
            padding.PKCS1v15(),
        )
        return plaintext.decode("utf-8")
    except (ValueError, InvalidKey) as exc:
        raise ValueError(f"Failed to decrypt sensitive payload: {exc}") from exc
