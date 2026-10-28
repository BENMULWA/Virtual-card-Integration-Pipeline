import json
from decimal import Decimal

import httpx
import pytest

from app.cards import wasabi_client as wc
from app.core.money import parse_amount, wire_amount
from app.core.signature import canonical_json, generate_signature
from app.transactions.orchestrator import build_order_no


def test_amounts_keep_decimals_and_reject_bad_input():
    assert parse_amount("10.50") == Decimal("10.50")
    assert wire_amount(parse_amount("10.5")) == 10.5          # old code sent 10
    assert json.dumps({"amount": wire_amount(parse_amount("0.07"))}) == '{"amount": 0.07}'
    for bad in ("1.234", "abc", "NaN", "inf"):
        with pytest.raises(ValueError):
            parse_amount(bad)


def test_signature_is_over_exact_transmitted_string():
    payload = {"b": 1, "a": {"z": 2, "y": 3}}
    sig, message = generate_signature(payload)
    assert message == canonical_json(payload) == '{"a":{"y":3,"z":2},"b":1}'
    assert sig  # base64


def test_order_numbers_are_deterministic_and_in_wasabi_limits():
    a = build_order_no("HLD", "user-" + "9" * 36, "key-12345678")
    assert a == build_order_no("HLD", "user-" + "9" * 36, "key-12345678")
    assert a != build_order_no("HLD", "user-2", "key-12345678")
    assert 20 <= len(a) <= 40


def _client_with(handler):
    wc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_post_sends_signed_exact_bytes_and_parses_success():
    seen = {}

    def handler(request):
        seen["body"] = request.content.decode()
        seen["sig"] = request.headers["X-WSB-SIGNATURE"]
        return httpx.Response(200, json={"success": True, "code": 200, "data": {"ok": 1}})

    _client_with(handler)
    result = await wc.post("/x", {"b": 2, "a": 1, "skip": None})
    assert seen["body"] == '{"a":1,"b":2}' and seen["sig"]
    assert result["data"] == {"ok": 1}


@pytest.mark.asyncio
async def test_error_classification():
    _client_with(lambda r: httpx.Response(200, json={"success": False, "code": 1001, "msg": "no"}))
    with pytest.raises(wc.WasabiError):
        await wc.post("/x", {})

    _client_with(lambda r: httpx.Response(502, text="bad gateway"))
    with pytest.raises(wc.WasabiUnknownOutcome):
        await wc.post("/x", {})

    _client_with(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(wc.WasabiUnknownOutcome):
        await wc.post("/x", {})

    def boom(request):
        raise httpx.ReadTimeout("slow")
    _client_with(boom)
    with pytest.raises(wc.WasabiUnknownOutcome):
        await wc.post("/x", {})


def test_loaders_accept_mislabelled_pkcs8_and_bare_base64_public_key():
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from app.core.signature import _load_private_key, _load_public_key

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pkcs8 = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption())
    mislabelled = pkcs8.replace(b"BEGIN PRIVATE KEY", b"BEGIN RSA PRIVATE KEY").replace(
        b"END PRIVATE KEY", b"END RSA PRIVATE KEY")
    assert _load_private_key(mislabelled).key_size == 2048

    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert _load_public_key(base64.b64encode(der)).key_size == 2048
